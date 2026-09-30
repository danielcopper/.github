import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import decisions  # noqa: E402
from decisions import Issue, PullRequest, evaluate  # noqa: E402

REPO = "octo/repo"

GOOD_ISSUE = """\
## Today

It works one way.

## Wanted

It works another way.

## Decisions

- Do it the other way.

## Done when

- It works the other way.
"""

FORM_ISSUE = """\
### Today

It works one way.

### Wanted

It works another way.

### Decisions

- Do it the other way.

### Done when

- It works the other way.
"""

CLAUDE_WITH_SECTION = """\
# Project

## Where decisions live

In the issue.

## Tests

Run them.
"""

CLAUDE_WITHOUT_SECTION = """\
# Project

## Tests

Run them.
"""


def issue(body, number=1, repository=REPO):
    return Issue(repository=repository, number=number, body=body)


def pull_request(**overrides):
    fields = dict(
        repository=REPO,
        head_branch="feature/thing",
        body="Closes #1",
        labels=[],
        base_claude_md=None,
        head_claude_md=None,
        issues=[issue(GOOD_ISSUE)],
    )
    fields.update(overrides)
    return PullRequest(**fields)


class ExemptBranchTest(unittest.TestCase):
    def test_renovate_branch_passes_without_any_other_check(self):
        verdict = evaluate(pull_request(
            head_branch="renovate/some-dependency",
            issues=[],
            base_claude_md=CLAUDE_WITH_SECTION,
            head_claude_md=None,
        ))
        self.assertTrue(verdict.passed)
        self.assertIn("exempt", verdict.notices[0])

    def test_release_please_branch_passes_without_any_other_check(self):
        verdict = evaluate(pull_request(head_branch="release-please--branches--main", issues=[]))
        self.assertTrue(verdict.passed)

    def test_branch_that_only_contains_the_prefix_is_not_exempt(self):
        verdict = evaluate(pull_request(head_branch="feature/renovate/x", issues=[]))
        self.assertFalse(verdict.passed)


class ClaudeMdGuardTest(unittest.TestCase):
    def test_base_and_head_with_section_pass(self):
        verdict = evaluate(pull_request(base_claude_md=CLAUDE_WITH_SECTION, head_claude_md=CLAUDE_WITH_SECTION))
        self.assertTrue(verdict.passed)

    def test_head_without_section_fails(self):
        verdict = evaluate(pull_request(base_claude_md=CLAUDE_WITH_SECTION, head_claude_md=CLAUDE_WITHOUT_SECTION))
        self.assertFalse(verdict.passed)
        self.assertIn("removes the section `## Where decisions live`", verdict.errors[0])

    def test_head_deleting_claude_md_fails(self):
        verdict = evaluate(pull_request(base_claude_md=CLAUDE_WITH_SECTION, head_claude_md=None))
        self.assertFalse(verdict.passed)
        self.assertIn("deletes CLAUDE.md", verdict.errors[0])

    def test_head_with_section_only_in_fenced_code_fails(self):
        head = "# Project\n\n```markdown\n## Where decisions live\n```\n"
        verdict = evaluate(pull_request(base_claude_md=CLAUDE_WITH_SECTION, head_claude_md=head))
        self.assertFalse(verdict.passed)

    def test_base_without_section_is_unaffected(self):
        verdict = evaluate(pull_request(base_claude_md=CLAUDE_WITHOUT_SECTION, head_claude_md=None))
        self.assertTrue(verdict.passed)

    def test_repository_without_claude_md_is_unaffected(self):
        verdict = evaluate(pull_request(base_claude_md=None, head_claude_md=None))
        self.assertTrue(verdict.passed)

    def test_base_with_section_only_in_fenced_code_is_unaffected(self):
        base = "# Project\n\n~~~\n## Where decisions live\n~~~\n"
        verdict = evaluate(pull_request(base_claude_md=base, head_claude_md=CLAUDE_WITHOUT_SECTION))
        self.assertTrue(verdict.passed)

    def test_only_the_heading_is_checked(self):
        head = "## Where decisions live\n"
        verdict = evaluate(pull_request(base_claude_md=CLAUDE_WITH_SECTION, head_claude_md=head))
        self.assertTrue(verdict.passed)

    def test_level3_section_in_head_does_not_count(self):
        head = "### Where decisions live\n"
        verdict = evaluate(pull_request(base_claude_md=CLAUDE_WITH_SECTION, head_claude_md=head))
        self.assertFalse(verdict.passed)

    def test_level3_section_in_base_is_unaffected(self):
        base = "### Where decisions live\n"
        verdict = evaluate(pull_request(base_claude_md=base, head_claude_md=CLAUDE_WITHOUT_SECTION))
        self.assertTrue(verdict.passed)

    def test_heading_with_different_text_does_not_count(self):
        head = "## Where decisions live now\n"
        verdict = evaluate(pull_request(base_claude_md=CLAUDE_WITH_SECTION, head_claude_md=head))
        self.assertFalse(verdict.passed)


class OptOutTest(unittest.TestCase):
    def test_label_skips_the_issue_rules(self):
        verdict = evaluate(pull_request(labels=["no-decisions"], issues=[]))
        self.assertTrue(verdict.passed)
        self.assertIn("Opted out", verdict.notices[0])

    def test_body_marker_skips_the_issue_rules(self):
        verdict = evaluate(pull_request(body="A typo fix.\n\ndecisions: none\n", issues=[]))
        self.assertTrue(verdict.passed)

    def test_body_marker_is_case_insensitive_and_spacing_free(self):
        for body in ("Decisions: None", "DECISIONS:none", "decisions:\tnone"):
            with self.subTest(body=body):
                self.assertTrue(evaluate(pull_request(body=body, issues=[])).passed)

    def test_opt_out_skips_a_failing_issue(self):
        verdict = evaluate(pull_request(labels=["no-decisions"], issues=[issue("## To decide\n\n- What?\n")]))
        self.assertTrue(verdict.passed)

    def test_opt_out_still_runs_the_claude_md_guard(self):
        verdict = evaluate(pull_request(
            labels=["no-decisions"],
            base_claude_md=CLAUDE_WITH_SECTION,
            head_claude_md=CLAUDE_WITHOUT_SECTION,
        ))
        self.assertFalse(verdict.passed)
        self.assertEqual(len(verdict.errors), 1)

    def test_body_marker_still_runs_the_claude_md_guard(self):
        verdict = evaluate(pull_request(
            body="decisions: none",
            base_claude_md=CLAUDE_WITH_SECTION,
            head_claude_md=None,
        ))
        self.assertFalse(verdict.passed)

    def test_other_label_does_not_opt_out(self):
        verdict = evaluate(pull_request(labels=["documentation"], issues=[]))
        self.assertFalse(verdict.passed)


class LinkedIssueTest(unittest.TestCase):
    def test_no_linked_issue_fails(self):
        verdict = evaluate(pull_request(issues=[]))
        self.assertFalse(verdict.passed)
        self.assertIn("No linked issue", verdict.errors[0])
        self.assertIn("decisions: none", verdict.errors[0])


class DecisionsSectionTest(unittest.TestCase):
    def test_issue_with_decisions_passes(self):
        self.assertTrue(evaluate(pull_request()).passed)

    def test_issue_without_decisions_fails(self):
        body = "## Today\n\nX\n\n## Wanted\n\nY\n"
        verdict = evaluate(pull_request(issues=[issue(body, number=7)]))
        self.assertFalse(verdict.passed)
        self.assertIn("Issue #7 has no `## Decisions` section", verdict.errors[0])

    def test_empty_decisions_fails(self):
        body = "## Decisions\n## Done when\n\n- Z\n"
        verdict = evaluate(pull_request(issues=[issue(body, number=8)]))
        self.assertFalse(verdict.passed)
        self.assertIn("Issue #8 has a `## Decisions` section, but it is empty", verdict.errors[0])

    def test_whitespace_only_decisions_fails(self):
        body = "## Wanted\n\nY\n\n## Decisions\n\n   \n\t\n\n## Done when\n\n- Z\n"
        self.assertFalse(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_decisions_at_end_of_body_with_only_whitespace_fails(self):
        body = "## Wanted\n\nY\n\n## Decisions\n\n  \n"
        self.assertFalse(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_pointer_to_epic_counts_as_content(self):
        body = "## Wanted\n\nY\n\n## Decisions\n\nSee epic #1896.\n"
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_level3_subheading_inside_decisions_counts_as_content(self):
        body = "## Decisions\n\n### D1: storage\n\n## Done when\n\n- Z\n"
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_decisions_heading_inside_fenced_code_does_not_count(self):
        body = "## Wanted\n\n```markdown\n## Decisions\n\n- Fake.\n```\n"
        self.assertFalse(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_level2_heading_inside_fenced_code_does_not_end_the_section(self):
        body = "## Decisions\n\n```\n## Not a heading\n```\n"
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_level3_decisions_passes(self):
        body = "### Decisions\n\n- Level three.\n"
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_level4_decisions_does_not_count(self):
        body = "#### Decisions\n\n- Level four.\n"
        self.assertFalse(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_level1_decisions_does_not_count(self):
        body = "# Decisions\n\n- Level one.\n"
        self.assertFalse(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_issue_form_body_with_decisions_passes(self):
        self.assertTrue(evaluate(pull_request(issues=[issue(FORM_ISSUE)])).passed)

    def test_no_response_decisions_fails(self):
        body = "### Wanted\n\nY\n\n### Decisions\n\n_No response_\n\n### Done when\n\n- Z\n"
        verdict = evaluate(pull_request(issues=[issue(body, number=6)]))
        self.assertFalse(verdict.passed)
        self.assertIn("Issue #6 has a `## Decisions` section, but it is empty", verdict.errors[0])

    def test_no_response_with_surrounding_whitespace_fails(self):
        body = "## Decisions\n\n  _No response_  \n\n"
        self.assertFalse(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_no_response_next_to_real_content_passes(self):
        body = "## Decisions\n\n_No response_\n\n- A real decision.\n"
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_level3_decisions_ends_at_the_next_level3_heading(self):
        body = "### Decisions\n\n### Done when\n\n- Z\n"
        self.assertFalse(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_level3_decisions_ends_at_the_next_level2_heading(self):
        body = "### Decisions\n\n## Done when\n\n- Z\n"
        self.assertFalse(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_level4_heading_inside_level3_decisions_counts_as_content(self):
        body = "### Decisions\n\n#### D1: storage\n\n### Done when\n\n- Z\n"
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_level2_decisions_ends_at_the_next_level1_heading(self):
        body = "## Decisions\n\n# Appendix\n\nText.\n"
        self.assertFalse(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_windows_line_endings(self):
        body = GOOD_ISSUE.replace("\n", "\r\n")
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_trailing_whitespace_on_heading_is_ignored(self):
        body = "## Decisions  \n\n- Yes.\n"
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_second_decisions_section_with_content_passes(self):
        body = "## Decisions\n\n## Decisions\n\n- Yes.\n"
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)


class ToDecideTest(unittest.TestCase):
    def test_open_questions_fail(self):
        body = GOOD_ISSUE + "\n## To decide\n\n- Which way?\n"
        verdict = evaluate(pull_request(issues=[issue(body, number=9)]))
        self.assertFalse(verdict.passed)
        self.assertEqual(len(verdict.errors), 1)
        self.assertIn("Issue #9 still has a `## To decide` section", verdict.errors[0])

    def test_open_questions_without_decisions_report_both_rules(self):
        body = "## To decide\n\n- Which way?\n"
        verdict = evaluate(pull_request(issues=[issue(body)]))
        self.assertEqual(len(verdict.errors), 2)

    def test_to_decide_inside_fenced_code_does_not_count(self):
        body = GOOD_ISSUE + "\n```\n## To decide\n```\n"
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_level3_to_decide_fails(self):
        body = GOOD_ISSUE + "\n### To decide\n\n- Which way?\n"
        verdict = evaluate(pull_request(issues=[issue(body, number=9)]))
        self.assertFalse(verdict.passed)
        self.assertIn("Issue #9 still has a `## To decide` section", verdict.errors[0])

    def test_issue_form_body_with_to_decide_fails(self):
        body = FORM_ISSUE.replace("### Decisions", "### To decide")
        self.assertFalse(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_level4_to_decide_does_not_count(self):
        body = GOOD_ISSUE + "\n#### To decide\n\n- Which way?\n"
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)


class MultipleIssuesTest(unittest.TestCase):
    def test_every_issue_must_pass(self):
        verdict = evaluate(pull_request(issues=[
            issue(GOOD_ISSUE, number=1),
            issue("## Wanted\n\nY\n", number=2),
            issue(GOOD_ISSUE, number=3),
        ]))
        self.assertFalse(verdict.passed)
        self.assertEqual(len(verdict.errors), 1)
        self.assertIn("Issue #2", verdict.errors[0])

    def test_all_passing_issues_pass(self):
        verdict = evaluate(pull_request(issues=[issue(GOOD_ISSUE, number=1), issue(GOOD_ISSUE, number=2)]))
        self.assertTrue(verdict.passed)

    def test_issue_in_another_repository_is_named_with_its_repository(self):
        verdict = evaluate(pull_request(issues=[issue("", number=4, repository="octo/other")]))
        self.assertIn("Issue octo/other#4", verdict.errors[0])


class WorkflowFileTest(unittest.TestCase):
    def test_file_name_from_workflow_ref(self):
        ref = "octo/repo/.github/workflows/decisions.yml@refs/heads/main"
        self.assertEqual(decisions.workflow_file(ref, REPO), "decisions.yml")

    def test_ref_from_another_repository_is_rejected(self):
        with self.assertRaises(ValueError):
            decisions.workflow_file("octo/other/.github/workflows/decisions.yml@refs/heads/main", REPO)


class FakeClient:
    def __init__(self, graphql_data=None, files=None, runs=None):
        self.graphql_data = graphql_data
        self.files = files or {}
        self.runs = runs or {}
        self.graphql_calls = []
        self.reruns = []

    def graphql(self, query, variables):
        self.graphql_calls.append(variables)
        return self.graphql_data

    def file_at(self, path, ref):
        return self.files.get((path, ref))

    def latest_run(self, workflow, head_sha):
        return self.runs.get((workflow, head_sha))

    def rerun(self, run_id):
        self.reruns.append(run_id)


def pull_request_event(head_ref="feature/thing"):
    return {"pull_request": {
        "number": 5,
        "head": {"ref": head_ref, "sha": "head-sha"},
        "base": {"sha": "base-sha"},
    }}


def pull_request_data(body="Closes #1", labels=(), issues=()):
    return {"repository": {"pullRequest": {
        "body": body,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "closingIssuesReferences": {"nodes": [
            {"number": number, "body": text, "repository": {"nameWithOwner": REPO}} for number, text in issues
        ]},
    }}}


def run_quietly(function, *args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = function(*args)
    return code, output.getvalue()


class RunPullRequestTest(unittest.TestCase):
    def test_reads_claude_md_at_the_event_commits(self):
        client = FakeClient(
            graphql_data=pull_request_data(issues=[(1, GOOD_ISSUE)]),
            files={("CLAUDE.md", "base-sha"): CLAUDE_WITH_SECTION, ("CLAUDE.md", "head-sha"): CLAUDE_WITHOUT_SECTION},
        )
        code, output = run_quietly(decisions.run_pull_request, pull_request_event(), client, REPO)
        self.assertEqual(code, 1)
        self.assertIn("::error::This pull request removes the section", output)
        self.assertEqual(client.graphql_calls, [{"owner": "octo", "name": "repo", "number": 5}])

    def test_passing_pull_request_exits_zero(self):
        client = FakeClient(graphql_data=pull_request_data(issues=[(1, GOOD_ISSUE)]))
        code, output = run_quietly(decisions.run_pull_request, pull_request_event(), client, REPO)
        self.assertEqual(code, 0)
        self.assertNotIn("::error::", output)

    def test_live_label_opts_out(self):
        client = FakeClient(graphql_data=pull_request_data(labels=["no-decisions"]))
        code, output = run_quietly(decisions.run_pull_request, pull_request_event(), client, REPO)
        self.assertEqual(code, 0)
        self.assertIn("::notice::Opted out", output)

    def test_exempt_branch_makes_no_api_call(self):
        client = FakeClient()
        code, _ = run_quietly(decisions.run_pull_request, pull_request_event("renovate/x"), client, REPO)
        self.assertEqual(code, 0)
        self.assertEqual(client.graphql_calls, [])

    def test_event_without_pull_request_fails(self):
        code, output = run_quietly(decisions.run_pull_request, {"push": {}}, FakeClient(), REPO)
        self.assertEqual(code, 1)
        self.assertIn("::error::", output)


def issue_data(*pulls):
    return {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": [
        {"number": number, "state": state, "headRefOid": sha, "repository": {"nameWithOwner": repository}}
        for number, state, sha, repository in pulls
    ]}}}}


WORKFLOW_REF = "octo/repo/.github/workflows/checks.yml@refs/heads/main"


class RunIssueTest(unittest.TestCase):
    def test_reruns_the_latest_completed_run_of_each_open_pull_request(self):
        client = FakeClient(
            graphql_data=issue_data((10, "OPEN", "sha10", REPO), (11, "OPEN", "sha11", REPO)),
            runs={
                ("checks.yml", "sha10"): {"id": 100, "status": "completed"},
                ("checks.yml", "sha11"): {"id": 110, "status": "completed"},
            },
        )
        code, _ = run_quietly(decisions.run_issue, {"issue": {"number": 3}}, client, REPO, WORKFLOW_REF)
        self.assertEqual(code, 0)
        self.assertEqual(client.reruns, [100, 110])
        self.assertEqual(client.graphql_calls, [{"owner": "octo", "name": "repo", "number": 3}])

    def test_skips_closed_pull_requests_and_other_repositories(self):
        client = FakeClient(
            graphql_data=issue_data((10, "MERGED", "sha10", REPO), (12, "OPEN", "sha12", "octo/other")),
            runs={("checks.yml", "sha10"): {"id": 100, "status": "completed"},
                  ("checks.yml", "sha12"): {"id": 120, "status": "completed"}},
        )
        code, output = run_quietly(decisions.run_issue, {"issue": {"number": 3}}, client, REPO, WORKFLOW_REF)
        self.assertEqual(code, 0)
        self.assertEqual(client.reruns, [])
        self.assertIn("No open pull request closes issue #3", output)

    def test_running_run_is_not_rerun_and_warns(self):
        client = FakeClient(
            graphql_data=issue_data((10, "OPEN", "sha10", REPO)),
            runs={("checks.yml", "sha10"): {"id": 100, "status": "in_progress"}},
        )
        code, output = run_quietly(decisions.run_issue, {"issue": {"number": 3}}, client, REPO, WORKFLOW_REF)
        self.assertEqual(code, 0)
        self.assertEqual(client.reruns, [])
        self.assertIn("::warning::", output)

    def test_pull_request_without_a_run_is_reported(self):
        client = FakeClient(graphql_data=issue_data((10, "OPEN", "sha10", REPO)))
        code, output = run_quietly(decisions.run_issue, {"issue": {"number": 3}}, client, REPO, WORKFLOW_REF)
        self.assertEqual(code, 0)
        self.assertEqual(client.reruns, [])
        self.assertIn("Pull request #10 has no checks.yml run", output)


if __name__ == "__main__":
    unittest.main()

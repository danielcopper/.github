import contextlib
import dataclasses
import email.message
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import decisions  # noqa: E402
from decisions import PLANNED_CHANGE, Issue, PullRequest, SharedForm, evaluate  # noqa: E402

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

SHARED_FORM = """\
name: Planned change
body:
  - type: textarea
    attributes:
      label: Out of scope
"""

OUTDATED_FORM = """\
name: Planned change
body:
  - type: textarea
    attributes:
      label: Done when
"""

SHARED = SharedForm(SHARED_FORM, "abcdef1234567890abcdef1234567890abcdef12")


def issue(body, number=1, repository=REPO):
    return Issue(repository=repository, number=number, body=body)


def pull_request(**overrides):
    default = PullRequest(
        repository=REPO,
        head_branch="feature/thing",
        from_fork=False,
        body="Closes #1",
        labels=[],
        base_claude_md=None,
        head_claude_md=None,
        head_planned_change=None,
        shared_planned_change=SHARED,
        issues=[issue(GOOD_ISSUE)],
    )
    return dataclasses.replace(default, **overrides)


class ExemptBranchTest(unittest.TestCase):
    def test_renovate_branch_is_exempt_from_the_issue_rules(self):
        verdict = evaluate(pull_request(head_branch="renovate/some-dependency", issues=[]))
        self.assertTrue(verdict.passed)
        self.assertIn("exempt", verdict.notices[0])

    def test_release_please_branch_is_exempt_from_the_issue_rules(self):
        verdict = evaluate(pull_request(head_branch="release-please--branches--main", issues=[]))
        self.assertTrue(verdict.passed)

    def test_bot_branch_from_a_fork_is_not_exempt(self):
        for branch in ("renovate/x", "release-please--branches--main"):
            with self.subTest(branch=branch):
                verdict = evaluate(pull_request(head_branch=branch, from_fork=True, issues=[]))
                self.assertFalse(verdict.passed)
                self.assertIn("No linked issue", verdict.errors[0])

    def test_bot_branch_that_removes_the_claude_md_section_fails(self):
        verdict = evaluate(pull_request(
            head_branch="renovate/some-dependency",
            issues=[],
            base_claude_md=CLAUDE_WITH_SECTION,
            head_claude_md=CLAUDE_WITHOUT_SECTION,
        ))
        self.assertFalse(verdict.passed)
        self.assertEqual(len(verdict.errors), 1)
        self.assertIn("removes the section `## Where decisions live`", verdict.errors[0])

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


class PlannedChangeCopyTest(unittest.TestCase):
    def test_identical_copy_passes(self):
        self.assertTrue(evaluate(pull_request(head_planned_change=SHARED_FORM)).passed)

    def test_absent_copy_passes(self):
        self.assertTrue(evaluate(pull_request(head_planned_change=None)).passed)

    def test_differing_copy_fails_and_says_to_copy_the_shared_form(self):
        verdict = evaluate(pull_request(head_planned_change=OUTDATED_FORM))
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.errors, [
            ".github/ISSUE_TEMPLATE/planned_change.yml differs from the shared form at "
            "danielcopper/.github@abcdef1. Copy the shared file over it unchanged."
        ])

    def test_copy_with_other_line_endings_fails(self):
        verdict = evaluate(pull_request(head_planned_change=SHARED_FORM.replace("\n", "\r\n")))
        self.assertFalse(verdict.passed)

    def test_copy_without_the_final_newline_fails(self):
        verdict = evaluate(pull_request(head_planned_change=SHARED_FORM.rstrip("\n")))
        self.assertFalse(verdict.passed)

    def test_exempt_bot_branch_still_checks_the_copy(self):
        for branch in ("renovate/x", "release-please--branches--main"):
            with self.subTest(branch=branch):
                verdict = evaluate(pull_request(head_branch=branch, issues=[], head_planned_change=OUTDATED_FORM))
                self.assertFalse(verdict.passed)
                self.assertEqual(len(verdict.errors), 1)
                self.assertIn("differs from the shared form", verdict.errors[0])
                self.assertIn("exempt", verdict.notices[0])

    def test_opt_out_still_checks_the_copy(self):
        for overrides in ({"labels": ["no-decisions"]}, {"body": "decisions: none"}):
            with self.subTest(overrides=overrides):
                verdict = evaluate(pull_request(issues=[], head_planned_change=OUTDATED_FORM, **overrides))
                self.assertFalse(verdict.passed)
                self.assertEqual(len(verdict.errors), 1)
                self.assertIn("differs from the shared form", verdict.errors[0])
                self.assertIn("Opted out", verdict.notices[0])

    def test_claude_md_guard_and_copy_rule_both_report(self):
        verdict = evaluate(pull_request(
            base_claude_md=CLAUDE_WITH_SECTION,
            head_claude_md=None,
            head_planned_change=OUTDATED_FORM,
        ))
        self.assertEqual(len(verdict.errors), 2)


class ReadSharedFormTest(unittest.TestCase):
    def test_reads_the_file_with_its_line_endings_and_the_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "planned_change.yml")
            with open(path, "wb") as handle:
                handle.write(b"name: Planned change\r\nbody: []\n")
            shared = decisions.read_shared_form(path, "0123456789")
        self.assertEqual(shared, SharedForm("name: Planned change\r\nbody: []\n", "0123456789"))


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


class CommonMarkTest(unittest.TestCase):
    """Headings and fences follow CommonMark, read through the Decisions and To decide rules."""

    def passes(self, body):
        return evaluate(pull_request(issues=[issue(body)])).passed

    def test_heading_indented_up_to_three_spaces_counts(self):
        for indent in (" ", "  ", "   "):
            with self.subTest(indent=len(indent)):
                self.assertTrue(self.passes(indent + "## Decisions\n\n- Yes.\n"))
                self.assertFalse(self.passes(GOOD_ISSUE + indent + "### To decide\n\n- Which way?\n"))

    def test_heading_indented_four_spaces_does_not_count(self):
        self.assertFalse(self.passes("    ## Decisions\n\n- Yes.\n"))
        self.assertTrue(self.passes(GOOD_ISSUE + "    ## To decide\n\n- Which way?\n"))

    def test_closing_sequence_is_stripped(self):
        self.assertTrue(self.passes("## Decisions ##\n\n- Yes.\n"))
        self.assertTrue(self.passes("### Decisions #####  \n\n- Yes.\n"))
        self.assertFalse(self.passes(GOOD_ISSUE + "### To decide ###\n\n- Which way?\n"))

    def test_hashes_without_a_space_are_part_of_the_title(self):
        self.assertFalse(self.passes("## Decisions##\n\n- Yes.\n"))

    def test_title_before_the_closing_sequence_must_still_match(self):
        self.assertFalse(self.passes("## Decisions later ##\n\n- Yes.\n"))

    def test_spaces_or_tabs_after_the_opening_hashes(self):
        for separator in ("  ", "\t", " \t "):
            with self.subTest(separator=separator):
                self.assertTrue(self.passes("##" + separator + "Decisions\n\n- Yes.\n"))

    def test_no_space_after_the_opening_hashes_is_not_a_heading(self):
        self.assertFalse(self.passes("##Decisions\n\n- Yes.\n"))

    def test_title_stays_case_sensitive(self):
        self.assertFalse(self.passes("## decisions\n\n- Yes.\n"))
        self.assertTrue(self.passes(GOOD_ISSUE + "## to decide\n\n- Which way?\n"))
        verdict = evaluate(pull_request(base_claude_md=CLAUDE_WITH_SECTION, head_claude_md="## where decisions live\n"))
        self.assertFalse(verdict.passed)

    def test_backtick_info_string_with_a_backtick_is_not_a_fence(self):
        body = GOOD_ISSUE + "\n``` not`a fence\n## To decide\n\n- Which way?\n"
        self.assertFalse(self.passes(body))

    def test_tilde_info_string_with_a_backtick_is_a_fence(self):
        body = GOOD_ISSUE + "\n~~~ a`fence\n## To decide\n\n- Which way?\n~~~\n"
        self.assertTrue(self.passes(body))

    def test_fence_indented_up_to_three_spaces_hides_headings(self):
        body = GOOD_ISSUE + "\n   ```\n## To decide\n\n- Which way?\n   ```\n"
        self.assertTrue(self.passes(body))

    def test_fence_indented_four_spaces_is_not_a_fence(self):
        body = GOOD_ISSUE + "\n    ```\n## To decide\n\n- Which way?\n"
        self.assertFalse(self.passes(body))

    def test_closing_fence_indented_up_to_three_spaces_closes(self):
        self.assertTrue(self.passes("```\ncode\n   ```\n## Decisions\n\n- Yes.\n"))

    def test_closing_fence_indented_four_spaces_does_not_close(self):
        self.assertFalse(self.passes("```\ncode\n    ```\n## Decisions\n\n- Yes.\n"))

    def test_closing_fence_with_trailing_text_does_not_close(self):
        self.assertFalse(self.passes("```\ncode\n``` more\n## Decisions\n\n- Yes.\n"))

    def test_shorter_closing_fence_does_not_close(self):
        self.assertFalse(self.passes("````\ncode\n```\n## Decisions\n\n- Yes.\n"))

    def test_other_fence_character_does_not_close(self):
        self.assertFalse(self.passes("```\ncode\n~~~\n## Decisions\n\n- Yes.\n"))


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

    def test_empty_to_decide_passes(self):
        body = GOOD_ISSUE + "\n## To decide\n"
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_empty_to_decide_before_the_next_section_passes(self):
        body = "## To decide\n\n## Decisions\n\n- Yes.\n"
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_whitespace_only_to_decide_passes(self):
        body = GOOD_ISSUE + "\n## To decide\n\n   \n\t\n"
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_issue_form_blank_to_decide_passes_when_decisions_has_content(self):
        body = FORM_ISSUE.replace("### Done when", "### To decide\n\n_No response_\n\n### Done when")
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_level3_to_decide_ends_at_the_next_level3_heading(self):
        body = FORM_ISSUE.replace("### Done when", "### To decide\n\n### Done when")
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_to_decide_with_fenced_content_fails(self):
        body = GOOD_ISSUE + "\n## To decide\n\n```\nwhich way?\n```\n"
        self.assertFalse(evaluate(pull_request(issues=[issue(body)])).passed)

    def test_one_of_two_to_decide_sections_with_content_fails(self):
        empty = "## To decide\n\n_No response_\n"
        filled = "## To decide\n\n- Which way?\n"
        for sections in ((empty, filled), (filled, empty)):
            with self.subTest(first=sections[0]):
                body = GOOD_ISSUE + "\n" + sections[0] + "\n" + sections[1]
                verdict = evaluate(pull_request(issues=[issue(body, number=9)]))
                self.assertFalse(verdict.passed)
                self.assertEqual(len(verdict.errors), 1)
                self.assertIn("Issue #9 still has a `## To decide` section", verdict.errors[0])

    def test_level4_to_decide_does_not_count(self):
        body = GOOD_ISSUE + "\n#### To decide\n\n- Which way?\n"
        self.assertTrue(evaluate(pull_request(issues=[issue(body)])).passed)


class ToDecideTaskListTest(unittest.TestCase):
    """Under To decide, only checked task-list items are settled."""

    def verdict(self, to_decide, heading="## To decide"):
        return evaluate(pull_request(issues=[issue(GOOD_ISSUE + "\n" + heading + "\n\n" + to_decide, number=9)]))

    def test_all_items_checked_passes(self):
        self.assertTrue(self.verdict("- [x] Which way? → D1\n- [x] How fast? → D2\n").passed)

    def test_one_unchecked_item_fails_and_names_it(self):
        verdict = self.verdict("- [x] Which way? → D1\n- [ ] How fast?\n")
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.errors, [
            'Issue #9 still has a `## To decide` section with open questions: "- [ ] How fast?". '
            "Answer each under `## Decisions` and check it off (`- [x]`)."
        ])

    def test_checked_item_with_indented_continuation_passes(self):
        for continuation in ("  → D1, see the comment.\n", "\t→ D1.\n", "\n  A second paragraph.\n"):
            with self.subTest(continuation=continuation):
                self.assertTrue(self.verdict("- [x] Which way?\n" + continuation + "- [x] How fast?\n").passed)

    def test_intro_prose_line_fails(self):
        verdict = self.verdict("These are settled:\n\n- [x] Which way?\n")
        self.assertFalse(verdict.passed)
        self.assertIn('"These are settled:"', verdict.errors[0])

    def test_unindented_line_after_a_checked_item_fails(self):
        self.assertFalse(self.verdict("- [x] Which way?\nAnd one more thing.\n").passed)

    def test_indented_line_without_an_item_fails(self):
        self.assertFalse(self.verdict("  Which way?\n").passed)

    def test_plain_list_item_fails(self):
        self.assertFalse(self.verdict("- [x] Which way?\n- How fast?\n").passed)

    def test_no_response_passes(self):
        self.assertTrue(self.verdict("_No response_\n", heading="### To decide").passed)

    def test_star_and_plus_markers(self):
        for marker in ("*", "+"):
            with self.subTest(marker=marker):
                self.assertTrue(self.verdict(f"{marker} [x] Which way?\n").passed)
                self.assertFalse(self.verdict(f"{marker} [ ] Which way?\n").passed)

    def test_uppercase_x_is_checked(self):
        self.assertTrue(self.verdict("- [X] Which way?\n").passed)

    def test_unchecked_item_nested_under_a_checked_item_fails(self):
        self.assertFalse(self.verdict("- [x] Which way?\n  - [ ] And which detail?\n").passed)

    def test_every_open_line_is_named(self):
        verdict = self.verdict("- [ ] Which way?\n- [x] Settled.\n- [ ] How fast?\n")
        self.assertIn('"- [ ] Which way?", "- [ ] How fast?"', verdict.errors[0])

    def test_box_without_a_space_before_the_text_is_not_an_item(self):
        self.assertFalse(self.verdict("- [x]Which way?\n").passed)


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

    def graphql(self, _query, variables):
        self.graphql_calls.append(variables)
        return self.graphql_data

    def file_at(self, path, ref):
        return self.files.get((path, ref))

    def latest_run(self, workflow, head_sha):
        return self.runs.get((workflow, head_sha))

    def rerun(self, run_id):
        self.reruns.append(run_id)


def pull_request_event(head_ref="feature/thing", head_repository=REPO):
    return {"pull_request": {
        "number": 5,
        "head": {"ref": head_ref, "sha": "head-sha", "repo": {"full_name": head_repository}},
        "base": {"sha": "base-sha", "repo": {"full_name": REPO}},
    }}


def deleted_fork_event(head_ref="feature/thing"):
    event = pull_request_event(head_ref)
    event["pull_request"]["head"]["repo"] = None
    return event


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
        code, output = run_quietly(decisions.run_pull_request, pull_request_event(), client, REPO, SHARED)
        self.assertEqual(code, 1)
        self.assertIn("::error::This pull request removes the section", output)
        self.assertEqual(client.graphql_calls, [{"owner": "octo", "name": "repo", "number": 5}])

    def test_passing_pull_request_exits_zero(self):
        client = FakeClient(graphql_data=pull_request_data(issues=[(1, GOOD_ISSUE)]))
        code, output = run_quietly(decisions.run_pull_request, pull_request_event(), client, REPO, SHARED)
        self.assertEqual(code, 0)
        self.assertNotIn("::error::", output)

    def test_live_label_opts_out(self):
        client = FakeClient(graphql_data=pull_request_data(labels=["no-decisions"]))
        code, output = run_quietly(decisions.run_pull_request, pull_request_event(), client, REPO, SHARED)
        self.assertEqual(code, 0)
        self.assertIn("::notice::Opted out", output)

    def test_exempt_branch_skips_the_pull_request_query(self):
        client = FakeClient()
        code, _ = run_quietly(decisions.run_pull_request, pull_request_event("renovate/x"), client, REPO, SHARED)
        self.assertEqual(code, 0)
        self.assertEqual(client.graphql_calls, [])

    def test_exempt_branch_still_runs_the_claude_md_guard(self):
        client = FakeClient(files={("CLAUDE.md", "base-sha"): CLAUDE_WITH_SECTION})
        code, output = run_quietly(decisions.run_pull_request, pull_request_event("renovate/x"), client, REPO, SHARED)
        self.assertEqual(code, 1)
        self.assertIn("::error::This pull request deletes CLAUDE.md", output)

    def test_reads_the_planned_change_copy_at_the_head_commit(self):
        cases = (
            ("head differs", OUTDATED_FORM, SHARED_FORM, 1),
            ("base differs", SHARED_FORM, OUTDATED_FORM, 0),
        )
        for name, head, base, expected in cases:
            with self.subTest(name):
                client = FakeClient(
                    graphql_data=pull_request_data(issues=[(1, GOOD_ISSUE)]),
                    files={(PLANNED_CHANGE, "head-sha"): head, (PLANNED_CHANGE, "base-sha"): base},
                )
                code, output = run_quietly(decisions.run_pull_request, pull_request_event(), client, REPO, SHARED)
                self.assertEqual(code, expected)
                self.assertEqual("::error::.github/ISSUE_TEMPLATE/planned_change.yml differs" in output, bool(expected))

    def test_exempt_branch_still_checks_the_planned_change_copy(self):
        client = FakeClient(files={(PLANNED_CHANGE, "head-sha"): OUTDATED_FORM})
        code, output = run_quietly(decisions.run_pull_request, pull_request_event("renovate/x"), client, REPO, SHARED)
        self.assertEqual(code, 1)
        self.assertIn("::error::.github/ISSUE_TEMPLATE/planned_change.yml differs from the shared form", output)
        self.assertEqual(client.graphql_calls, [])

    def test_bot_branch_from_a_fork_gets_the_issue_rules(self):
        client = FakeClient(graphql_data=pull_request_data())
        event = pull_request_event("renovate/x", head_repository="someone/repo")
        code, output = run_quietly(decisions.run_pull_request, event, client, REPO, SHARED)
        self.assertEqual(code, 1)
        self.assertIn("::error::No linked issue", output)

    def test_deleted_fork_bot_branch_is_not_exempt(self):
        event = deleted_fork_event("renovate/x")
        client = FakeClient(graphql_data=pull_request_data())
        code, output = run_quietly(decisions.run_pull_request, event, client, REPO, SHARED)
        self.assertEqual(code, 1)
        self.assertIn("::error::No linked issue", output)
        self.assertNotIn("exempt", output)

    def test_deleted_fork_can_opt_out(self):
        client = FakeClient(
            graphql_data=pull_request_data(body="decisions: none"),
            files={("CLAUDE.md", "base-sha"): CLAUDE_WITH_SECTION, ("CLAUDE.md", "head-sha"): CLAUDE_WITH_SECTION},
        )
        code, output = run_quietly(decisions.run_pull_request, deleted_fork_event(), client, REPO, SHARED)
        self.assertEqual(code, 0)
        self.assertIn("::notice::Opted out", output)

    def test_deleted_fork_gets_the_issue_rules(self):
        client = FakeClient(graphql_data=pull_request_data(issues=[(1, "## Wanted\n\nY\n")]))
        code, output = run_quietly(decisions.run_pull_request, deleted_fork_event(), client, REPO, SHARED)
        self.assertEqual(code, 1)
        self.assertIn("::error::Issue #1 has no `## Decisions` section", output)

    def test_event_without_base_repository_fails(self):
        event = pull_request_event()
        del event["pull_request"]["base"]["repo"]
        client = FakeClient()
        code, output = run_quietly(decisions.run_pull_request, event, client, REPO, SHARED)
        self.assertEqual(code, 1)
        self.assertIn("::error::The pull_request event names no base repository", output)
        self.assertEqual(client.graphql_calls, [])

    def test_event_without_pull_request_fails(self):
        code, output = run_quietly(decisions.run_pull_request, {"push": {}}, FakeClient(), REPO, SHARED)
        self.assertEqual(code, 1)
        self.assertIn("::error::", output)


def issue_data(*pulls):
    return {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": [
        {"number": number, "state": state, "headRefOid": sha, "repository": {"nameWithOwner": repository}}
        for number, state, sha, repository in pulls
    ]}}}}


WORKFLOW_REF = "octo/repo/.github/workflows/checks.yml@refs/heads/main"


class RunIssueTest(unittest.TestCase):
    def test_reruns_the_completed_run_of_each_open_pull_request(self):
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


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self.payload


def http_error(code):
    return urllib.error.HTTPError("https://api.example/x", code, "status", email.message.Message(), None)


class PlannedChangeOverTheApiTest(unittest.TestCase):
    """The copy is read like CLAUDE.md: 404 means absent, any other error fails the check."""

    def run_with(self, planned_change_response):
        def urlopen(request, timeout):
            if request.full_url.endswith("/graphql"):
                return FakeResponse(json.dumps({"data": pull_request_data(issues=[(1, GOOD_ISSUE)])}).encode())
            if f"/contents/{PLANNED_CHANGE}?ref=head-sha" in request.full_url:
                if isinstance(planned_change_response, Exception):
                    raise planned_change_response
                return FakeResponse(planned_change_response)
            raise http_error(404)

        client = decisions.GitHubClient("token", REPO, "https://api.example", "https://api.example/graphql")
        with mock.patch.object(decisions.urllib.request, "urlopen", side_effect=urlopen):
            return run_quietly(decisions.run_pull_request, pull_request_event(), client, REPO, SHARED)

    def test_identical_copy_passes(self):
        code, _ = self.run_with(SHARED_FORM.encode())
        self.assertEqual(code, 0)

    def test_differing_copy_fails(self):
        code, output = self.run_with(OUTDATED_FORM.encode())
        self.assertEqual(code, 1)
        self.assertIn("::error::.github/ISSUE_TEMPLATE/planned_change.yml differs", output)

    def test_404_means_the_copy_is_absent(self):
        code, output = self.run_with(http_error(404))
        self.assertEqual(code, 0)
        self.assertNotIn("::error::", output)

    def test_server_error_fails_the_check(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.run_with(http_error(500))


class GitHubClientTest(unittest.TestCase):
    def setUp(self):
        self.client = decisions.GitHubClient("token", REPO, "https://api.example", "https://api.example/graphql")

    def test_file_at_returns_the_file_text(self):
        with mock.patch.object(decisions.urllib.request, "urlopen", return_value=FakeResponse(b"# Project\n")):
            self.assertEqual(self.client.file_at("CLAUDE.md", "sha"), "# Project\n")

    def test_file_at_returns_none_when_the_file_is_absent(self):
        with mock.patch.object(decisions.urllib.request, "urlopen", side_effect=http_error(404)):
            self.assertIsNone(self.client.file_at("CLAUDE.md", "sha"))

    def test_file_at_raises_on_a_server_error(self):
        with mock.patch.object(decisions.urllib.request, "urlopen", side_effect=http_error(500)):
            with self.assertRaises(urllib.error.HTTPError):
                self.client.file_at("CLAUDE.md", "sha")

    def test_file_at_raises_when_access_is_refused(self):
        with mock.patch.object(decisions.urllib.request, "urlopen", side_effect=http_error(403)):
            with self.assertRaises(urllib.error.HTTPError):
                self.client.file_at("CLAUDE.md", "sha")

    def test_graphql_returns_the_data(self):
        body = json.dumps({"data": {"repository": {}}}).encode()
        with mock.patch.object(decisions.urllib.request, "urlopen", return_value=FakeResponse(body)):
            self.assertEqual(self.client.graphql("query", {}), {"repository": {}})

    def test_graphql_raises_on_errors(self):
        body = json.dumps({"data": None, "errors": [{"message": "Resource not accessible by integration"}]}).encode()
        with mock.patch.object(decisions.urllib.request, "urlopen", return_value=FakeResponse(body)):
            with self.assertRaisesRegex(RuntimeError, "Resource not accessible by integration"):
                self.client.graphql("query", {})

    def test_graphql_raises_on_an_http_error(self):
        with mock.patch.object(decisions.urllib.request, "urlopen", side_effect=http_error(502)):
            with self.assertRaises(urllib.error.HTTPError):
                self.client.graphql("query", {})


if __name__ == "__main__":
    unittest.main()

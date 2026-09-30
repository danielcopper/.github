#!/usr/bin/env python3
"""The decisions check: a pull request needs an issue whose decisions are written down.

Two modes, one per event the calling workflow runs on:

  decisions.py pull-request   evaluate the pull request in GITHUB_EVENT_PATH
  decisions.py issue          re-run the check of every open pull request that
                              closes the edited issue in GITHUB_EVENT_PATH

Python 3 standard library only. The rules live in plain functions over strings
so they can be tested without the GitHub API; the API sits behind GitHubClient.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

DECISIONS = "Decisions"
TO_DECIDE = "To decide"
CLAUDE_MD = "CLAUDE.md"
CLAUDE_MD_SECTION = "Where decisions live"
# Issue forms render each field as a level-3 heading; hand-written issues use level 2.
ISSUE_HEADING_LEVELS = (2, 3)
# What an issue form writes for an optional field left blank.
NO_RESPONSE = "_No response_"
OPT_OUT_LABEL = "no-decisions"
OPT_OUT_BODY = re.compile(r"decisions:\s*none", re.IGNORECASE)
EXEMPT_BRANCH_PREFIXES = ("renovate/", "release-please--")

# Markdown as CommonMark reads it: headings and fences may be indented up to three spaces.
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_CLOSING_SEQUENCE = re.compile(r"(?:^|[ \t]+)#+$")


# --- Markdown -------------------------------------------------------------


def _unfenced_lines(text: str) -> list[str | None]:
    """Return the lines of text, with every line inside a fenced code block as None.

    A fence opens with three or more backticks or tildes, indented at most three
    spaces; a backtick fence's info string may not contain a backtick. It closes
    with a line of at least as many of the same character, indented at most
    three spaces and followed only by whitespace. An unclosed fence runs to the
    end of the text.
    """
    result: list[str | None] = []
    fence: str | None = None
    for line in text.splitlines():
        if fence is None:
            opening = _FENCE_OPEN.match(line)
            if opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
                fence = opening.group(1)
                result.append(None)
            else:
                result.append(line.rstrip())
        else:
            closing = _FENCE_CLOSE.match(line)
            if closing and closing.group(1)[0] == fence[0] and len(closing.group(1)) >= len(fence):
                fence = None
            result.append(None)
    return result


def _heading(line: str | None) -> tuple[int, str] | None:
    """The level and title of an ATX heading line such as `### Decisions`, else None.

    An optional closing sequence of `#`s is dropped: `## Decisions ##` has the title `Decisions`.
    """
    if line is None:
        return None
    match = _ATX_HEADING.match(line)
    if not match:
        return None
    title = _CLOSING_SEQUENCE.sub("", match.group(2) or "")
    return len(match.group(1)), title.strip()


def _is_content(line: str | None) -> bool:
    """Whether a line inside a section counts as written content.

    Fenced lines count. Blank lines and an issue form's `_No response_` do not.
    """
    if line is None:
        return True
    stripped = line.strip()
    return bool(stripped) and stripped != NO_RESPONSE


def has_heading(text: str, title: str, levels: tuple[int, ...]) -> bool:
    """Whether text has a heading of one of the levels with exactly this title, outside fenced code."""
    return any(
        heading is not None and heading[0] in levels and heading[1] == title
        for heading in map(_heading, _unfenced_lines(text))
    )


def section_has_content(text: str, title: str, levels: tuple[int, ...]) -> bool:
    """Whether a section with this title has content.

    The section starts at a heading of one of the levels with exactly this
    title and ends at the next heading of the same or a higher level (fewer
    `#`). Deeper headings inside it are content.
    """
    open_level: int | None = None
    for line in _unfenced_lines(text):
        heading = _heading(line)
        if heading is not None and open_level is not None and heading[0] <= open_level:
            open_level = None
        if heading is not None and open_level is None:
            if heading[0] in levels and heading[1] == title:
                open_level = heading[0]
            continue
        if open_level is not None and _is_content(line):
            return True
    return False


# --- Rules ----------------------------------------------------------------


@dataclass
class Issue:
    repository: str
    number: int
    body: str


@dataclass
class PullRequest:
    repository: str
    head_branch: str
    from_fork: bool
    body: str
    labels: list[str]
    base_claude_md: str | None
    head_claude_md: str | None
    issues: list[Issue]


@dataclass
class Verdict:
    errors: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors


def is_exempt(branch: str, from_fork: bool) -> bool:
    """Whether a bot branch is exempt from the linked-issue rules. A fork never is."""
    return not from_fork and branch.startswith(EXEMPT_BRANCH_PREFIXES)


def is_opted_out(labels: list[str], body: str) -> bool:
    return OPT_OUT_LABEL in labels or bool(OPT_OUT_BODY.search(body))


def claude_md_error(base: str | None, head: str | None) -> str | None:
    """The CLAUDE.md guard: a section the base branch has must not be removed."""
    if base is None or not has_heading(base, CLAUDE_MD_SECTION, levels=(2,)):
        return None
    if head is None:
        return (
            f"This pull request deletes {CLAUDE_MD}, which on the base branch has the "
            f"section `## {CLAUDE_MD_SECTION}`. Keep the file and the section."
        )
    if not has_heading(head, CLAUDE_MD_SECTION, levels=(2,)):
        return (
            f"This pull request removes the section `## {CLAUDE_MD_SECTION}` from "
            f"{CLAUDE_MD}. The base branch has it; keep it."
        )
    return None


def issue_errors(issue: Issue, repository: str) -> list[str]:
    name = f"#{issue.number}" if issue.repository == repository else f"{issue.repository}#{issue.number}"
    errors = []
    if not has_heading(issue.body, DECISIONS, ISSUE_HEADING_LEVELS):
        errors.append(
            f"Issue {name} has no `## {DECISIONS}` section. Write the decisions down in the "
            f"issue under `## {DECISIONS}` before the pull request can pass."
        )
    elif not section_has_content(issue.body, DECISIONS, ISSUE_HEADING_LEVELS):
        errors.append(
            f"Issue {name} has a `## {DECISIONS}` section, but it is empty. Write the "
            "decisions into it (a pointer such as \"See epic #N.\" is enough)."
        )
    # An empty To decide section, such as a blank issue form field, holds no open questions.
    if section_has_content(issue.body, TO_DECIDE, ISSUE_HEADING_LEVELS):
        errors.append(
            f"Issue {name} still has a `## {TO_DECIDE}` section, so it has open questions. "
            f"Settle them, then rename the section to `## {DECISIONS}`."
        )
    return errors


def evaluate(pr: PullRequest) -> Verdict:
    """Apply the rules to a pull request, in order."""
    verdict = Verdict()
    guard = claude_md_error(pr.base_claude_md, pr.head_claude_md)
    if guard:
        verdict.errors.append(guard)

    if is_exempt(pr.head_branch, pr.from_fork):
        verdict.notices.append(
            f"Branch {pr.head_branch} is exempt from the linked-issue rules; only the {CLAUDE_MD} section is checked."
        )
        return verdict

    if is_opted_out(pr.labels, pr.body):
        verdict.notices.append(
            "Opted out of the linked-issue rules (`decisions: none` or the "
            f"`{OPT_OUT_LABEL}` label); only the {CLAUDE_MD} section is checked."
        )
        return verdict

    if not pr.issues:
        verdict.errors.append(
            "No linked issue. Link an issue with `Closes #N`, or opt out with "
            f"`decisions: none` in the description or the `{OPT_OUT_LABEL}` label."
        )
        return verdict

    for issue in pr.issues:
        verdict.errors.extend(issue_errors(issue, pr.repository))
    return verdict


def workflow_file(workflow_ref: str, repository: str) -> str:
    """The file name of the calling workflow, from `github.workflow_ref`.

    `octo/repo/.github/workflows/decisions.yml@refs/heads/main` -> `decisions.yml`
    """
    path = workflow_ref.split("@", 1)[0]
    prefix = f"{repository}/"
    if not path.startswith(prefix):
        raise ValueError(f"workflow ref {workflow_ref!r} is not in repository {repository}")
    return posixpath.basename(path[len(prefix):])


# --- GitHub API -----------------------------------------------------------


class Client(Protocol):
    def graphql(self, query: str, variables: dict) -> dict: ...

    def file_at(self, path: str, ref: str) -> str | None: ...

    def latest_run(self, workflow: str, head_sha: str) -> dict | None: ...

    def rerun(self, run_id: int) -> None: ...


class GitHubClient:
    def __init__(self, token: str, repository: str, api_url: str, graphql_url: str):
        self.repository = repository
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._graphql_url = graphql_url

    def _request(self, method: str, url: str, payload: dict | None = None, accept: str = "application/vnd.github+json"):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", accept)
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    def graphql(self, query: str, variables: dict) -> dict:
        raw = self._request("POST", self._graphql_url, {"query": query, "variables": variables})
        result = json.loads(raw)
        if result.get("errors"):
            raise RuntimeError(f"GraphQL errors: {json.dumps(result['errors'])}")
        return result["data"]

    def file_at(self, path: str, ref: str) -> str | None:
        url = f"{self._api_url}/repos/{self.repository}/contents/{path}?ref={ref}"
        try:
            return self._request("GET", url, accept="application/vnd.github.raw+json").decode()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def latest_run(self, workflow: str, head_sha: str) -> dict | None:
        url = (
            f"{self._api_url}/repos/{self.repository}/actions/workflows/{workflow}/runs"
            f"?event=pull_request&head_sha={head_sha}&per_page=1"
        )
        runs = json.loads(self._request("GET", url))["workflow_runs"]
        return runs[0] if runs else None

    def rerun(self, run_id: int) -> None:
        self._request("POST", f"{self._api_url}/repos/{self.repository}/actions/runs/{run_id}/rerun")


PULL_REQUEST_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      body
      labels(first: 100) { nodes { name } }
      closingIssuesReferences(first: 100) {
        nodes { number body repository { nameWithOwner } }
      }
    }
  }
}
"""

ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      closedByPullRequestsReferences(first: 100, includeClosedPrs: false) {
        nodes { number state headRefOid repository { nameWithOwner } }
      }
    }
  }
}
"""


def _repo_variables(repository: str, number: int) -> dict:
    owner, name = repository.split("/", 1)
    return {"owner": owner, "name": name, "number": number}


def load_pull_request(event: dict, client: Client, repository: str) -> PullRequest:
    """Build the PullRequest to judge.

    The commits come from the event, so the verdict belongs to the commit the
    run reports on. Description, labels and linked issues are read live: a
    re-run replays the original event, but the issue may have changed since.
    """
    pr = event["pull_request"]
    head_branch = pr["head"]["ref"]
    from_fork = pr["head"]["repo"]["full_name"] != pr["base"]["repo"]["full_name"]
    base_claude_md = client.file_at(CLAUDE_MD, pr["base"]["sha"])
    head_claude_md = client.file_at(CLAUDE_MD, pr["head"]["sha"])
    if is_exempt(head_branch, from_fork):
        return PullRequest(repository, head_branch, from_fork, "", [], base_claude_md, head_claude_md, [])
    data = client.graphql(PULL_REQUEST_QUERY, _repo_variables(repository, pr["number"]))
    live = data["repository"]["pullRequest"]
    return PullRequest(
        repository=repository,
        head_branch=head_branch,
        from_fork=from_fork,
        body=live["body"] or "",
        labels=[label["name"] for label in live["labels"]["nodes"]],
        base_claude_md=base_claude_md,
        head_claude_md=head_claude_md,
        issues=[
            Issue(node["repository"]["nameWithOwner"], node["number"], node["body"] or "")
            for node in live["closingIssuesReferences"]["nodes"]
        ],
    )


def run_pull_request(event: dict, client: Client, repository: str) -> int:
    if "pull_request" not in event:
        _annotate("error", "The decisions workflow checks pull_request events and re-runs on issues events; "
                  "this event has no pull request.")
        return 1
    pr = event["pull_request"]
    if not (pr["head"].get("repo") and pr["base"].get("repo")):
        _annotate("error", "The pull_request event names no head or base repository, so the check cannot tell "
                  "whether the pull request comes from a fork.")
        return 1
    verdict = evaluate(load_pull_request(event, client, repository))
    for notice in verdict.notices:
        _annotate("notice", notice)
    for error in verdict.errors:
        _annotate("error", error)
    if verdict.passed:
        print("The decisions check passed.")
        return 0
    return 1


def run_issue(event: dict, client: Client, repository: str, workflow_ref: str) -> int:
    """Re-run the check of each open pull request in this repository that closes the issue."""
    workflow = workflow_file(workflow_ref, repository)
    number = event["issue"]["number"]
    data = client.graphql(ISSUE_QUERY, _repo_variables(repository, number))
    nodes = data["repository"]["issue"]["closedByPullRequestsReferences"]["nodes"]
    pulls = [n for n in nodes if n["state"] == "OPEN" and n["repository"]["nameWithOwner"] == repository]
    if not pulls:
        _annotate("notice", f"No open pull request closes issue #{number}; nothing to re-run.")
        return 0
    for pull in pulls:
        run = client.latest_run(workflow, pull["headRefOid"])
        if run is None:
            _annotate("notice", f"Pull request #{pull['number']} has no {workflow} run for its head commit yet.")
        elif run["status"] != "completed":
            _annotate("warning", f"The {workflow} run {run['id']} of pull request #{pull['number']} is still "
                      "running and was not re-run; it may not see the edit to the issue.")
        else:
            client.rerun(run["id"])
            print(f"Re-ran {workflow} run {run['id']} of pull request #{pull['number']}.")
    return 0


def _annotate(level: str, message: str) -> None:
    print(f"::{level}::{message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The decisions check.")
    parser.add_argument("mode", choices=["pull-request", "issue"])
    args = parser.parse_args(argv)

    repository = os.environ["GITHUB_REPOSITORY"]
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
        event = json.load(handle)
    client = GitHubClient(
        token=os.environ["GITHUB_TOKEN"],
        repository=repository,
        api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        graphql_url=os.environ.get("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql"),
    )
    if args.mode == "pull-request":
        return run_pull_request(event, client, repository)
    return run_issue(event, client, repository, os.environ["CALLER_WORKFLOW_REF"])


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""The decisions check: a pull request needs an issue whose decisions are written down.

Two modes, one per event the calling workflow runs on:

  decisions.py pull-request   evaluate the pull request in GITHUB_EVENT_PATH
  decisions.py issue          re-run the check after the edit of the issue in
                              GITHUB_EVENT_PATH (README.md says which runs)

Python 3 standard library only. The rules live in plain functions over strings
so they can be tested without the GitHub API; the API sits behind GitHubClient.
"""

from __future__ import annotations

import argparse
import datetime
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
PLANNED_CHANGE = ".github/ISSUE_TEMPLATE/planned_change.yml"
# The repository the shared form, and this script, come from.
SHARED_REPOSITORY = "danielcopper/.github"
# Issue forms render each field as a level-3 heading.
ISSUE_HEADING_LEVELS = (2, 3)
# What an issue form writes for an optional field left blank.
NO_RESPONSE = "_No response_"
ADR_DIRECTORY = "docs/adr"
ADR_STATUSES = ("proposed", "accepted", "rejected", "deprecated", "superseded")
ADR_REQUIRED_KEYS = ("status", "decided", "updated")
# Each relation key with the key the other ADR declares it with.
ADR_RELATIONS = {
    "supersedes": "superseded-by",
    "superseded-by": "supersedes",
    "amends": "amended-by",
    "amended-by": "amends",
}
OPT_OUT_LABEL = "no-decisions"
OPT_OUT_BODY = re.compile(r"decisions:\s*none", re.IGNORECASE)
EXEMPT_BRANCH_PREFIXES = ("renovate/", "release-please--")

# Markdown as CommonMark reads it: headings and fences may be indented up to three spaces.
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_CLOSING_SEQUENCE = re.compile(r"(?:^|[ \t]+)#+$")
# A task-list item such as `- [ ] Which way?` or `* [x] Settled.`; group 1 is the box's mark.
_TASK_ITEM = re.compile(r"^[ \t]*[-*+][ \t]+\[([ xX])\](?:[ \t]|$)")

# An ADR file name, `0007-some-slug.md`.
_ADR_FILE = re.compile(r"^\d{4}-[^/]+\.md$")
_FRONT_MATTER_LINE = re.compile(r"^([A-Za-z0-9_-]+):[ \t]+(\S.*)$")
_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ADR_NUMBER = re.compile(r"^\d{4}$")


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


def open_questions(text: str) -> list[str]:
    """The lines of the `To decide` sections that hold an open question.

    Sections are found as in section_has_content. Inside one, a checked
    task-list item (`- [x]`, `- [X]`, also with `*` or `+`) is settled, and so
    are the indented lines that follow it. An unchecked item, and every other
    line with text, is open. Blank lines and `_No response_` are neither.
    """
    result: list[str] = []
    open_level: int | None = None
    in_item = False
    for raw, line in zip(text.splitlines(), _unfenced_lines(text)):
        heading = _heading(line)
        if heading is not None and open_level is not None and heading[0] <= open_level:
            open_level = None
        if heading is not None and open_level is None:
            if heading[0] in ISSUE_HEADING_LEVELS and heading[1] == TO_DECIDE:
                open_level = heading[0]
                in_item = False
            continue
        stripped = raw.strip()
        if open_level is None or not stripped or stripped == NO_RESPONSE:
            continue
        item = _TASK_ITEM.match(raw)
        if item:
            in_item = True
            if item.group(1) == " ":
                result.append(stripped)
        elif in_item and raw[0] in " \t":
            continue
        else:
            in_item = False
            result.append(stripped)
    return result


# --- ADRs -----------------------------------------------------------------


@dataclass
class Adr:
    """An ADR file, `docs/adr/NNNN-slug.md`, at the pull request's head commit."""

    path: str
    text: str

    @property
    def number(self) -> str:
        """The four digits the file name starts with, kept as text: `0020` stays `0020`."""
        return posixpath.basename(self.path)[:4]


# A front matter value: a bare word or an ISO date as text, or a list of ADR numbers.
FrontMatterValue = str | list[str]


@dataclass
class FrontMatter:
    """An ADR's front matter as read by parse_front_matter, and the body below it."""

    values: dict[str, FrontMatterValue]
    body: str
    errors: list[str]


def _front_matter_value(raw: str) -> FrontMatterValue | None:
    """The value of a front matter line, or None when it is none of the three kinds."""
    if raw.startswith("["):
        if not raw.endswith("]"):
            return None
        inner = raw[1:-1].strip()
        if not inner:
            return []
        entries = [entry.strip() for entry in inner.split(",")]
        return entries if all(_ADR_NUMBER.match(entry) for entry in entries) else None
    if _WORD.match(raw) or _DATE.match(raw):
        return raw
    return None


def parse_front_matter(path: str, text: str) -> FrontMatter | None:
    """Read an ADR's front matter, or return None when the ADR has none.

    The front matter is the block between a first line `---` and the next
    `---` line. Each line in it is `key: value` with an allowed key, set once;
    the value is a bare word, an ISO date or a flow list of four-digit ADR
    numbers such as `[0020, 0031]`. Anything else is an error naming the line.
    """
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return None
    closing = next((index for index in range(1, len(lines)) if lines[index].rstrip() == "---"), None)
    if closing is None:
        return FrontMatter({}, "", [f"{path} line 1: the front matter starts here but has no closing `---` line."])
    allowed = (*ADR_REQUIRED_KEYS, *ADR_RELATIONS)
    values: dict[str, FrontMatterValue] = {}
    first_line: dict[str, int] = {}
    errors: list[str] = []
    for number in range(2, closing + 1):
        line = lines[number - 1].rstrip()
        where = f"{path} line {number}"
        match = _FRONT_MATTER_LINE.match(line)
        if not match:
            found = f"`{line}`" if line.strip() else "a blank line"
            errors.append(f"{where}: front matter lines are `key: value`, found {found}.")
            continue
        key, raw = match.groups()
        value = _front_matter_value(raw)
        if key not in allowed:
            errors.append(f"{where}: `{key}` is not a front matter key. The keys are {', '.join(allowed)}.")
        elif key in first_line:
            errors.append(f"{where}: `{key}` is already set on line {first_line[key]}.")
        elif value is None:
            errors.append(
                f"{where}: `{raw}` is not a bare word, an ISO date (YYYY-MM-DD) or a list of four-digit ADR "
                "numbers such as `[0020, 0031]`."
            )
        else:
            values[key] = value
            first_line[key] = number
    return FrontMatter(values, "\n".join(lines[closing + 1:]), errors)


def _shown(value: FrontMatterValue) -> str:
    return f"[{', '.join(value)}]" if isinstance(value, list) else value


def _date(value: FrontMatterValue) -> datetime.date | None:
    if not isinstance(value, str) or not _DATE.match(value):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def front_matter_errors(adr: Adr, front: FrontMatter, numbers: set[str]) -> list[str]:
    """The rules for one ADR with front matter that parsed; numbers are all ADR numbers in the repository."""
    values = front.values
    errors = []
    missing = [key for key in ADR_REQUIRED_KEYS if key not in values]
    if missing:
        errors.append(
            f"{adr.path}: the front matter has no {', '.join(f'`{key}`' for key in missing)}. "
            "An ADR with front matter has `status`, `decided` and `updated`."
        )
    status = values.get("status")
    if status is not None and status not in ADR_STATUSES:
        errors.append(f"{adr.path}: `status` is `{_shown(status)}`; it must be one of {', '.join(ADR_STATUSES)}.")
    dates = {}
    for key in ("decided", "updated"):
        if key in values:
            date = _date(values[key])
            if date is None:
                errors.append(f"{adr.path}: `{key}` is `{_shown(values[key])}`, which is not a date (YYYY-MM-DD).")
            else:
                dates[key] = date
    if len(dates) == 2 and dates["updated"] < dates["decided"]:
        errors.append(f"{adr.path}: `updated` ({dates['updated']}) is before `decided` ({dates['decided']}).")
    for key in ADR_RELATIONS:
        value = values.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            errors.append(f"{adr.path}: `{key}` is `{value}`; it must be a list of ADR numbers such as `[0020]`.")
            continue
        if not value:
            errors.append(f"{adr.path}: `{key}` is an empty list. Name the ADRs, or remove the key.")
        for entry in value:
            if entry == adr.number:
                errors.append(f"{adr.path}: `{key}` names this ADR itself ({entry}).")
            elif entry not in numbers:
                errors.append(f"{adr.path}: `{key}` names ADR {entry}, but there is no {ADR_DIRECTORY}/{entry}-*.md.")
    if status == "superseded" and "superseded-by" not in values:
        errors.append(f"{adr.path}: `status` is `superseded`, but no `superseded-by` names the ADR that replaced it.")
    elif status != "superseded" and "superseded-by" in values:
        errors.append(f"{adr.path}: `superseded-by` is set, so `status` must be `superseded`.")
    if has_heading(front.body, "Status", levels=(2,)):
        errors.append(
            f"{adr.path}: the body has a `## Status` section. The status lives only in the front matter; "
            "remove the section."
        )
    return errors


def adr_errors(adrs: list[Adr]) -> list[str]:
    """The ADR rules over every ADR at the head commit; README.md lists them.

    An ADR without front matter is only checked when a relation points to it,
    or when it shares its number with an ADR that has front matter.
    """
    by_number: dict[str, list[Adr]] = {}
    for adr in adrs:
        by_number.setdefault(adr.number, []).append(adr)
    fronts = {adr.path: parse_front_matter(adr.path, adr.text) for adr in adrs}
    # The front matter whose relations can be followed: present and free of syntax errors.
    readable = {path: front for path, front in fronts.items() if front is not None and not front.errors}

    errors: list[str] = []
    for adr in adrs:
        front = fronts[adr.path]
        if front is not None:
            errors.extend(front.errors or front_matter_errors(adr, front, set(by_number)))

    targets = {
        entry
        for front in readable.values()
        for key in ADR_RELATIONS
        for entry in _relation(front, key)
    }
    for number, files in sorted(by_number.items()):
        if len(files) > 1 and (number in targets or any(fronts[adr.path] is not None for adr in files)):
            errors.append(
                f"ADR number {number} is used by more than one file: {', '.join(adr.path for adr in files)}. "
                "Give each ADR its own number."
            )

    for adr in adrs:
        front = readable.get(adr.path)
        if front is None:
            continue
        for key, inverse in ADR_RELATIONS.items():
            for entry in _relation(front, key):
                others = by_number.get(entry, [])
                # A missing, duplicate or self reference is reported above.
                if entry == adr.number or len(others) != 1:
                    continue
                other = others[0]
                other_front = fronts[other.path]
                if other_front is None:
                    errors.append(
                        f"{adr.path} names ADR {entry} under `{key}`, but {other.path} has no front matter. "
                        f"Add front matter to it with `{inverse}: [{adr.number}]`, so the relation stands on both ends."
                    )
                elif other.path in readable and adr.number not in _relation(other_front, inverse):
                    errors.append(
                        f"{adr.path} names ADR {entry} under `{key}`, but {other.path} does not name ADR "
                        f"{adr.number} under `{inverse}`. Add it there, so the relation stands on both ends."
                    )
    return errors


def _relation(front: FrontMatter, key: str) -> list[str]:
    """The ADR numbers a relation key lists; empty when it is absent or not a list."""
    value = front.values.get(key)
    return value if isinstance(value, list) else []


# --- Rules ----------------------------------------------------------------


@dataclass
class Issue:
    repository: str
    number: int
    body: str


@dataclass
class SharedForm:
    """The shared Planned change form, as checked out with this script."""

    text: str
    commit: str


@dataclass
class PullRequest:
    repository: str
    head_branch: str
    from_fork: bool
    body: str
    labels: list[str]
    base_claude_md: str | None
    head_claude_md: str | None
    head_planned_change: str | None
    shared_planned_change: SharedForm
    adrs: list[Adr]
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


def planned_change_error(head: str | None, shared: SharedForm) -> str | None:
    """The copy rule: a repository's own Planned change form must equal the shared one byte for byte."""
    if head is None or head == shared.text:
        return None
    return (
        f"{PLANNED_CHANGE} differs from the shared form at {SHARED_REPOSITORY}@{shared.commit[:7]}. "
        "Copy the shared file over it unchanged."
    )


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
    questions = open_questions(issue.body)
    if questions:
        listed = ", ".join(f'"{question}"' for question in questions)
        errors.append(
            f"Issue {name} still has a `## {TO_DECIDE}` section with open questions: {listed}. "
            f"Answer each under `## {DECISIONS}` and check it off (`- [x]`)."
        )
    return errors


def evaluate(pr: PullRequest) -> Verdict:
    """Apply the rules to a pull request, in order."""
    verdict = Verdict()
    guard = claude_md_error(pr.base_claude_md, pr.head_claude_md)
    if guard:
        verdict.errors.append(guard)
    copy = planned_change_error(pr.head_planned_change, pr.shared_planned_change)
    if copy:
        verdict.errors.append(copy)
    verdict.errors.extend(adr_errors(pr.adrs))

    if is_exempt(pr.head_branch, pr.from_fork):
        verdict.notices.append(
            f"Branch {pr.head_branch} is exempt from the linked-issue rules; only the {CLAUDE_MD} section, "
            "the Planned change copy and the ADRs are checked."
        )
        return verdict

    if is_opted_out(pr.labels, pr.body):
        verdict.notices.append(
            "Opted out of the linked-issue rules (`decisions: none` or the "
            f"`{OPT_OUT_LABEL}` label); only the {CLAUDE_MD} section, the Planned change copy and the ADRs are "
            "checked."
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

    def list_dir(self, path: str, ref: str) -> list[str] | None: ...

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

    def list_dir(self, path: str, ref: str) -> list[str] | None:
        """The names of the files directly in the directory at path, or None when there is no directory."""
        url = f"{self._api_url}/repos/{self.repository}/contents/{path}?ref={ref}"
        try:
            entries = json.loads(self._request("GET", url))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        # For a file the API returns one object instead of a list.
        if not isinstance(entries, list):
            return None
        return [entry["name"] for entry in entries if entry["type"] == "file"]

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


def load_adrs(client: Client, ref: str) -> list[Adr]:
    """The ADRs at ref: the files named `NNNN-slug.md` directly in docs/adr/. Other files there are not read."""
    adrs = []
    for name in sorted(client.list_dir(ADR_DIRECTORY, ref) or []):
        if not _ADR_FILE.match(name):
            continue
        path = f"{ADR_DIRECTORY}/{name}"
        text = client.file_at(path, ref)
        if text is None:
            raise RuntimeError(f"{path} is listed at {ref} but could not be read.")
        adrs.append(Adr(path, text))
    return adrs


def load_pull_request(event: dict, client: Client, repository: str, shared: SharedForm) -> PullRequest:
    """Build the PullRequest to judge.

    The commits come from the event, so the verdict belongs to the commit the
    run reports on. Description, labels and linked issues are read live: a
    re-run replays the original event, but the issue may have changed since.
    """
    pr = event["pull_request"]
    head_branch = pr["head"]["ref"]
    head_repo = pr["head"]["repo"]
    # GitHub sends a null head repository once the fork is deleted; it is still a fork.
    from_fork = head_repo is None or head_repo["full_name"] != pr["base"]["repo"]["full_name"]
    base_claude_md = client.file_at(CLAUDE_MD, pr["base"]["sha"])
    head_claude_md = client.file_at(CLAUDE_MD, pr["head"]["sha"])
    head_planned_change = client.file_at(PLANNED_CHANGE, pr["head"]["sha"])
    adrs = load_adrs(client, pr["head"]["sha"])
    if is_exempt(head_branch, from_fork):
        return PullRequest(
            repository, head_branch, from_fork, "", [], base_claude_md, head_claude_md, head_planned_change, shared,
            adrs, [],
        )
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
        head_planned_change=head_planned_change,
        shared_planned_change=shared,
        adrs=adrs,
        issues=[
            Issue(node["repository"]["nameWithOwner"], node["number"], node["body"] or "")
            for node in live["closingIssuesReferences"]["nodes"]
        ],
    )


def read_shared_form(path: str, commit: str) -> SharedForm:
    """Read the shared form from the checkout as it is on disk, line endings included."""
    with open(path, encoding="utf-8", newline="") as handle:
        return SharedForm(handle.read(), commit)


def run_pull_request(event: dict, client: Client, repository: str, shared: SharedForm) -> int:
    if "pull_request" not in event:
        _annotate("error", "The decisions workflow checks pull_request events and re-runs on issues events; "
                  "this event has no pull request.")
        return 1
    pr = event["pull_request"]
    if not pr["base"].get("repo"):
        _annotate("error", "The pull_request event names no base repository, so the check cannot tell whether the "
                  "pull request comes from a fork.")
        return 1
    verdict = evaluate(load_pull_request(event, client, repository, shared))
    for notice in verdict.notices:
        _annotate("notice", notice)
    for error in verdict.errors:
        _annotate("error", error)
    if verdict.passed:
        print("The decisions check passed.")
        return 0
    return 1


def run_issue(event: dict, client: Client, repository: str, workflow_ref: str) -> int:
    """Re-run the check after an edit of the issue; README.md says which runs."""
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
        shared = read_shared_form(os.environ["SHARED_FORM"], os.environ["SHARED_FORM_COMMIT"])
        return run_pull_request(event, client, repository, shared)
    return run_issue(event, client, repository, os.environ["CALLER_WORKFLOW_REF"])


if __name__ == "__main__":
    sys.exit(main())

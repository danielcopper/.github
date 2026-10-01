# .github

Shared files for my repositories: default community health files, and the `decisions` check as a reusable workflow.

## Default community files

GitHub uses these for every repository of this account that has no copy of its own:

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how an issue becomes a pull request.
- [`.github/ISSUE_TEMPLATE/planned_change.yml`](.github/ISSUE_TEMPLATE/planned_change.yml) — the **Planned change**
  issue form: Today, Wanted, To decide, Done when, Out of scope.
- [`.github/pull_request_template.md`](.github/pull_request_template.md) — a prose summary, the final decisions, and
  `Closes #`.

A repository with its own `.github/ISSUE_TEMPLATE/` folder gets none of the shared issue templates, so it carries a
copy of Planned change; rule 2 of the `decisions` check keeps that copy identical.

## The `decisions` check

An issue's open questions sit under `## To decide`, one checkbox each. Once a question is answered under `## Decisions`,
it is checked off. A pull request must link an issue whose decisions are written down and whose questions are all
checked off.

[`.github/workflows/decisions.yml`](.github/workflows/decisions.yml) enforces this. On a pull request it checks, in
order:

1. If `CLAUDE.md` on the base commit has the heading `## Where decisions live`, the pull request must keep it.
   Repositories without the section are unaffected. This rule applies to every pull request, including the exempt
   branches and the opt-outs below.
2. If the head commit has `.github/ISSUE_TEMPLATE/planned_change.yml`, it must be byte-identical with the shared form
   at the commit the caller pinned (see [Default community files](#default-community-files)). Repositories without
   the file are unaffected. Like rule 1, it applies to every pull request, so a bot's bump of the pin shows an
   outdated copy.
3. Every ADR at the head commit that has front matter must follow the [ADR format](#adrs): `status`, `decided` and
   `updated` are set; `status` is `proposed`, `accepted`, `rejected`, `deprecated` or `superseded`, and it is
   `superseded` exactly when `superseded-by` is set; the dates are real dates and `updated` is not before `decided`;
   each relation lists other ADRs that exist; and the body has no `## Status` section. A relation is declared on both
   ends, and no ADR number is used twice. Repositories without `docs/adr/` are unaffected. Like rules 1 and 2, it
   applies to every pull request.
4. A head branch starting with `renovate/` or `release-please--` in the pull request's own repository skips the rules
   below. A branch from a fork is never exempt, whatever its name, even after the fork is deleted.
5. With an opt-out, the rules below are skipped.
6. The pull request must link at least one issue (`Closes #N`, `Fixes #N`, `Resolves #N`, or a link set in the
   sidebar). Closing keywords only link an issue when the pull request targets the default branch.
7. Every linked issue needs a `## Decisions` section with some text in it. A pointer such as "See epic #1896." is
   enough.
8. No linked issue may have an open question: under `## To decide`, every item is a checked task-list item (`- [x]`);
   an unchecked item or any other text there counts as open. An empty section holds no open questions.

`_No response_`, which an issue form writes for a field left blank, does not count as text.

Under `## To decide`, a list item may start with `-`, `*` or `+`, `[X]` counts as checked like `[x]`, and an indented
line under a checked item, such as `  → D1`, belongs to that item.

In an issue, `## Decisions` and `## To decide` count at level 2 or level 3 (`### Decisions`, the level an issue form
writes), with exactly that title and outside fenced code blocks. A section ends at the next heading with the same
number of `#` or fewer, so a `### D1: …` heading under `## Decisions` is part of it. The `CLAUDE.md` section counts
only as the level-2 heading `## Where decisions live`.

When a linked issue is edited, the check runs again for each open pull request in the same repository that closes
it. A run that is still in progress is left alone, with a warning.

The logic is in [`scripts/decisions.py`](scripts/decisions.py) (Python 3 standard library only).

### ADRs

An ADR is a file `docs/adr/NNNN-slug.md`, numbered with four digits; other files in `docs/adr/` are not read. Its front
matter is the block between a first line `---` and the next `---` line:

```markdown
---
status: superseded
decided: 2026-03-02
updated: 2026-09-14
superseded-by: [0031]
amends: [0012]
---

# Use one queue
```

Each line of the block is `key: value`, and each key appears once. The keys are `status`, `decided` and `updated`,
plus the relations `supersedes`, `superseded-by`, `amends` and `amended-by`. A value is a bare word, a date written
`YYYY-MM-DD`, or a list of four-digit ADR numbers such as `[0020, 0031]`; the numbers stay text, so `0020` is ADR 0020.
Anything else in the block (another key, quotes, a comment, a blank line, a missing closing `---`) is an error naming
the line, and the other rules for that ADR wait until it is fixed.

A relation stands on both ends: when 0031 has `supersedes: [0020]`, 0020 has `superseded-by: [0031]`, and likewise
`amends` with `amended-by`.

An ADR without front matter is not checked. Two exceptions: when a relation points to it, it needs front matter, so the
relation can stand on both ends; and when it shares its number with another ADR, the number counts as used twice if
one of those files has front matter or a relation points to that number.

### Opting out

For a small fix without decisions, either:

- write `decisions: none` anywhere in the pull request description (any case, any spacing after the colon), or
- add the label `no-decisions`.

### Calling it

Add a workflow to the repository, for example `.github/workflows/decisions.yml`:

```yaml
name: decisions

on:
  pull_request:
    types: [opened, synchronize, reopened, edited, labeled, unlabeled]
  issues:
    types: [edited]

permissions: {}

jobs:
  decisions:
    permissions:
      actions: write        # re-run pull request checks after an issue edit
      contents: read        # read CLAUDE.md, the Planned change copy and the ADRs
      issues: read          # read the linked issues
      pull-requests: read   # read the pull request, its labels and linked issues
    uses: danielcopper/.github/.github/workflows/decisions.yml@<full-commit-sha>
```

- Pin `<full-commit-sha>` to a full commit SHA of this repository, not a branch or tag. The checker script and the
  shared Planned change form are checked out at that same commit.
- A called workflow can only reduce the token permissions its caller grants, so the caller grants all four. Each
  job of the reusable workflow keeps only what it needs; `actions: write` is used only on issue edits.
- Issue events run the workflow file from the default branch, so re-runs work once the caller is merged there.
- Pull requests from forks get a read-only token, which is all the check needs. The re-run after an issue edit is
  requested by the issue event's run, with a token of the base repository.
- To make the check required, add it to the branch's ruleset. With the caller above, it appears as
  `decisions / decisions`.

## Development

```sh
python3 -m unittest discover -s tests
```

The tests also run in CI on every pull request and on pushes to `main`.

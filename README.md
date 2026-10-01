# .github

Shared files for my repositories: default community health files, and the `decisions` check as a reusable workflow.

## Default community files

GitHub uses these for every repository of this account that has no copy of its own:

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how an issue becomes a pull request.
- [`.github/ISSUE_TEMPLATE/planned_change.yml`](.github/ISSUE_TEMPLATE/planned_change.yml) — the **Planned change**
  issue form: Today, Wanted, To decide, Done when, Out of scope.
- [`.github/pull_request_template.md`](.github/pull_request_template.md) — a prose summary, the final decisions, and
  `Closes #`.

A repository with its own `.github/ISSUE_TEMPLATE/` folder uses none of the default issue templates, so it needs its
own copy of Planned change.

## The `decisions` check

An issue's open questions sit under `## To decide`. Once they are answered, the section becomes `## Decisions`. A pull
request must link an issue whose decisions are written down.

[`.github/workflows/decisions.yml`](.github/workflows/decisions.yml) enforces this. On a pull request it checks, in
order:

1. If `CLAUDE.md` on the base commit has the heading `## Where decisions live`, the pull request must keep it.
   Repositories without the section are unaffected. This rule applies to every pull request, including the exempt
   branches and the opt-outs below.
2. A head branch starting with `renovate/` or `release-please--` in the pull request's own repository skips the rules
   below. A branch from a fork is never exempt, whatever its name, even after the fork is deleted.
3. With an opt-out, the rules below are skipped.
4. The pull request must link at least one issue (`Closes #N`, `Fixes #N`, `Resolves #N`, or a link set in the
   sidebar). Closing keywords only link an issue when the pull request targets the default branch.
5. Every linked issue needs a `## Decisions` section with some text in it. A pointer such as "See epic #1896." is
   enough.
6. No linked issue may still have a `## To decide` section with text in it. An empty one holds no open questions.

`_No response_`, which an issue form writes for a field left blank, does not count as text.

In an issue, `## Decisions` and `## To decide` count at level 2 or level 3 (`### Decisions`, the level an issue form
writes), with exactly that title and outside fenced code blocks. A section ends at the next heading with the same
number of `#` or fewer, so a `### D1: …` heading under `## Decisions` is part of it. The `CLAUDE.md` section counts
only as the level-2 heading `## Where decisions live`.

When a linked issue is edited, the check runs again for each open pull request in the same repository that closes
it. A run that is still in progress is left alone, with a warning.

The logic is in [`scripts/decisions.py`](scripts/decisions.py) (Python 3 standard library only).

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
      contents: read        # read CLAUDE.md
      issues: read          # read the linked issues
      pull-requests: read   # read the pull request, its labels and linked issues
    uses: danielcopper/.github/.github/workflows/decisions.yml@<full-commit-sha>
```

- Pin `<full-commit-sha>` to a full commit SHA of this repository, not a branch or tag. The checker script is
  checked out at that same commit.
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

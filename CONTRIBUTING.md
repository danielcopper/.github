# Contributing

Thanks for your interest. These repositories are maintained by one person, so the process is small but fixed.

## Before you write code

1. Open an issue first. For a change, use the **Planned change** template. Typo fixes and dependency bumps don't
   need an issue.
2. If the change needs decisions — open questions you list under `## To decide`, or ones I raise — I write them in an
   issue of my own and link it to your pull request; it merges once every question there is answered. Work started
   before a question is answered may need to change.

## The pull request

- Link the issue (`Closes #N`).
- Fill in the PR template: a prose summary (it becomes the squash commit body) and the final decisions. A test
  should be seen failing before the change makes it pass.
- Update the documentation in the same PR when behavior changes.
- A small fix without decisions: write `decisions: none` in the PR description.

## Review and merge

I review every pull request and merge it myself. Some changes need a manual check on real hardware before they can be
merged; I'll say so on the pull request.

Setup, tests, and local checks are described in each repository's README or `docs/`.

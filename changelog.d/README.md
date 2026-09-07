# Changelog fragments

One file per pull request, named after its ticket (`DAH-1234.md`; `pr-123.md` when there is no ticket). It holds the
`CHANGELOG.md` lines the PR would otherwise add under `## [Unreleased]`: one or more `### Added` / `### Changed` /
`### Fixed` / `### Removed` headings, each followed by bullets. Example:

```markdown
### Fixed
- `lium up` no longer retries the rent POST blindly; one `lium up` could create two pods.
```

Why: every PR inserting at the same line of `CHANGELOG.md` conflicts with every other one, so no two PRs could be
merged in sequence without a rebase. Separate files cannot conflict.

At release time `python scripts/changelog.py --version X.Y.Z` folds every fragment into `CHANGELOG.md` under a new
`## [X.Y.Z] - YYYY-MM-DD` heading (same-named sections merged, files deleted). `--dry-run` prints the result.

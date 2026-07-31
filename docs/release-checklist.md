# Release checklist: 0.2.0

## Scope and evidence

- [x] Start from qualified commit `d3bcadd47aacf522e842c076b7915de330bfe289`.
- [x] Preserve completed campaign, candidate, prepared campaigns, historical logs,
  gold, and protected fixtures.
- [x] Record 5/5 functional and 0/5 exact identity in safe tracked metadata.
- [x] Mark packaged Bubblewrap evaluation as experimental/non-authoritative.
- [x] Add direct replay with synthetic-only tests, including mode `0400` overlay.

## Package and documentation

- [x] Set package version 0.2.0 and complete metadata/entry points.
- [x] Include installed synthetic example resources.
- [x] Rewrite README and add focused public documentation.
- [x] Update changelog, release notes, and evaluator migration note.
- [x] Verify source-distribution and wheel contents contain no private/runtime data.

## Validation

- [x] Final focused release tests pass (268 tests).
- [x] Ruff passes.
- [x] mypy passes (46 source files, strict mode).
- [x] Complete suite ran once: 1097 passed, one expected skip, and one stale
  version-literal assertion failed; the assertion was repaired and its targeted rerun
  passed.
- [x] Wheel and source distribution build.
- [x] Clean temporary-environment wheel install/import/entry-point smoke passes.
- [x] Installed synthetic quick start passes.
- [x] Installed direct replay passes against freshly generated synthetic inputs.
- [x] Documentation links/references pass a practical check.
- [x] One bounded repair pass closed all three independent final-audit findings:
  source-repository output isolation, toolchain-independent experimental CLI help,
  and packaged MIT license text.

## Git and artifacts

- [x] Generate local wheel/source sizes and SHA-256 values for the final report.
- [ ] Commit tracked release-closure changes.
- [ ] Push `campaign-offline-evaluation-split`.
- [ ] Fast-forward `origin/main` only when ancestry permits without merge/rewrite.
- [x] Do not publish to PyPI or create/push a release tag.

## Local artifacts

Record final artifact paths, sizes, and checksums in the operator's release report.
They are intentionally not embedded here because this checklist is itself part of the
source distribution.

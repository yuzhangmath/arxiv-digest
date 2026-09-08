# Changelog

This file records notable user-visible changes to arXiv Digest.

## Contents

- [0.3.0](#030---2026-09-08)
- [0.2.1](#021---2026-08-25)

## [0.3.0] - 2026-09-08

### Added

- Version 0.3.0 is a manual-bootstrap release for updater protocol 1. Later
  compatible releases can update verified pipx 1.16.7 installations with one
  click, retaining the interpreter and unchanged dependencies.
- Verified wheel downloads, complete environment snapshots, private portable
  data backups, protected installation provenance, atomic journal receipts, and
  a copied recovery helper support offline installation and snapshot recovery.
- Update preparation drains admitted work and rejects new work across tabs.
  The browser renders restart guidance before acknowledging handoff and shows
  the durable result in the fresh dashboard.
- A fixed recovery wrapper and recovery-aware managed launchers support an
  interrupted update; explicit guarded recovery refuses unexpected live state.

- `--copy-url` for dashboard commands copies the current session URL without
  opening a browser, including to the Windows clipboard from WSL.
- Dashboard startup in WSL tries Windows browser helpers and prints a clear
  URL fallback when opening fails.
- Dashboard startup checks published GitHub releases in the background and
  shows a link to the newest release when the installed version is outdated.

### Fixed

- `doctor` reports pending or blocked recovery without running recovery,
  opening application data, or exposing local paths, including when recovery
  directories or locks are damaged.
- Update notices consume the current discovery results, wait through the full
  check deadline, and offer manual guidance when the check is inconclusive.
  Returning to a suspended tab preserves the observation deadline and ignores
  stale responses.
- Safe preparation cleanup resumes only ordinary bounded synchronization.
  Canceled PDF and corpus jobs remain visible for manual retry.

### Changed

- Publication requires successful macOS and Linux validation on Python 3.11
  and the current Python release against the same built candidate.
- Similarity explanations name and link the matching seed or saved paper.
- Library cards include arXiv links and explain the default order by original
  submission date and the relevance-first order used for search results.
- The installation guide includes backup, upgrade, verification, and launcher
  steps for existing 0.2.x users, plus guidance for virtual environments and
  incompatible older data generations.

## [0.2.1] - 2026-08-25

Full technical-beta notes are available in the
[v0.2.1 release notes](docs/releases/v0.2.1.md).

### Changed

- Synchronization now distinguishes daily-list recovery from later metadata
  enrichment and reports each phase clearly.
- Review page controls now follow the paper list. **Finish date** appears only
  on the final page and advances to the first page of the next later unreviewed
  date.
- Paper cards use simpler public-facing version and event labels, show all arXiv
  subjects, and consistently use the latest known version when an announcement
  version is unresolved.
- Calendar entries show explicit **Reviewed**, **Partial**, or **Unreviewed**
  states with accessible light- and dark-theme outlines.
- Closing the only browser tab now normally allows the local application to
  stop after about three minutes, or up to about 4.5 minutes without a browser
  disconnect notice, instead of about 30 minutes.
- Settings and `arxiv-digest doctor` no longer expose internal canonical-event
  resolution counters.

### Fixed

- Review PDF buttons now remain pending until the server-side download finishes
  and accurately report completion or a retryable failure.
- Finishing a date no longer sends the user backward to an older unfinished
  date or resumes a later date at a saved middle page.

### Release engineering

- Package, runtime, artifact, and tag identity now derive from one application
  version.
- Tagged prereleases are verified with tests, privacy scans, an isolated pipx
  smoke test, and SHA-256 checksums before publication.

[0.2.1]: https://github.com/yuzhangmath/arxiv-digest/compare/v0.2.0...v0.2.1

[0.3.0]: https://github.com/yuzhangmath/arxiv-digest/compare/v0.2.1...v0.3.0

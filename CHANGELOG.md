# Changelog

This file records notable user-visible changes to arXiv Digest.

## Contents

- [Unreleased](#unreleased)
- [0.4.0](#040---2026-10-02)
- [0.3.1](#031---2026-09-15)
- [0.3.0](#030---2026-09-08)
- [0.2.1](#021---2026-08-25)

## Unreleased

No changes yet.

## [0.4.0] - 2026-10-02

See the [v0.4.0 release notes](docs/releases/v0.4.0.md) for installation and
generation-2 upgrade guidance.

### Added

- Native Windows support, with local application data, Windows file permissions
  and locking, browser and clipboard integration, a native PDF-folder picker,
  and an optional desktop launcher. Windows, macOS, and Linux share the tagged
  pipx installation route.
- Windows CI and release validation alongside macOS and Linux, covering Python,
  Chromium, WebKit, and installation of the wheel through pipx.
- Settings offers retries for individual failed category/date daily lists.
  Calendar shows **Retrieval failed** for dates without confirmed papers and
  **Some retrievals failed** when confirmed papers remain available.
- Paced system-`curl` fallbacks for plain HTTP 406 from daily-list retrieval,
  OAI metadata synchronization, and abstract lookups. Daily lists also retain
  a compatibility attempt without inline abstracts.
- Persistent pauses after HTTP 429 or explicit arXiv rate-limit responses,
  using the supplied retry time or a one-hour fallback. Pauses survive restarts
  while saved Review and Library papers remain available.

### Changed

- Daily-list dates become eligible at 20:00 America/New_York, consistently
  across retrieval, Review, Calendar, and coverage counts.
- Confirmed dates remain reviewable and finishable when abstracts are missing.
  Retrying daily-list retrieval preserves abstracts already stored locally.
- New PDF downloads use all authors' surnames followed by the title, without an
  arXiv ID or version prefix. Existing filenames are preserved, and conflicting
  new filenames receive a numbered suffix. Filename associations survive folder
  changes and portable restores while missing PDFs remain marked absent.
- **Retry missing abstracts** now runs on a confirmed Review date, including
  previously reviewed papers. Each recovered abstract is saved immediately,
  and reading, saving, and finishing the date remain available during retries.
- Updates are now manual. The dashboard retains a passive new-release notice
  and a link to update instructions.
- Removed automatic installation, environment snapshots, updater recovery,
  browser restart handoffs, and updater-specific release manifests and tests.
- Simplified startup, shutdown, backup integration, and release verification
  while preserving the daily review, Library, PDF, and portable backup workflows.
- First-run setup now asks for categories, coverage, a tested PDF folder,
  profile confirmation, and an optional launcher. Seed papers, terms, authors,
  and the optional recent-paper sample are available in Interests after setup.
- **Refresh suggestions** in Interests builds or resumes the bounded 90-day
  sample. Explicit selections still take effect only after **Update interests**.
- Settings retains browser backup export and gives terminal restore instructions:
  quit the app, wait for it to stop, then run `arxiv-digest import BACKUP.zip`.
  Archive validation, PDF destination selection, and verified pre-restore
  recovery backups remain in place.
- Status and Settings share their coverage calculation. A date with a failed
  category and a pending category counts as failed in both, while category
  details retain the pending work.

### Fixed

- Navigating between dashboard views prevents late responses and scheduled
  refreshes from replacing the newly opened view. PDF completion updates remain
  attached to the view that started them.

## [0.3.1] - 2026-09-15

### Added

- **Retry missing abstracts** on the Review starting page retries metadata for
  papers with blank abstracts across unreviewed dates. It shows progress and
  leaves remaining failures retryable while review stays available.

### Changed

- Library orders saved papers by latest arXiv version date, newest first.
  Searches keep relevance first and use latest-version recency to break ties.
- The release policy enables one-click updates from eligible 0.3.0
  installations while retaining updater protocol 1, application-data generation
  2, and the prerelease channel.

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

[0.3.1]: https://github.com/yuzhangmath/arxiv-digest/compare/v0.3.0...v0.3.1

[0.4.0]: https://github.com/yuzhangmath/arxiv-digest/compare/v0.3.1...v0.4.0

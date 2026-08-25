# Changelog

This file records notable user-visible changes to arXiv Digest.

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

# arXiv Digest

```bash
pipx install git+https://github.com/yuzhangmath/arxiv-digest.git
arxiv-digest init
```

arXiv Digest is a local, explainable review queue for arXiv. It runs on macOS
and Linux, keeps its durable state on your computer, and opens a loopback-only
dashboard in your browser.

## Contents

- [Daily use](#daily-use)
- [First-run setup](#first-run-setup)
- [Review and history](#review-and-history)
- [PDFs and the desktop launcher](#pdfs-and-the-desktop-launcher)
- [Privacy](#privacy)
- [Maintenance](#maintenance)
- [Documentation](#documentation)
- [License](#license)

## Daily use

After setup, run `arxiv-digest` from any directory. The dashboard opens to the
oldest unfinished review date. Each page contains at most 20 paper cards; date
navigation and paging do not change your interests. Use **Finish** explicitly
when you are done with a date.

Cards label evidence as **current**, **recovered**, or **inferred**. Current and
recovered dates come from exact announcement evidence. Inferred dates are a
best reconstruction from durable metadata and may not equal the original
mailing date.

## First-run setup

Run `arxiv-digest init`. Choose categories, review candidate papers, add any
custom keywords, phrases, authors, or seed paper IDs, choose a PDF destination,
and confirm the summary. Only checked suggestions and entries you explicitly
type become preferences. Searching, navigating, or paging never selects an
interest. No preferences are bundled with the application.

## Review and history

The OAI-PMH synchronization checkpoints are durable. If the application misses
days, it resumes and recovers metadata rather than silently treating the next
run as complete. Exact short-gap enrichment is separate from the historical
coverage backfill. When exact announcement evidence cannot be recovered,
inferred history is visibly labeled and its limits remain visible.

The HTML catch-up source is best effort: an upstream markup change or temporary
failure can leave exact-date holes while OAI metadata remains synchronized.

## PDFs and the desktop launcher

Settings offers standard PDF folders and a native folder picker; the dashboard
does not accept a typed filesystem path. Downloaded PDFs stay outside backups.

A desktop launcher is optional. Setup can create it, Settings can retry,
recreate, or remove it, and `arxiv-digest install-launcher` provides the same
explicit action. The launcher only starts the app when you open it. arXiv Digest
does not install scheduled or background work.

## Privacy

There is no telemetry, cloud account, or bundled personal collection.
There is no `.eml` import. Paper metadata, interests, review progress, and the library
remain in the platform data directories described in
[Data and backup](docs/data-and-backup.md).

`arxiv-digest doctor` prints aggregate, redacted diagnostics. You can paste that
output into ChatGPT when asking for troubleshooting help; do not supplement it
with private paper titles, paths, or account details.

## Maintenance

Use `arxiv-digest export BACKUP.zip` for a portable backup and inspect a backup
in Settings before confirming import. Cache deletion is safe: it does not erase
the profile, synchronization checkpoints, review progress, or saved library.
See [Installation](docs/installation.md) for upgrades and uninstalling, and
[Troubleshooting](docs/troubleshooting.md) for offline and recovery guidance.

## Documentation

- [Installation and first launch](docs/installation.md)
- [Data locations, cache, and backup](docs/data-and-backup.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

Friends should install over the public HTTPS URL shown above. Maintainers may
use the separate SSH remote
`git@github.com:yuzhangmath/arxiv-digest.git`; it is not an end-user install
address.

## License

arXiv Digest is released under the [MIT License](LICENSE). Vendored dependency
licenses are listed in [Third-Party Notices](THIRD_PARTY_NOTICES.md).

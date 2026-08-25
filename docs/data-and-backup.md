# Data and backup

arXiv Digest separates durable state, regenerable cache files, downloaded
PDFs, and portable backups.

## Contents

- [What is durable](#what-is-durable)
- [Privacy and encryption](#privacy-and-encryption)
- [Review, Calendar, and coverage](#review-calendar-and-coverage)
- [macOS locations](#macos-locations)
- [Linux locations](#linux-locations)
- [Cache safety](#cache-safety)
- [PDF destinations](#pdf-destinations)
- [Export a backup](#export-a-backup)
- [Inspect and import](#inspect-and-import)
- [What a portable backup excludes](#what-a-portable-backup-excludes)

## What is durable

The profile stores categories, keywords, phrases, authors, seed papers, the
chosen PDF folder reference, and the daily-list coverage start for each
category. The SQLite database stores article metadata, source observations,
daily-list coverage status, canonical announcement events, review progress,
and the saved Library. These are durable even when the dashboard is offline.

Guided setup groups keywords and phrases under **Terms**, but the durable
profile keeps them as separate fields. Interests presents both together as
**Terms** while preserving that classification when the profile is updated.

**Refresh suggestions** resumes or reuses the bounded 90-day paper sample and
recalculates suggestions from the last updated profile. Persisted interests are
excluded and each response is deduplicated, but a suggestion that was merely
shown—or selected only in the unsaved draft—may appear again. Refreshing does
not change the profile; changes take effect only after **Update interests**.

## Privacy and encryption

Ranking, interest matching, and Library searches run locally, and the
application does not send telemetry. It does make HTTPS requests to arXiv
services to build the candidate sample, recover daily lists, synchronize paper
metadata, look up custom papers, and download PDFs. These requests disclose
selected arXiv categories and requested date windows. A custom paper lookup or
PDF download discloses the exact arXiv identifier; opening an arXiv link sends
that identifier from the browser. arXiv also receives ordinary connection
details such as the connecting network address and request time, plus a
User-Agent containing the application version and project URL. Network
operators can ordinarily see the arXiv host and connection metadata, while
HTTPS protects the request contents in transit.

Profiles, SQLite databases, downloaded PDFs, and portable backups are private
local files, but arXiv Digest does not cryptographically encrypt them. A
portable backup is a ZIP archive, not an encrypted vault. Use device or volume
encryption where needed, restrict access to backup copies, and use a trusted
channel when transferring them.

## Review, Calendar, and coverage

Review and Calendar require recovered daily-list membership. Atom and OAI are
hidden support for metadata and version resolution; neither source can place a
paper on a visible date without recovered daily-list membership. Failed,
pending, or unavailable recovery leaves coverage gaps. Settings preserves and
reports those gaps instead of filling them with another date source.

The setup candidate corpus does not populate Review, Calendar, or Library, and
selecting a seed does not save it. Library is independent of category coverage:
saving a paper does not add a review event, and removing a category does not
remove its saved papers or separately downloaded PDFs.

## macOS locations

- Configuration and durable data: `~/Library/Application Support/arxiv-digest`
- Regenerable cache: `~/Library/Caches/arxiv-digest`
- Backups: the `backups` folder inside the durable data directory

The active PDF destination is the folder you confirmed in setup or Settings.
The native picker is the normal path; app-managed Downloads and Documents
fallbacks appear only on systems where no native folder picker is available.

## Linux locations

- Configuration: `${XDG_CONFIG_HOME:-~/.config}/arxiv-digest`
- Durable data: `${XDG_DATA_HOME:-~/.local/share}/arxiv-digest`
- Regenerable cache: `${XDG_CACHE_HOME:-~/.cache}/arxiv-digest`
- Backups: the `backups` folder inside the durable data directory

Relative XDG overrides are ignored in favor of the platform fallback.

## Cache safety

Deleting cache files is safe. The app can fetch or recompute them. Cache
deletion does not remove the profile, synchronization checkpoints, review
progress, saved papers, or downloaded PDFs. A temporarily empty cache does not
mean durable history was lost.

## PDF destinations

First-run setup and Settings normally use the native folder picker. If the
picker is unavailable, the dashboard offers app-managed Downloads and
Documents fallbacks so setup and restore remain possible. The setup review
shows the chosen destination as a
read-only path and shortens the current home directory to `~`; destinations
outside the home directory remain absolute. The dashboard does not accept a
typed path or trust a path sent by a browser request. Folder testing and **Open
folder** use only the active, server-validated destination. Backups do not
contain PDF bytes or an absolute machine-local destination.

## Export a backup

From Settings, choose **Export backup** and save the browser download. To use
the command line instead, select **Quit** in the dashboard, open a terminal,
and choose a new filename:

```bash
arxiv-digest export arxiv-digest-backup.zip
```

Export refuses to overwrite an existing file. Portable backup format 2 records
application-data generation 2, profile schema 2, and record schema 2 in its
manifest. It contains exact per-category coverage, portable article metadata
and version history, source observations, daily-list status, canonical events
and observation links, review-date state, saved Library papers, and portable
download-file metadata. Keep it as private as the interests and Library it
contains.

## Inspect and import

Settings inspects an uploaded backup without changing state. Review the
summary, reconfirm the local PDF destination, and confirm creation of a
pre-restore recovery backup before restore. To use the terminal instead,
select **Quit** in the dashboard, open a terminal, and run:

```bash
arxiv-digest import arxiv-digest-backup.zip
```

The command validates the backup format and generation before changing local
state, asks you to choose the restored PDF destination, and then revalidates
the archive immediately before restore. Format-1 or generation-1 backups are
reported as unsupported and are not imported. A valid restore builds a fresh
schema-version-4, generation-2 database. Before replacing nonempty
generation-2 state, the app creates and verifies a private pre-restore recovery
archive; failed inspection or validation leaves the current state unchanged.

Download-file records never carry PDF bytes. Restore registers only the exact
named file when it already exists in the newly selected destination and its PDF
signature, size, and checksum all match the record. Missing or nonmatching
files are skipped; restore does not scan unrelated files or invent downloaded
state.

## What a portable backup excludes

Portable backups exclude caches and raw responses, downloaded PDFs, runtime
tokens and locks, launcher files and launcher error details, machine-specific
absolute PDF paths, and transient setup drafts. Restore uses a newly confirmed
destination on the receiving computer. The portable content still includes
personal interests and library state, so do not publish a backup.

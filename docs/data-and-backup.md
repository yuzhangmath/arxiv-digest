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
- [Automatic update recovery](#automatic-update-recovery)
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
application does not send telemetry. When a dashboard process starts, it checks
GitHub's public releases API within a sixty-second deadline. The check may read
multiple release list pages and release manifests to compare versions and
compatibility. These requests contain no interests or Library data. Choosing **Update and
restart** additionally downloads the verified wheel from the canonical GitHub
release assets. Installation and package rollback require no network after
commit. Following
an update-instructions link opens the release's GitHub page; an inconclusive
check offers the public releases page for a manual check. GitHub receives
ordinary connection details and a User-Agent containing the installed version.

The application also makes HTTPS requests to arXiv services to build the
candidate sample, recover daily lists, synchronize paper metadata, look up
custom papers, and download PDFs. These requests disclose selected arXiv
categories and requested date windows. A custom paper lookup or PDF download
discloses the exact arXiv identifier; opening an arXiv link sends that identifier
from the browser. arXiv also receives ordinary connection details such as the
connecting network address and request time, plus a User-Agent containing the
application version and project URL. Network operators can ordinarily see the
GitHub or arXiv host and connection metadata, while HTTPS protects the request
contents in transit.

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

## Automatic update recovery

The durable data directory contains a private `update-recovery` directory for
update plans, the atomic transition journal, protected wheel provenance,
retained wheels, automatic data backups, copied recovery programs, raw failed
state, and diagnostic logs. Environment snapshots live in the validated
application-owned sibling of pipx's `venvs` directory on the same filesystem.
These are recovery data, not regenerable application cache.

The target wheel is retained before commit. After a healthy update, protected
provenance identifies it as the current installed source. A prior wheel already
referenced by provenance remains available while an attempt could still need
recovery. The complete verified environment snapshot is the sole package
rollback source; no old wheel is retained solely to reinstall it.

After healthy completion, snapshot cleanup runs in bounded passes. It records
the verified snapshot's exact ownership before deleting any member, so an
interrupted deletion can retry even after a later update. If initial verification
times out or finds changed or unknown contents, the snapshot remains for
troubleshooting. Cleanup refusal does not undo a successful update.

Automatic backups use the existing portable format and exclude PDF bytes.
Recovery preserves the pre-update PDF destination. If the target version opened
application data before failing, recovery first preserves the current profile,
database, and restore-journal bytes as a private raw copy before restoring the
verified backup. If that preservation cannot be verified, recovery stops before
replacing the data. Failed-target raw state is never treated as disposable cache.

After a verified successful terminal outcome, retention keeps at most two valid
updater-owned automatic backups and two valid raw failed-state copies. Pending,
blocking, changed, or unverified recovery artifacts remain protected even if a
count limit is exceeded. Ordinary user-created backups are never pruned by the
updater. Private bounded diagnostic logs may contain local paths and process
errors; keep them private, along with plans and snapshots.

The transition journal is the only persistent receipt store. Reading a receipt
does not consume it; a dashboard acknowledges that exact receipt after rendering
it. Browser transition markers contain only a schema version, old-server
startup nonce, job identifier, and expiry, and expire after 24 hours. They
contain no session token, filesystem path, or paper data.

Do not delete recovery records, snapshots, retained wheels, or raw copies to
bypass an update lock or recovery refusal. Follow the
[update recovery instructions](troubleshooting.md#an-update-or-restart-did-not-finish)
first. Portable exports do not include updater internals or private logs.

## What a portable backup excludes

Portable backups exclude caches and raw responses, downloaded PDFs, runtime
tokens and locks, launcher files and launcher error details, machine-specific
absolute PDF paths, and transient setup drafts. Restore uses a newly confirmed
destination on the receiving computer. The portable content still includes
personal interests and library state, so do not publish a backup.

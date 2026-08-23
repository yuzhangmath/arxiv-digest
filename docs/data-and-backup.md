# Data and backup

arXiv Digest separates durable state, regenerable cache files, downloaded
PDFs, and portable backups.

## Contents

- [What is durable](#what-is-durable)
- [macOS locations](#macos-locations)
- [Linux locations](#linux-locations)
- [Cache safety](#cache-safety)
- [PDF destinations](#pdf-destinations)
- [Export a backup](#export-a-backup)
- [Inspect and import](#inspect-and-import)
- [What a portable backup excludes](#what-a-portable-backup-excludes)

## What is durable

The profile stores categories, keywords, phrases, authors, seed papers, and
the chosen PDF destination kind. The SQLite database stores article metadata,
synchronization checkpoints, evidence, review progress, and the saved library.
These are durable even when the dashboard is offline.

## macOS locations

- Configuration and durable data: `~/Library/Application Support/arxiv-digest`
- Regenerable cache: `~/Library/Caches/arxiv-digest`
- Backups: the `backups` folder inside the durable data directory

The active PDF destination is whichever standard folder or native-picker
choice you confirmed in setup or Settings.

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

The dashboard offers friendly standard destinations before the native folder
picker. It does not accept a typed path or trust a path sent by a browser
request. Folder testing and **Open folder** use only the active,
server-validated destination. Backups do not contain PDF bytes or an absolute
machine-local destination.

## Export a backup

From Settings, choose **Export backup** and save the browser download. From a
terminal, choose a new filename:

```bash
arxiv-digest export arxiv-digest-backup.zip
```

Export refuses to overwrite an existing file. A backup contains a manifest,
portable profile, and durable portable records with checksums. Keep it as
private as the interests and library it contains.

## Inspect and import

Settings inspects an uploaded backup without changing state. Review the
summary, reconfirm the local PDF destination, and confirm creation of a
pre-restore recovery backup before restore. The terminal equivalent is:

```bash
arxiv-digest import arxiv-digest-backup.zip
```

The app revalidates the archive immediately before restore and keeps the
current state if inspection or restore validation fails.

## What a portable backup excludes

Portable backups exclude caches and raw responses, downloaded PDFs, runtime
tokens and locks, launcher files and launcher error details, machine-specific
absolute PDF paths, and transient setup drafts. Restore uses a newly confirmed
destination on the receiving computer. The portable content still includes
personal interests and library state, so do not publish a backup.

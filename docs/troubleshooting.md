# Troubleshooting

Most problems can be diagnosed without exposing paper titles, author names,
interests, identifiers, or local paths.

## Contents

- [Run redacted diagnostics](#run-redacted-diagnostics)
- [The dashboard does not open](#the-dashboard-does-not-open)
- [First-run setup cannot load live data](#first-run-setup-cannot-load-live-data)
- [Synchronization is offline or partial](#synchronization-is-offline-or-partial)
- [Review or Calendar has coverage gaps](#review-or-calendar-has-coverage-gaps)
- [PDF actions fail](#pdf-actions-fail)
- [The launcher is missing](#the-launcher-is-missing)
- [A backup will not import](#a-backup-will-not-import)
- [Clean reset with recovery copy](#clean-reset-with-recovery-copy)

## Run redacted diagnostics

Open a terminal and run:

```bash
arxiv-digest doctor
```

`doctor` is read-only. On a fresh installation it does not create application
directories. Its output contains versions, statuses, and aggregate counts, not
paper titles, authors, keywords, IDs, error details, tokens, or paths. You may
paste this redacted output into ChatGPT and describe the visible symptom. Do
not attach the database, profile, backup, or downloaded PDFs.

## The dashboard does not open

Open a terminal and run `arxiv-digest`. Keep the terminal open while using the
dashboard. If a browser cannot be opened, the command prints a one-time
loopback URL. Do not share that URL: its fragment contains a short-lived local
session token. Start a new process instead of reusing an old URL. Only one
application instance uses a data directory at a time.

## First-run setup cannot load live data

First-run setup needs a live connection to arXiv to list categories and build
the candidate sample. Keep the terminal open. If the connection was
interrupted and no cached work is available, select **Retry corpus**; an
incomplete candidate-building attempt offers **Resume corpus** or **Restart
corpus**. If the same error returns after connectivity is restored, select
**Quit**, run `arxiv-digest doctor`, and share only its redacted output.

## Synchronization is offline or partial

Cached Review and Library pages remain usable offline. Settings reports
metadata synchronization separately from historical daily-list coverage and
canonical-event version resolution. One failed category does not imply that
all categories succeeded, and an interrupted recovery does not erase the
metadata checkpoint. Retry after connectivity returns.

## Review or Calendar has coverage gaps

Review and Calendar require recovered daily-list membership. Atom and OAI are
hidden support for metadata and version resolution; Atom-only or OAI-only
records stay out of both views. A failed, pending, or permanently unavailable
category/date remains one of the reported coverage gaps. Its papers do not
appear under a substitute date.

Settings shows the target, checked, with-papers, confirmed-empty, failed,
pending, and unavailable counts per category. Retry a failed date while it is
still in the supported recovery window. If it is outside that window, the gap
remains visible and the queue remains incomplete.

The setup candidate corpus does not populate Review, Calendar, or Library.
Library is independent of daily-list coverage: a saved paper stays saved when
its category is removed, and saving a paper does not create a Review event.

## PDF actions fail

Open Settings, choose the PDF folder again, then select **Test and use folder**.
The app will not use a path typed into a web request.
Existing downloads remain in their chosen folder; a portable restore requires
you to reconfirm a destination.

## The launcher is missing

The launcher is optional. In Settings choose **Create/Recreate** or **Retry**.
Choose **Not now** to clear a stored setup-time launcher error without touching
the filesystem, or **Remove** to delete only the exact managed launcher. You
can always reopen the app by opening a terminal and running `arxiv-digest`.
There is no scheduled background startup.

## A backup will not import

Import is inspect-first and rejects changed, oversized, encrypted, malformed,
or path-traversing archives. A failed inspection makes no change. A failed
restore leaves the same session usable and preserves the prior state; for a
non-empty destination the app verifies a private pre-restore recovery backup.
Portable backup format 2 is the only supported format; format-1 and
generation-1 archives are rejected before local state changes.

## Clean reset with recovery copy

Application-data generation 2 does not open an earlier database or profile and
does not import an earlier backup. Follow these steps in order:

1. **Quit arXiv Digest.** Select **Quit** and wait for the terminal prompt to
   return.
2. **Move the old durable data and regenerable cache** into a private
   timestamped recovery location outside the active directories. On Linux,
   move both the configuration and durable-data locations listed in
   [Data and backup](data-and-backup.md).
3. **Remove the managed desktop launcher and installed application.** Remove
   only the launcher created by arXiv Digest, then run `pipx uninstall
   arxiv-digest`.
4. **Install and start application-data generation 2.** Use the canonical
   HTTPS install command in [Installation](installation.md), then run
   `arxiv-digest init` to create new state.
5. **Leave separately downloaded PDFs** in their existing destination. Do not
   move or delete them with the old application data.

The private timestamped recovery location is rollback-only and is not imported
by generation 2. Do not copy it into the new active directories or select an
old backup for restore.

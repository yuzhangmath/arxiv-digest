# Troubleshooting

Most problems can be diagnosed without exposing paper titles, author names,
interests, identifiers, or local paths.

## Contents

- [Run redacted diagnostics](#run-redacted-diagnostics)
- [The dashboard does not open](#the-dashboard-does-not-open)
- [Synchronization is offline or partial](#synchronization-is-offline-or-partial)
- [Historical dates are inferred](#historical-dates-are-inferred)
- [PDF actions fail](#pdf-actions-fail)
- [The launcher is missing](#the-launcher-is-missing)
- [A backup will not import](#a-backup-will-not-import)
- [After an upgrade](#after-an-upgrade)

## Run redacted diagnostics

```bash
arxiv-digest doctor
```

`doctor` is read-only. On a fresh installation it does not create application
directories. Its output contains versions, statuses, and aggregate counts, not
paper titles, authors, keywords, IDs, error details, tokens, or paths. You may
paste this redacted output into ChatGPT and describe the visible symptom. Do
not attach the database, profile, backup, or downloaded PDFs.

## The dashboard does not open

Run `arxiv-digest` in a terminal. If a browser cannot be opened, the command
prints a one-time loopback URL. Do not share that URL: its fragment contains a
short-lived local session token. Start a new process instead of reusing an old
URL. Only one application instance uses a data directory at a time.

## Synchronization is offline or partial

Cached Review and Library pages remain usable offline. Settings reports
current metadata synchronization separately from historical coverage and
exact announcement enrichment. One failed category does not imply that all
categories succeeded, and an interrupted backfill does not erase the current
metadata checkpoint. Retry after connectivity returns.

## Historical dates are inferred

**Current** and **recovered** labels have exact announcement evidence.
**Inferred** means the application reconstructed the event from durable OAI
metadata without an exact mailing record. Inferred ordering is useful but may
not reproduce the original announcement day. The catch-up HTML parser is best
effort and can stop safely when upstream markup changes.

## PDF actions fail

Open Settings, select a standard destination or use the native picker again,
then run **Test folder**. The app will not use a path typed into a web request.
Existing downloads remain in their chosen folder; a portable restore requires
you to reconfirm a destination.

## The launcher is missing

The launcher is optional. In Settings choose **Create/Recreate** or **Retry**.
Choose **Not now** to clear a stored setup-time launcher error without touching
the filesystem, or **Remove** to delete only the exact managed launcher. You
can always reopen the app with `arxiv-digest`. There is no scheduled background
startup.

## A backup will not import

Import is inspect-first and rejects changed, oversized, encrypted, malformed,
or path-traversing archives. A failed inspection makes no change. A failed
restore leaves the same session usable and preserves the prior state; for a
non-empty destination the app verifies a private pre-restore recovery backup.

## After an upgrade

Run `arxiv-digest doctor`, then start `arxiv-digest` once. If the executable is
missing, open a new terminal after `pipx ensurepath` and run `pipx list`. If the
pipx environment itself is damaged, reinstall from the canonical HTTPS command
in [Installation](installation.md); do not delete durable data as a first step.

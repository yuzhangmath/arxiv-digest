# Troubleshooting

Most problems can be diagnosed without exposing paper titles, author names,
interests, identifiers, or local paths.

## Contents

- [Run redacted diagnostics](#run-redacted-diagnostics)
- [The dashboard does not open](#the-dashboard-does-not-open)
- [WSL cannot open the Windows browser](#wsl-cannot-open-the-windows-browser)
- [First-run setup cannot load live data](#first-run-setup-cannot-load-live-data)
- [Synchronization is offline or partial](#synchronization-is-offline-or-partial)
- [Review or Calendar has coverage gaps](#review-or-calendar-has-coverage-gaps)
- [PDF actions fail](#pdf-actions-fail)
- [The launcher is missing](#the-launcher-is-missing)
- [An update or restart did not finish](#an-update-or-restart-did-not-finish)
- [A backup will not import](#a-backup-will-not-import)
- [Clean reset with recovery copy](#clean-reset-with-recovery-copy)

## Run redacted diagnostics

Open a terminal and run:

```bash
arxiv-digest doctor
```

During ordinary startup, `doctor` reads profile and database state without
changing it and makes no network requests. Updater preflight may initialize
private coordination directories and lock files, including on a fresh
installation. Ordinary diagnostics do not create a profile, database, or
running dashboard. Their output contains versions, statuses, and aggregate counts, not
paper titles, authors, keywords, IDs, error details, tokens, or paths. You may
paste this redacted output into ChatGPT and describe the visible symptom. Do
not attach the database, profile, backup, or downloaded PDFs.

When update recovery is pending or blocked, `doctor` reports
`Update recovery: pending` or `Update recovery: blocked` and exits with status
3. It does not attempt recovery, open profile or database state, or print local
paths. This output can also be shared. Follow
[update recovery guidance](#an-update-or-restart-did-not-finish) to resolve the
unfinished update separately.

## The dashboard does not open

Open a terminal and run `arxiv-digest`. Keep the terminal open while using the
dashboard. If a browser cannot be opened, the command prints a one-time
loopback URL. Do not share that URL: its fragment contains a short-lived local
session token. Start a new process instead of reusing an old URL. Only one
application instance uses a data directory at a time.

To copy the current address without opening a browser, run
`arxiv-digest --copy-url`. This starts the dashboard if needed, or copies the
address for the existing instance. Paste it into your browser's address bar
and keep the original server terminal running. The option also works after
`init`, `library`, or `config`. If copying fails, the complete URL is printed
on its own line for manual copying. Clipboard access uses `pbcopy` on macOS,
or an available `wl-copy`, `xclip`, or `xsel` tool in a Linux desktop session.

## WSL cannot open the Windows browser

An `xdg-open: no method available for opening ...` message can occur when a
Linux installation inside WSL tries to open a Linux browser. Dashboard
startup now tries `wslview` when available, then Windows PowerShell to open
the Windows default browser. An explicit `BROWSER` setting is tried first.

For quick copying instead, run this in WSL:

```bash
arxiv-digest --copy-url
```

When the command reports that the URL was copied, open your Windows browser
and paste into its address bar with **Ctrl+V**. For first-run setup, use
`arxiv-digest init --copy-url`. The command uses Windows `clip.exe` to copy
the complete address, including the session token. If the dashboard is
already running, you can run the copy command from a second WSL terminal.

These Windows actions require
[WSL interoperability](https://learn.microsoft.com/en-us/windows/wsl/filesystems#run-windows-tools-from-linux)
and the relevant Windows tool (`powershell.exe` or `clip.exe`) on WSL's
`PATH`. If Windows integration is disabled or the tool fails, the app prints
the address for manual copying and keeps the dashboard available. Native
Windows installation remains unsupported; this applies to Linux running
inside WSL.

## First-run setup cannot load live data

First-run setup needs a live connection to arXiv to list categories and build
the candidate sample. Keep the terminal open. If the connection was
interrupted and no cached work is available, select **Retry corpus**; an
incomplete candidate-building attempt offers **Resume corpus** or **Restart
corpus**. If the same error returns after connectivity is restored, select
**Quit**, run `arxiv-digest doctor`, and share only its redacted output.

## Synchronization is offline or partial

Cached Review and Library pages remain usable offline. Settings reports
metadata synchronization separately from historical daily-list coverage.
One failed category does not imply that
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

## An update or restart did not finish

A manual link is expected for the 0.3.0 bootstrap, unsupported pipx versions or
installation layouts, changed dependencies, and unverified local provenance.
Use the [manual upgrade instructions](installation.md#upgrade-within-application-data-generation-2).
Do not change installation metadata to make the automatic-update check pass.

During normal preparation, background work finishes before the app closes. A
safe preparation failure restores normal use; retry canceled PDF downloads and
candidate-corpus jobs manually. **Updating and restarting** in the old tab is
handoff guidance, not confirmation that installation succeeded. Wait for the
new dashboard, which reports the final update or restoration result. If it does
not appear after a few minutes, run `arxiv-digest` again.

If the application reports an unresolved handoff, external installation change,
or failed recovery, fully quit all remaining arXiv Digest processes first. Do
not run pipx, replace an exposed executable, or delete a journal, snapshot,
retained wheel, launcher, or lock file while recovery is unresolved. Reopening
uses the protected recovery state and refuses to overwrite an unexpected live
installation. Keep the private recovery data available for diagnosis.

If the normal command cannot load because its installed files are incomplete,
invoke the application-owned recovery wrapper. On macOS:

```bash
"$HOME/Library/Application Support/arxiv-digest/update-recovery/recover-arxiv-digest" --explicit-recovery
```

On Linux with the default data directory:

```bash
"$HOME/.local/share/arxiv-digest/update-recovery/recover-arxiv-digest" --explicit-recovery
```

If an absolute `XDG_DATA_HOME` was configured for this installation, use its
`arxiv-digest/update-recovery/recover-arxiv-digest` wrapper instead. The wrapper
accepts only no arguments for ordinary recovery or the exact
`--explicit-recovery` flag for an explicitly requested guarded retry. Both paths
validate the copied runtime and acquire all required locks before touching state.
The wrapper is created by verified updater preparation; a missing wrapper is not a reason
to construct one or edit a journal by hand. Its fixed interpreter and arguments
come from protected local state. Follow any further explicit recovery refusal
and keep the files intact.

The updater preserves downloaded PDFs and makes a private raw copy before
replacing data opened by a failed target. If recovery cannot be verified, it
stops and shows recovery guidance; it does not claim that a dashboard reopened.
Share only `doctor`'s redacted output in ordinary reports. Recovery plans,
backups, snapshots, raw copies, and logs may contain private data and paths.

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

# Troubleshooting

Most problems can be diagnosed without exposing paper titles, author names,
interests, identifiers, or local paths.

## Contents

- [Run redacted diagnostics](#run-redacted-diagnostics)
- [The dashboard does not open](#the-dashboard-does-not-open)
- [WSL cannot open the Windows browser](#wsl-cannot-open-the-windows-browser)
- [First-run setup cannot load live data](#first-run-setup-cannot-load-live-data)
- [Interests suggestions do not load](#interests-suggestions-do-not-load)
- [Synchronization is offline or partial](#synchronization-is-offline-or-partial)
- [Review or Calendar has coverage gaps](#review-or-calendar-has-coverage-gaps)
- [PDF actions fail](#pdf-actions-fail)
- [The launcher is missing](#the-launcher-is-missing)
- [Updating the app](#updating-the-app)
- [A backup will not import](#a-backup-will-not-import)
- [Clean reset with recovery copy](#clean-reset-with-recovery-copy)

## Run redacted diagnostics

Open a terminal and run:

```bash
arxiv-digest doctor
```

`doctor` reads profile and database state without changing it or making
network requests. It does not create a profile, database, or running dashboard.
Its output contains versions, statuses, and aggregate counts, not paper titles,
authors, keywords, IDs, error details, tokens, or paths. You may paste this
redacted output into ChatGPT and describe the visible symptom. Do not attach
the database, profile, backup, or downloaded PDFs.

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

First-run setup needs a live connection to arXiv to list categories. Keep the
terminal open and select **Retry** after connectivity returns. Once categories
are available, choose coverage and a PDF folder, test it, and confirm the
profile. Personalization is optional and available later in **Interests**.
If the same error returns after connectivity is restored, select **Quit**, run
`arxiv-digest doctor`, and share only its redacted output.

## Interests suggestions do not load

Choose **Refresh suggestions** in Interests to build or resume the optional
90-day paper sample. This uses live arXiv data and each attempt is capped at
five minutes. If an attempt is interrupted or the sample needs more data,
refresh again after connectivity returns. You can keep reviewing papers and
add custom interests without waiting for suggestions. Selections and custom
entries change your profile only after **Update interests**.

## Synchronization is offline or partial

Cached Review and Library pages remain usable offline. Settings reports
metadata synchronization separately from historical daily-list coverage, and
shows it as incomplete when a category has a saved failure, even if other arXiv
requests succeeded. The checkpoint is the last
completed metadata synchronization; a failed attempt does not advance or erase it.
After connectivity returns, restart the app to retry ordinary synchronization.

If arXiv reports HTTP 429 or an explicit “Rate exceeded” response, synchronization
stops and Settings shows when requests can resume. A valid `Retry-After` response
controls that deadline; otherwise the app uses a one-hour pause. This is a
conservative local retry policy, not a guarantee that arXiv access will have
recovered. Restarting the app or clearing its cache does not cancel the pause.
Saved Review and Library papers remain available during it.

After the pause expires, use the retry button next to one failed date in Settings.
If that succeeds, retry the remaining failed dates. HTTP 406 is displayed as a
request refusal with its status code, without assuming it means rate limiting.
For a plain HTTP 406 from a daily-list page, an OAI ListRecords metadata page
(including continuation pages), or an OAI GetRecord abstract lookup, the app can
try the same URL once using the system `curl` executable. This fallback keeps
the app's request pacing, cooldown, timeout, response-size and
HTTPS redirect checks. A successful response goes directly through the normal
parser; the app does not depend on a second Python request or cache warming.
If `curl` is unavailable or cannot complete the request, the original HTTP
failure remains available for diagnosis. No curl installation is performed.

When a daily list's first page still returns plain HTTP 406, the app also tries
that list once without inline abstracts (`abs=False`), as earlier versions
requested it. Every recovery still requires a complete verified daily list.
An explicit rate limit or active pause stops requests, including all fallbacks.
These attempts do not guarantee that arXiv will accept the request, and waiting
alone does not establish that the problem is resolved.

The app does not mark dates it has not attempted as new failures. If a single
date still fails after the pause expires, retain the date, HTTP status and time
for an [arXiv support request](https://info.arxiv.org/help/contact.html).

## Review or Calendar has coverage gaps

Review and Calendar require recovered daily-list membership. Atom and OAI are
hidden support for metadata and version resolution; Atom-only or OAI-only
records stay out of both views. A failed, pending, or permanently unavailable
category/date remains one of the reported coverage gaps. Its papers do not
appear under a substitute date.

Calendar shows **Retrieval failed** for a failed date with no confirmed papers,
without implying that the date was empty or reviewed. If confirmed papers are
already available for that date, it remains clickable and shows **Some retrievals
failed** alongside the confirmed paper count. Open Settings for coverage details
and retry controls. After a successful retry, reopen Calendar to see the updated
date.

Settings shows the target, checked, with-papers, confirmed-empty, failed,
pending, and unavailable counts per category. Retry a failed date while it is
still in the supported recovery window. If it is outside that window, the gap
remains visible and the queue remains incomplete. When one category failed and
another is pending on the same date, the overall date counts as failed in both
Status and Settings; per-category counts retain the pending work.

Missing abstracts are separate from daily-list coverage. You can read and save
the confirmed papers and complete the date with **Finish date** or **Finish all**.
Open the date to optionally choose **Retry missing abstracts**; this requests
only missing abstracts for its confirmed papers, including previously reviewed
papers. The date reports recovered and remaining counts and any errors, such as
HTTP 406, during the current app session. A failed abstract request leaves the
paper available to review. Each success is saved immediately, so another retry
continues with the remaining papers. Restarting clears the retry-result display
but preserves recovered abstracts and review progress.

The curl fallback also applies to the OAI GetRecord requests made by **Retry
missing abstracts**. It can help when HTTP 406 is preventing access to an
existing abstract; it cannot supply an abstract absent from arXiv's response.

The optional Interests sample does not populate Review, Calendar, or Library.
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

## Updating the app

The dashboard's update notice links to release notes and manual instructions.
It does not install an update or restart the app. Follow the
[manual upgrade instructions](installation.md#upgrade-within-application-data-generation-2),
then run `arxiv-digest doctor` to verify the version. If the release check is
inconclusive, the notice links to the public releases page so you can check
manually. Restarting the app runs a fresh check.

If an older version has an unfinished automatic update, resolve it with that
version before upgrading. Keep its recovery files, snapshots, and backups
intact; the current app does not use or delete them. Refresh an existing
desktop launcher after upgrading with `arxiv-digest install-launcher`.

## A backup will not import

Select **Quit**, wait for the app to stop, then run
`arxiv-digest import BACKUP.zip` in a terminal, replacing `BACKUP.zip` with your
backup's path. Settings provides export and these restore instructions. The
import command asks you to choose a local PDF destination and tests it.

Import is inspect-first and rejects changed, oversized, encrypted, malformed,
or path-traversing archives. A failed inspection makes no change. Before
replacing existing state, the app creates and verifies a private pre-restore
recovery backup. Archive revalidation and crash recovery preserve the restore
safeguards described in [Inspect and import](data-and-backup.md#inspect-and-import).
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

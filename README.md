# arXiv Digest

arXiv Digest is a local, explainable review queue for arXiv. It runs on macOS
and Linux, keeps its durable state on your computer, and opens a loopback-only
dashboard in your browser. There is no cloud account, telemetry, or API key.
It is an independent project and is not affiliated with or endorsed by arXiv.

This guide covers version 0.3.0. The `@v0.3.0` commands below require that tag on the
[releases page](https://github.com/yuzhangmath/arxiv-digest/releases).
Already using an earlier version? Follow the
[upgrade guide](docs/installation.md#upgrade-within-application-data-generation-2)
to back up, replace the program, and verify your saved state.

Already have Python 3.11+, Git, and `pipx`? Open a terminal and run these
commands one at a time:

```bash
pipx install git+https://github.com/yuzhangmath/arxiv-digest.git@v0.3.0
arxiv-digest init
```

## Contents

- [What it does](#what-it-does)
- [How ranking works](#how-ranking-works)
- [Requirements](#requirements)
- [Install](#install)
- [Set up your first digest](#set-up-your-first-digest)
- [Try the main workflow](#try-the-main-workflow)
- [Daily use](#daily-use)
- [Useful commands](#useful-commands)
- [Privacy and local data](#privacy-and-local-data)
- [Updates and recovery](#updates-and-recovery)
- [Maintenance and troubleshooting](#maintenance-and-troubleshooting)
- [Documentation](#documentation)
- [License](#license)

## What it does

arXiv Digest retrieves paper metadata for the categories you choose, builds a
review queue using interests you explicitly select, and explains why each
paper was ranked. Review and Calendar show a paper only after the app has
recovered its daily-list membership for that category and date. You can save
papers to a local library and optionally download PDFs to a folder you choose.

The application does not run a scheduled or background service. It runs only
while you have started it from a terminal or the optional desktop launcher.

## How ranking works

Papers are ranked locally and deterministically, separately for each arXiv
announcement date. Interest matches ignore case and punctuation but otherwise
require whole terms. The app first groups every paper into **Top**, **Possible**,
or **Other**. **Top** contains papers with an exact selected-author match, a
selected phrase in the title, or two distinct selected term or author matches.
One other selected term match puts a paper in **Possible**. A selected category
adds only a small baseline score and does not change the group by itself.

The app also compares title-and-abstract text with selected seed papers and
papers saved in the local Library using TF-IDF similarity. Strong similarity
to a seed paper can promote a paper to **Top**; similarity to a seed or saved
paper can promote it to **Possible**. Within each group, papers are ordered by
a fixed weighted score: authors and title matches carry more weight than
abstract matches, and seed-paper similarity carries more weight than
saved-paper similarity. Ties use arXiv mailing order when known, then stable
paper identifiers. No paper is discarded for a low rank; expand **Why this
ranking** on a card to see the signals that contributed. Similarity explanations
name and link the seed or saved paper that contributed the match.

## Requirements

You need:

- macOS or Linux; Windows is not supported in version 0.3.0
- Python 3.11 or newer
- Git
- a current `pipx`
- a web browser and internet access to arXiv

Open a terminal and check your Python and Git versions:

```bash
python3 --version
git --version
```

## Install

### 1. Install pipx

If `pipx --version` already works, continue to the next step.

In a terminal on macOS with Homebrew, install `pipx`:

```bash
brew install pipx
```

Wait for that command to finish and return to the terminal prompt. Then add
the `pipx` commands to your shell's `PATH`:

```bash
pipx ensurepath
```

In a terminal on Ubuntu 23.04 or newer, first refresh the package list:

```bash
sudo apt update
```

When that finishes, install `pipx`:

```bash
sudo apt install pipx
```

When installation finishes, add the `pipx` commands to your shell's `PATH`:

```bash
pipx ensurepath
```

On Fedora, install `pipx`:

```bash
sudo dnf install pipx
```

Wait for installation to finish, then update your shell's `PATH`:

```bash
pipx ensurepath
```

For other Linux distributions, see the detailed
[Installation guide](docs/installation.md).

After `pipx ensurepath`, open a new terminal before continuing. In the new
terminal, confirm that the command is available:

```bash
pipx --version
```

### 2. Install arXiv Digest

In that terminal, install from the public HTTPS repository:

```bash
pipx install git+https://github.com/yuzhangmath/arxiv-digest.git@v0.3.0
```

When installation finishes, verify that the installed command works:

```bash
arxiv-digest doctor
```

On a fresh installation, `doctor` should report version `0.3.0`, a missing
profile and database, and `arxiv-digest init` as the next step. If your shell
cannot find `arxiv-digest`, open a new terminal after `pipx ensurepath` and try
again.

## Set up your first digest

In the terminal, start the guided setup:

```bash
arxiv-digest init
```

The command starts a local server and opens the dashboard in your browser.
Keep the terminal process running while you use the dashboard. Setup guides
you through these choices:

1. Choose one or more arXiv categories.
2. Choose the start of confirmed historical daily-list coverage. The
   recommended starting point is 30 days ago.
3. Let the app build a bounded 90-day candidate sample from live arXiv data.
   This requires internet access. Each candidate-building attempt is capped at
   five minutes; if more data is needed or the connection is interrupted, the
   dashboard offers **Resume corpus** or **Restart corpus**.
4. Review candidate papers and optionally choose seed papers, terms, and
   authors. The **Terms** step presents multiword **Suggested terms** and one
   **Custom term** control. A one-word custom term is saved as a keyword; a
   custom term of 2–12 words is saved as a phrase. Setup and Interests present
   both together as terms while preserving that classification. Only
   checked suggestions and nonblank entries you explicitly type become
   interests; searching, navigating, and paging do not select anything.
5. Choose and test a PDF folder.
6. Review the summary and confirm the profile.
7. Choose whether to create the optional desktop launcher. **Not now** is a
   complete and supported choice.

The candidate corpus does not populate Review, Calendar, or Library. Selecting
a seed paper affects ranking preferences but does not save it. A paper enters
Library only when you explicitly save it.

Downloaded PDFs stay in the selected folder and are not included in portable
backups.

## Try the main workflow

After you confirm setup, the app opens Review and starts synchronization. Wait
for papers to appear, then use the still-open dashboard for a practical smoke
test:

1. Open **Why this ranking** on a paper card and check that it matches your
   selected interests.
2. Save one paper to the Library.
3. Download one PDF and confirm that it appears in your chosen folder.
4. Use **Finish date** when you are done with a review date.
5. Open Calendar and confirm that the date's progress is recorded.
6. Open Library, Interests, and Settings and confirm that your choices appear.
7. Select **Quit** in the top-right corner when finished.

Closing the browser tab does not stop the application immediately: the
terminal process normally remains running for about 3 minutes and may take up
to about 4.5 minutes when the browser cannot deliver its disconnect notice.
Select **Quit** in the dashboard and wait for the terminal prompt to return.

After **Quit** stops the app and the terminal prompt returns, run
`arxiv-digest` again and confirm that your profile, review progress, and saved
library persist.

## Daily use

Open a terminal and start the application from any directory:

```bash
arxiv-digest
```

The dashboard opens to the oldest unfinished review date. Each page contains
at most 20 paper cards; date navigation and paging do not change your
interests. Use **Finish date** explicitly when you are done with a date, and
use **Quit** when you want to stop the local application.

Review and Calendar require recovered daily-list membership. The app uses Atom
and OAI as hidden support for current metadata and version resolution; neither
source creates a visible review date by itself. If daily-list recovery fails,
the affected category and date remain a visible coverage gap in Settings and
their papers stay out of Review and Calendar until recovery succeeds. Coverage
progress distinguishes checked dates with papers, confirmed empty dates,
pending dates, retryable failures, and dates that are no longer available.

Library is independent of the active Review and Calendar categories. Saving a
paper does not create daily-list membership, and removing a category does not
remove saved papers or separately downloaded PDFs. With search blank, Library
orders saved papers by original arXiv submission date, newest first; searches
prioritize relevance, then recency. Library cards also link to arXiv.

## Useful commands

Run these commands in a terminal. Commands that open the dashboard remain in
the foreground, so keep that terminal open and use **Quit** in the dashboard
when finished. Diagnostic, backup, and launcher commands return to the prompt
when complete.

| Command | Purpose |
| --- | --- |
| `arxiv-digest` | Open the normal Review dashboard |
| `arxiv-digest --copy-url` | Start or reuse the dashboard and copy its URL instead of opening a browser |
| `arxiv-digest init` | Start first-run setup, or open Interests after setup |
| `arxiv-digest library` | Open the Library directly |
| `arxiv-digest config` | Open Settings directly |
| `arxiv-digest doctor` | Print [read-only, redacted diagnostics](docs/troubleshooting.md#run-redacted-diagnostics), including pending recovery status |
| `arxiv-digest export BACKUP.zip` | Export a portable backup to a new file |
| `arxiv-digest import BACKUP.zip` | Inspect and import a portable backup |
| `arxiv-digest install-launcher` | Create or recreate the optional desktop launcher |

For a Linux installation inside WSL, the app tries to open the Windows
browser. To paste the address yourself, use `arxiv-digest --copy-url`, then
paste into the Windows browser's address bar. This also works with
`arxiv-digest init --copy-url`, `library --copy-url`, and `config --copy-url`.
Keep the original server terminal open. See
[WSL browser and clipboard help](docs/troubleshooting.md#wsl-cannot-open-the-windows-browser)
if Windows integration is unavailable.

## Privacy and local data

There is no telemetry, cloud account, or bundled personal collection.
There is no `.eml` import. The dashboard binds only to your computer's
loopback interface. Paper metadata, interests, review progress, and the library
remain in the platform data directories described in
[Data and backup](docs/data-and-backup.md).

When a dashboard process starts, the app checks GitHub's public releases API
in the background within a sixty-second deadline. It may read multiple release
list pages and release manifests to compare versions and compatibility. It
sends no interests or library data. A compatible release offers **Update and restart** after installation eligibility
is verified. Other releases retain an update-instructions link; an inconclusive
check offers the public releases page so you can check manually. GitHub receives ordinary connection details and a User-Agent
containing the installed version.

The app also makes outbound HTTPS requests to arXiv services. Candidate
building and synchronization disclose the selected arXiv categories and
requested date windows. Looking up a custom paper, downloading a PDF, or
following an arXiv link discloses that paper's exact arXiv identifier. Interest
terms and authors, ranking results, review progress, and local Library searches
are not sent to arXiv. arXiv also receives ordinary connection details such as
the connecting network address and request time, plus a User-Agent containing
the application version and project URL. Network operators can ordinarily see
the GitHub or arXiv host and connection metadata, while HTTPS protects the
request contents in transit.

arXiv Digest does not cryptographically encrypt the local profile, SQLite
database, downloaded PDFs, or portable backup ZIP files. Protect them with
operating-system access controls and device or volume encryption if needed, and
transfer backups only through trusted channels.

This release uses application-data generation 2, profile schema 2, and
portable backup format 2. Earlier databases, profiles, and portable backups
from application-data generation 1 are rejected without modification; they
are not converted or imported. Generation-2 data from 0.2.0 and 0.2.1 remains
compatible.

`arxiv-digest doctor` prints read-only, redacted diagnostics. If update recovery
is pending or blocked, it reports that status without attempting recovery,
opening application data, or printing local paths. See
[Run redacted diagnostics](docs/troubleshooting.md#run-redacted-diagnostics).
You can share its redacted output when asking for troubleshooting help, but do not share your
database, profile, backup, downloaded PDFs, or a dashboard URL containing a
session token.

## Updates and recovery

Version 0.3.0 is the manual bootstrap for one-click updates. Upgrade 0.2.0 or
0.2.1 using the [manual installation instructions](docs/installation.md#upgrade-within-application-data-generation-2).
It keeps application-data generation 2 and your existing profile, Library,
review progress, and downloaded PDFs.

For a later compatible release, **Update and restart** downloads and verifies
the release wheel, saves the complete installed environment and a private data
backup, finishes background work, then reopens the dashboard. One click starts
the whole process. Wait for the new tab before closing the old one; the new tab
reports whether the update succeeded or the previous version was restored.
Canceled PDF or candidate-corpus jobs require manual retry. Ordinary bounded
synchronization resumes after safe preparation failure.

Automatic updates initially support only verified macOS/Linux installations
using pipx 1.16.7 with its `pip` backend, the retained interpreter, no suffix,
injected packages, or system-site packages, and unchanged dependencies. Setup
must be complete. The source must be the documented canonical tag or a previous
verified updater installation. Other layouts and dependency-changing releases
remain manual. See [automatic-update eligibility](docs/installation.md#automatic-updates).

If no new tab appears after a few minutes, launch arXiv Digest again. If it asks
you to fully quit or reports an unresolved recovery, follow the
[update recovery instructions](docs/troubleshooting.md#an-update-or-restart-did-not-finish).

## Maintenance and troubleshooting

To upgrade an existing version-0.2.0 or version-0.2.1 installation, first quit the app and make
a current private backup if its local state matters. Then follow the
[routine generation-2 upgrade](docs/installation.md#upgrade-within-application-data-generation-2).
The upgrade replaces the pipx-managed program without resetting the profile,
database, or downloaded PDFs.

Generation-2 backups contain interests and Library state, so keep them
private. Export refuses to overwrite an existing file. Cache deletion is safe:
it does not erase the profile, synchronization checkpoints, review progress,
or saved Library. Moving from an earlier data generation requires the documented
**Clean reset with recovery copy**; do not import the old database, profile, or
backup into generation 2.

See [Installation](docs/installation.md) for clean resets and uninstalling, and
[Troubleshooting](docs/troubleshooting.md) for dashboard, synchronization,
PDF, launcher, backup, and recovery guidance.

## Documentation

- [Installation and first launch](docs/installation.md)
- [Data locations, cache, and backup](docs/data-and-backup.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [v0.3.0 update-bootstrap notes](docs/releases/v0.3.0.md)
- [v0.2.1 technical-beta notes](docs/releases/v0.2.1.md)

Friends should install over the public HTTPS URL shown above. Maintainers may
use the separate SSH remote `git@github.com:yuzhangmath/arxiv-digest.git`; it
is not an end-user install address.

## License

arXiv Digest is released under the [MIT License](LICENSE). Vendored dependency
licenses are listed in [Third-Party Notices](THIRD_PARTY_NOTICES.md).

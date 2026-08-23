# arXiv Digest

arXiv Digest is a local, explainable review queue for arXiv. It runs on macOS
and Linux, keeps its durable state on your computer, and opens a loopback-only
dashboard in your browser. There is no cloud account, telemetry, or API key.

Already have Python 3.11+, Git, and `pipx`? Open a terminal and run these
commands one at a time:

```bash
pipx install git+https://github.com/yuzhangmath/arxiv-digest.git@v0.1.0
arxiv-digest init
```

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Install](#install)
- [Set up your first digest](#set-up-your-first-digest)
- [Try the main workflow](#try-the-main-workflow)
- [Daily use](#daily-use)
- [Useful commands](#useful-commands)
- [Privacy and local data](#privacy-and-local-data)
- [Maintenance and troubleshooting](#maintenance-and-troubleshooting)
- [Documentation](#documentation)
- [License](#license)

## What it does

arXiv Digest retrieves paper metadata for the categories you choose, builds a
review queue using interests you explicitly select, and explains why each
paper was ranked. You can review papers by announcement date, save papers to a
local library, and optionally download PDFs to a folder you choose.

The application does not run a scheduled or background service. It runs only
while you have started it from a terminal or the optional desktop launcher.

## Requirements

You need:

- macOS or Linux; Windows is not supported in version 0.1
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
pipx install git+https://github.com/yuzhangmath/arxiv-digest.git@v0.1.0
```

When installation finishes, verify that the installed command works:

```bash
arxiv-digest doctor
```

On a fresh installation, `doctor` should report version `0.1.0`, a missing
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
2. Choose the initial history range. The recommended starting point is 30 days.
3. Let the app build a bounded 90-day candidate sample from live arXiv data.
   This requires internet access. Each candidate-building attempt is capped at
   five minutes; if more data is needed or the connection is interrupted, the
   dashboard offers **Resume** or **Retry**.
4. Review candidate papers and choose seed papers, keywords, phrases, and
   authors. Only checked suggestions and entries you explicitly type become
   interests; searching, navigating, and paging do not select anything.
5. Choose and test a PDF folder.
6. Choose whether to create the optional desktop launcher. **Not now** is a
   complete and supported choice.
7. Review the summary and confirm the profile.

Downloaded PDFs stay in the selected folder and are not included in portable
backups.

## Try the main workflow

After you confirm setup, the app opens Review and starts synchronization. Wait
for papers to appear, then use the still-open dashboard for a practical smoke
test:

1. Read **Why this ranking** on a paper card and check that it matches your
   selected interests.
2. Save one paper to the Library.
3. Download one PDF and confirm that it appears in your chosen folder.
4. Use **Finish date** when you are done with a review date.
5. Open Calendar and confirm that the date's progress is recorded.
6. Open Library, Interests, and Settings and confirm that your choices appear.
7. Select **Quit** in the top-right corner when finished.

Closing the browser tab does not stop the application immediately: the
terminal process may remain running for about 30 minutes. Select **Quit** in
the dashboard and wait for the terminal prompt to return.

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

Cards label date evidence as **current**, **recovered**, or **inferred**.
Current and recovered dates come from exact announcement evidence. Inferred
dates are a best reconstruction from durable metadata and may not equal the
original mailing date.

## Useful commands

Run these commands in a terminal. Commands that open the dashboard remain in
the foreground, so keep that terminal open and use **Quit** in the dashboard
when finished. Diagnostic, backup, and launcher commands return to the prompt
when complete.

| Command | Purpose |
| --- | --- |
| `arxiv-digest` | Open the normal Review dashboard |
| `arxiv-digest init` | Start first-run setup, or open Interests after setup |
| `arxiv-digest library` | Open the Library directly |
| `arxiv-digest config` | Open Settings directly |
| `arxiv-digest doctor` | Print read-only, redacted status and diagnostics |
| `arxiv-digest export BACKUP.zip` | Export a portable backup to a new file |
| `arxiv-digest import BACKUP.zip` | Inspect and import a portable backup |
| `arxiv-digest install-launcher` | Create or recreate the optional desktop launcher |

## Privacy and local data

There is no telemetry, cloud account, or bundled personal collection.
There is no `.eml` import. The dashboard binds only to your computer's
loopback interface. Paper metadata, interests, review progress, and the library
remain in the platform data directories described in
[Data and backup](docs/data-and-backup.md).

`arxiv-digest doctor` prints aggregate, redacted diagnostics. You can share
that output when asking for troubleshooting help, but do not share your
database, profile, backup, downloaded PDFs, or a dashboard URL containing a
session token.

## Maintenance and troubleshooting

Quit the dashboard before upgrading. Then open a terminal and export a backup.
Only run the upgrade command after the export finishes successfully:

```bash
arxiv-digest export arxiv-digest-backup.zip
pipx install --force git+https://github.com/yuzhangmath/arxiv-digest.git@v0.1.0
```

Export refuses to overwrite an existing file. Backups contain interests and
library state, so keep them private. Cache deletion is safe: it does not erase
the profile, synchronization checkpoints, review progress, or saved library.

See [Installation](docs/installation.md) for upgrades and uninstalling, and
[Troubleshooting](docs/troubleshooting.md) for dashboard, synchronization,
PDF, launcher, backup, and recovery guidance.

## Documentation

- [Installation and first launch](docs/installation.md)
- [Data locations, cache, and backup](docs/data-and-backup.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

Friends should install over the public HTTPS URL shown above. Maintainers may
use the separate SSH remote `git@github.com:yuzhangmath/arxiv-digest.git`; it
is not an end-user install address.

## License

arXiv Digest is released under the [MIT License](LICENSE). Vendored dependency
licenses are listed in [Third-Party Notices](THIRD_PARTY_NOTICES.md).

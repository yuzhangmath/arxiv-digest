# Installation

This guide installs arXiv Digest as an isolated command-line application on
macOS or Linux.

## Contents

- [Requirements](#requirements)
- [Install pipx on macOS](#install-pipx-on-macos)
- [Install pipx on Linux](#install-pipx-on-linux)
- [Install arXiv Digest](#install-arxiv-digest)
- [Complete first-run setup](#complete-first-run-setup)
- [Use the app](#use-the-app)
- [Clean reset with recovery copy](#clean-reset-with-recovery-copy)
- [Uninstall](#uninstall)
- [HTTPS and maintainer SSH](#https-and-maintainer-ssh)

## Requirements

arXiv Digest requires Python 3.11 or newer, macOS or Linux, Git for the
friend-facing source install, and a current `pipx`. `pipx` keeps applications
in isolated environments and exposes their commands on your `PATH`.

Open a terminal to run the commands in this guide. When a block contains more
than one command, run them in order and wait for each command to finish. First,
check that Python 3.11 or newer and Git are available:

```bash
python3 --version
git --version
```

The commands below follow the current official
[pipx installation guide](https://pipx.pypa.io/stable/how-to/install-pipx.html).
After `pipx ensurepath`, open a new terminal before continuing.

## Install pipx on macOS

Install Homebrew first if it is not already present. Then open a terminal and
install `pipx`:

```bash
brew install pipx
```

Wait for that command to finish and return to the terminal prompt. Then add
the `pipx` commands to your shell's `PATH`:

```bash
pipx ensurepath
```

This is the macOS route currently recommended by pipx.

## Install pipx on Linux

On Ubuntu 23.04 or newer, open a terminal and first refresh the package list:

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

For other distributions, prefer the distribution's `pipx` package. Systems
enforcing PEP 668 may reject `pip install --user`; consult the same official
pipx guide for its self-managed virtual-environment fallback.

## Install arXiv Digest

After `pipx ensurepath`, open a new terminal. In that terminal, install arXiv
Digest from the canonical public HTTPS address:

```bash
pipx install git+https://github.com/yuzhangmath/arxiv-digest.git@v0.2.0
```

The official [pipx CLI reference](https://pipx.pypa.io/stable/reference/cli.html)
documents VCS URLs as supported package specifications.

## Complete first-run setup

In the same terminal, start the guided first-run setup:

```bash
arxiv-digest init
```

The command keeps running while the local dashboard is open. Leave the
terminal open during setup. Listing categories and building the candidate
sample require a live connection to arXiv. Each candidate-building attempt is
capped at five minutes; an incomplete attempt offers **Resume corpus** or
**Restart corpus**.
When finished, select **Quit** and wait for the terminal prompt to return.
Closing only the browser tab normally leaves the process running for about 3
minutes and may take up to about 4.5 minutes when the browser cannot deliver
its disconnect notice. The dashboard guides you through categories, initial
history, candidate papers, optional seed papers, terms and authors, a
picker-selected PDF folder, profile review, and the optional desktop launcher.
The **Terms** step presents multiword **Suggested terms** and one **Custom
term** control. A one-word custom term is saved as a keyword; a custom term of
2–12 words is saved as a phrase. Setup and Interests present both together as
terms while preserving that classification. Only checked suggestions and
nonblank custom entries become preferences. Search, navigation, and paging do
not select anything.

The history choice controls recovered daily-list membership. Review and
Calendar remain empty for a category/date until that daily list is recovered.
Atom and OAI provide hidden support for metadata and version resolution but do
not create visible review dates. The candidate corpus does not populate Review,
Calendar, or Library, and selecting a seed does not save it. Failed recovery
leaves coverage gaps; Settings shows per-category progress and retryable dates.
Library is independent of that coverage, so removing a category does not
remove papers you saved explicitly.

## Use the app

For later sessions, open a terminal and run this from any directory:

```bash
arxiv-digest
```

Keep the terminal open while the dashboard is running, and use **Quit** in the
dashboard to stop the application cleanly. The desktop launcher is optional.
It is a manual launch shortcut, not a login item or scheduler.
`arxiv-digest library` opens the library and `arxiv-digest config` opens
Settings.

## Clean reset with recovery copy

This release uses application-data generation 2, profile schema 2, and
portable backup format 2. An earlier database or profile cannot be opened, and
an earlier portable backup cannot be imported. The app rejects each before
changing it. Use this exact flow when moving from the earlier generation:

1. **Quit arXiv Digest.** Select **Quit**, wait for the terminal prompt to
   return, and confirm no launcher-started copy remains running.
2. **Move the old durable data and regenerable cache** into a private
   timestamped recovery location outside the active application directories.
   Include both the configuration and durable-data locations on Linux. The
   locations are listed in [Data and backup](data-and-backup.md).
3. **Remove the managed desktop launcher and installed application.** Remove
   only the launcher created by arXiv Digest, then run `pipx uninstall
   arxiv-digest`.
4. **Install and start application-data generation 2.** Run the public install
   command from [Install arXiv Digest](#install-arxiv-digest), then run
   `arxiv-digest init` to create a new profile and database.
5. **Leave separately downloaded PDFs** in their existing destination. They
   are outside application data and must not be moved or deleted as part of
   this reset.

The private timestamped recovery location is rollback-only and is not imported
by generation 2. Do not place the recovery copy back into the new active data
directories and do not select an old portable backup during setup.

## Uninstall

Remove the optional desktop launcher in Settings and quit the application.
Then open a terminal and run:

```bash
pipx uninstall arxiv-digest
```

Uninstalling the executable does not silently erase application data or PDFs.
After making a verified backup, remove those directories explicitly with your
file manager if you also want to delete them. See
[Data and backup](data-and-backup.md) for their locations.

## HTTPS and maintainer SSH

The canonical public project URL is
`https://github.com/yuzhangmath/arxiv-digest`. HTTPS is the documented install
route for friends. The exact maintainer-only SSH remote is
`git@github.com:yuzhangmath/arxiv-digest.git`; it is a repository maintenance
identity, not an installation prerequisite.

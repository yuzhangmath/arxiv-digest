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
- [Upgrade](#upgrade)
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
pipx install git+https://github.com/yuzhangmath/arxiv-digest.git@v0.1.0
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
capped at five minutes; an incomplete attempt offers **Resume** or **Retry**.
When finished, select **Quit** and wait for the terminal prompt to return.
Closing only the browser tab may leave the process running for about 30
minutes. The dashboard guides you through categories, initial history,
candidate papers, interests, a standard or picker-selected PDF folder, and the
optional desktop launcher. Only checked suggestions and explicitly typed
custom entries become preferences. Search, navigation, and paging do not
select anything.

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

## Upgrade

Quit the dashboard first. Then open a terminal and export a backup. Only run
the upgrade command after the export finishes successfully:

```bash
arxiv-digest export arxiv-digest-backup.zip
pipx install --force git+https://github.com/yuzhangmath/arxiv-digest.git@v0.1.0
```

The release tag keeps the installed source fixed and reproducible. When a new
release is documented, replace `v0.1.0` with that release's tag. Open the app
after upgrading so any bundled database migrations can run. Portable
application data is stored separately from the pipx environment.

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

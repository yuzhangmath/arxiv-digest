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

The commands below follow the current official
[pipx installation guide](https://pipx.pypa.io/stable/how-to/install-pipx.html).
After `pipx ensurepath`, open a new terminal before continuing.

## Install pipx on macOS

Install Homebrew first if it is not already present, then run:

```bash
brew install pipx
pipx ensurepath
```

This is the macOS route currently recommended by pipx.

## Install pipx on Linux

On Ubuntu 23.04 or newer, use the distribution package:

```bash
sudo apt update
sudo apt install pipx
pipx ensurepath
```

On Fedora, the official guide uses `sudo dnf install pipx`, followed by
`pipx ensurepath`. For other distributions, prefer the distribution's pipx
package. Systems enforcing PEP 668 may reject `pip install --user`; consult the
same official pipx guide for its self-managed virtual-environment fallback.

## Install arXiv Digest

Friends install from the canonical public HTTPS address:

```bash
pipx install git+https://github.com/yuzhangmath/arxiv-digest.git
```

The official [pipx CLI reference](https://pipx.pypa.io/stable/reference/cli.html)
documents VCS URLs as supported package specifications.

## Complete first-run setup

```bash
arxiv-digest init
```

The local dashboard guides you through categories, candidate papers,
interests, a standard or picker-selected PDF folder, and the optional desktop
launcher. Only checked suggestions and explicitly typed custom entries become
preferences. Search, navigation, and paging do not select anything.

## Use the app

Run this from any directory:

```bash
arxiv-digest
```

The desktop launcher is optional. It is a manual launch shortcut, not a login
item or scheduler. `arxiv-digest library` opens the library and
`arxiv-digest config` opens Settings.

## Upgrade

Back up first, then ask pipx to refresh the installed application:

```bash
arxiv-digest export arxiv-digest-backup.zip
pipx upgrade arxiv-digest
```

Open the app after upgrading so any bundled database migrations can run. If a
VCS upgrade cannot identify the source ref, reinstall the same canonical HTTPS
URL; portable application data is stored separately from the pipx environment.

## Uninstall

Remove the optional desktop launcher in Settings first, then run:

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

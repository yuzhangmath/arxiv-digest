# Installation

This guide installs arXiv Digest as an isolated command-line application on
macOS or Linux.

## Contents

- [Requirements](#requirements)
- [Install pipx on macOS](#install-pipx-on-macos)
- [Install pipx on Linux](#install-pipx-on-linux)
- [Install arXiv Digest](#install-arxiv-digest)
- [Upgrade within application-data generation 2](#upgrade-within-application-data-generation-2)
- [Automatic updates](#automatic-updates)
- [Complete first-run setup](#complete-first-run-setup)
- [Use the app](#use-the-app)
- [Clean reset with recovery copy](#clean-reset-with-recovery-copy)
- [Uninstall](#uninstall)
- [HTTPS and maintainer SSH](#https-and-maintainer-ssh)

This guide covers version 0.3.0.
The tagged commands require a published `v0.3.0` release. Existing users
should start with [the upgrade instructions](#upgrade-within-application-data-generation-2).

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
pipx install git+https://github.com/yuzhangmath/arxiv-digest.git@v0.3.0
```

The official [pipx CLI reference](https://pipx.pypa.io/stable/reference/cli.html)
documents VCS URLs as supported package specifications.

## Upgrade within application-data generation 2

Version 0.3.0 is a manual-bootstrap release. It uses the same application-data
generation, profile schema, and portable backup format as versions 0.2.0 and
0.2.1. Earlier clients cannot install this bootstrap automatically.

Confirm that `v0.3.0` appears on the
[releases page](https://github.com/yuzhangmath/arxiv-digest/releases) before
using the tagged install or upgrade commands in this guide.

### Choose the route for your installation

In a terminal, check the installed version and installation method:

```bash
arxiv-digest doctor
pipx list
```

| Existing installation | Upgrade route |
| --- | --- |
| 0.2.0 or 0.2.1 listed as `arxiv-digest` in `pipx list` | Follow the steps below, including installations originally made from a downloaded wheel or source folder |
| 0.2.0 or 0.2.1 installed with `pip` in a virtual environment | Follow the virtual-environment instructions below |
| 0.1.x or an application reporting an incompatible data generation | Use [Clean reset with recovery copy](#clean-reset-with-recovery-copy); generation-1 data cannot be migrated or imported |

If a diagnostic reports unfinished update recovery, complete the
[recovery procedure](troubleshooting.md#an-update-or-restart-did-not-finish)
before replacing the program. If you are unsure which copy is running, use
`command -v arxiv-digest` and compare it with `pipx list` or your virtual
environment's executable.

### Upgrade a pipx installation

1. Select **Quit** in the dashboard and wait for the terminal prompt to return.
   Fully quit any launcher-started copy too; closing the tab alone is not enough.
2. Export your current state with the old version before installing the new one:

   ```bash
   arxiv-digest export arxiv-digest-before-0.3.0.zip
   ```

   Run this in a private folder where you want to keep the backup. Wait for a
   successful export; if that filename already exists, choose a new filename.
   The backup contains your profile, Library, and review state. Downloaded PDFs
   remain in their existing folder and are not included in the backup.

   If you never started setup and `doctor` confirms that both the profile and
   database are missing, there is no initialized state to export: skip this
   step and run `arxiv-digest init` after upgrading. For any other export
   failure, stop and follow [backup help](data-and-backup.md#export-a-backup).
3. Replace the installed program using the explicit new tag:

   ```bash
   pipx install --force git+https://github.com/yuzhangmath/arxiv-digest.git@v0.3.0
   ```

   This requires Git and internet access. Use the same user and pipx environment
   as before. A plain `pipx upgrade arxiv-digest` can reuse the old pinned tag;
   use the command above to select 0.3.0 explicitly.
4. Verify the version, then reopen the app:

   ```bash
   arxiv-digest doctor
   arxiv-digest
   ```

   The first line of `doctor` output should be `arXiv Digest 0.3.0`. In the
   dashboard, check your interests, Library, review progress, and PDF folder.
   Users who completed setup should return to their dashboard without repeating it.
   If setup unexpectedly appears, quit and check the user account and data
   locations in [Data and backup](data-and-backup.md) before creating new state.
5. If you use the optional desktop launcher, quit again and refresh it:

   ```bash
   arxiv-digest install-launcher
   ```

   Then launch it once to verify it opens the updated app. This installs the
   recovery-aware launcher used by future automatic updates. If the app reports
   that the launcher was changed outside arXiv Digest, follow its guidance.

This replaces the pipx-managed program while leaving the generation-2 profile,
database, downloaded PDFs, and backup files in place. Do not perform a clean
reset or import a backup for this upgrade.

### Upgrade a virtual environment or downloaded source copy

Downloading a new ZIP or running `git pull` does not replace a separately
installed program. If you installed a wheel, ZIP, or source folder with pipx,
use the pipx steps above.

If you installed with `pip` into a virtual environment, quit and export with
that environment's `arxiv-digest` first. Then, using that same environment
(shown here as `.venv`), run:

```bash
.venv/bin/python -m pip install --upgrade git+https://github.com/yuzhangmath/arxiv-digest.git@v0.3.0
.venv/bin/arxiv-digest doctor
.venv/bin/arxiv-digest
```

Replace `.venv` with your environment's path and run without a source-tree
`PYTHONPATH` override. This also replaces an editable installation with the
tagged package; keep any local source changes separately. Verify the version
and saved state as above. Custom environments remain manual-update
installations. Contributor setup is documented in
[CONTRIBUTING](../CONTRIBUTING.md#development-setup).

## Automatic updates

After the manual 0.3.0 bootstrap, a later eligible release can offer **Update
and restart**. This requires macOS or Linux, pipx exactly 1.16.7 using its `pip`
backend, an unsuffixed per-user `arxiv-digest` environment, its original Python
interpreter, no injected or system-site packages, and completed setup. The
release must preserve the running Python policy, updater protocol,
application-data generation, and normalized runtime dependencies.

A documented canonical-tag installation is eligible for verification. After an
automatic install, a protected local provenance record and its retained wheel
establish the installed source for the next update. Arbitrary local wheels,
editable installs, custom environments, unsupported pipx versions, and missing
or changed provenance stay manual. A missing current wheel disables automatic
updating without preventing an otherwise healthy application from starting.

The updater downloads from the canonical GitHub release, checks the wheel
against its manifest, snapshots the complete environment, and creates a private
portable data backup before shutdown. Installation is offline and preserves the
interpreter and every nonapplication distribution. Package recovery restores
the verified snapshot; it does not reinstall an old wheel or fetch rollback
packages. Downloaded PDFs remain in place.

A managed recovery-aware desktop launcher reopens recovery before the normal
application when needed. Updating preserves the absence of a launcher and does
not overwrite a launcher that changed outside the application. Ordinary **Quit**
and update restart are separate actions. While an update is finishing, wait for
its outcome or follow explicit fully-quit guidance.

The new dashboard reports the final result. If preparation safely fails, retry
from the current dashboard; canceled PDF and corpus jobs require manual retry.
If restart or recovery remains unresolved, follow the
[fixed recovery command](troubleshooting.md#an-update-or-restart-did-not-finish).

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

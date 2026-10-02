# Contributing

Thank you for helping improve arXiv Digest. Keep fixtures synthetic: never add
personal paper collections, profiles, backups, messages, credentials, local
paths, or identifying Git metadata to a contribution.

## Contents

- [Development setup](#development-setup)
- [Test changes](#test-changes)
- [Release verification](#release-verification)
- [Privacy review](#privacy-review)
- [Pull requests](#pull-requests)

## Development setup

Open a terminal in the repository root. Use Python 3.11 or a newer supported
Python and Node.js 24.19.0 to create the development environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install --editable ".[dev]"
```

On Windows, run the equivalent commands in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --editable ".[dev]"
```

Use `.\.venv\Scripts\python.exe` wherever the Unix examples use
`.venv/bin/python`. Activating the environment is optional. Windows developer
machines need Git and a Playwright-supported Windows version (currently
Windows 11 or newer); see [Playwright requirements](https://playwright.dev/python/docs/intro#system-requirements).

Application tests deny non-loopback network access. Add deterministic local
fixtures for source behavior instead of contacting arXiv.

## Test changes

From the repository root, run the smallest relevant test while developing,
then run the offline suites below one command at a time:

```bash
export ARXIV_DIGEST_RELEASE_ARTIFACT_DIR="$(mktemp -d)"
PYTHONPATH=src .venv/bin/python -m build --outdir "$ARXIV_DIGEST_RELEASE_ARTIFACT_DIR"
.venv/bin/python -m pytest tests/unit tests/integration -q
node --test tests/js/*.test.mjs
.venv/bin/python -m playwright install chromium webkit
.venv/bin/python -m pytest tests/browser -q
.venv/bin/python -m pytest tests/integration/test_package_contents.py -q
.venv/bin/python scripts/privacy_scan.py tree . --expected-remote-from-project
.venv/bin/python scripts/privacy_scan.py history . --expected-remote-from-project
ARXIV_DIGEST_VERSION=$(.venv/bin/python -c 'from arxiv_digest import __version__; print(__version__)')
.venv/bin/python scripts/privacy_scan.py archive "$ARXIV_DIGEST_RELEASE_ARTIFACT_DIR/arxiv_digest-${ARXIV_DIGEST_VERSION}.tar.gz"
.venv/bin/python scripts/privacy_scan.py archive "$ARXIV_DIGEST_RELEASE_ARTIFACT_DIR/arxiv_digest-${ARXIV_DIGEST_VERSION}-py3-none-any.whl"
```

For PowerShell, create a fresh candidate directory and run the same suites:

```powershell
$env:ARXIV_DIGEST_RELEASE_ARTIFACT_DIR = Join-Path ([System.IO.Path]::GetTempPath()) ([System.Guid]::NewGuid().ToString())
.\.venv\Scripts\python.exe -m build --outdir $env:ARXIV_DIGEST_RELEASE_ARTIFACT_DIR
.\.venv\Scripts\python.exe -m pytest tests/unit tests/integration -q
node --test tests/js/*.test.mjs
.\.venv\Scripts\python.exe -m playwright install chromium webkit
.\.venv\Scripts\python.exe -m pytest tests/browser -q
.\.venv\Scripts\python.exe -m pytest tests/integration/test_package_contents.py -q
```

The privacy scanner commands also run with `.\.venv\Scripts\python.exe`.
Use `$env:ARXIV_DIGEST_RELEASE_ARTIFACT_DIR` for the candidate directory and
`$arxivDigestVersion = .\.venv\Scripts\python.exe -c 'from arxiv_digest import __version__; print(__version__)'`
for the filename's version. Quote full artifact paths in scanner arguments.

The tree command above assumes that the checkout's sole `origin` is the
maintainer SSH URL `git@github.com:yuzhangmath/arxiv-digest.git`. For a
canonical public HTTPS clone, replace `--expected-remote-from-project` with
`--expected-remote` followed by the exact canonical URL reported for `origin`
(usually `https://github.com/yuzhangmath/arxiv-digest.git`; omit `.git` only
when the recorded URL omits it). Reserve `--expect-no-remote` for an
intentional release-audit copy with no Git remote; it is not the command for an
ordinary clone.

The history scan inspects every reachable Git ref. Run the release history gate
from a clean clone containing only the branches and tags intended for public
distribution; local editor or agent checkpoint refs are not publishable
history and can cause a whole-repository scan to report private working data.

Browser changes must pass both Chromium and WebKit. Packaging changes must
preserve the exact migration, static-asset, license, and notice inventories.

## Release verification

Use Python 3.11 or newer and Node.js 24.19.0. Keep installation smoke tests in
wholly temporary synthetic HOME/XDG, Windows AppData, and pipx directories. Application tests
deny non-loopback network access; never use the installed personal application
as a fixture.

Keep `ARXIV_DIGEST_RELEASE_ARTIFACT_DIR` set to the exact fresh candidate build
for all artifact checks. An explicit value must be absolute; tests never fall
back to ignored `dist` artifacts when it is set. Ordinary developer use may
omit it and use `dist`. Rebuild when source changes invalidate the candidate,
and validate the wheel, sdist, exact package resources, checksums, release
notes, and commit identity against that same candidate.

Release validation is configured for macOS, Windows, and Linux with Python
3.11 and the current Python release. Both browser engines are required on
each platform. CI uses Git Bash for consistent workflow syntax and native
Python on Windows, preserves LF checkout bytes for artifact comparisons, and
smoke-tests the native `arxiv-digest.exe` installed by pipx. Follow
`.github/workflows/` for the single candidate build, source/tag/channel checks,
privacy gates, isolated pipx smoke, and `SHA256SUMS`. Report unavailable native
coverage explicitly; mocked platform branches do not establish readiness.

The read-only build job produces the candidate. The write-enabled publish job
verifies downloaded bytes with its embedded standard-library verifier and
never executes downloaded project code. Existing releases are verified without
modification. Do not stage, commit, tag, publish, or upload generated artifacts
without authorization.

`arxiv-digest doctor` must remain read-only for profiles and databases and make
no network requests. Fresh-install smoke must leave no profile, database, or
running-server descriptor.

## Privacy review

Keep machine-specific audit reports and scratch notes in the ignored
`docs/local/` directory. Local design and implementation plans remain in the
ignored `docs/superpowers/` directory. Neither belongs in release artifacts,
and public documentation must not link to these local files.

The generic tree and archive scans are necessary but do not replace the
private-derived release audit performed by the maintainer. Scanner diagnostics
must name only a relative path, artifact class, and opaque rule identifier;
they must never echo a matched value. Do not weaken a rule to accommodate a
real private artifact. Rewrite synthetic test data so no realistic private
value is stored in the repository.

The private-derived audit compares the public candidate with an explicitly
identified private predecessor's collection, preferences, paths, and Git
metadata. Use that private checkout as the first argument to
`scripts/privacy_scan.py audit-private`, and the public checkout as the second;
the scanner reads private data through temporary snapshots. Keep its findings
in the ignored local audit record. A missing private source is an unavailable
comparison, not a passing check. When auditing a temporary public-history clone,
configure that clone with the established public repository identity; do not
override the private source's identity through shared `GIT_AUTHOR_*` or
`GIT_COMMITTER_*` environment variables.

## Pull requests

Explain observable behavior, tests run, platform-specific implications, and
any remaining limitation. Keep changes focused. Do not add telemetry,
background scheduling, remote accounts, deployment credentials, or live-network
tests without an approved design and explicit review.

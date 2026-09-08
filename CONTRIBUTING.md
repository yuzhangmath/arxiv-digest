# Contributing

Thank you for helping improve arXiv Digest. Keep fixtures synthetic: never add
personal paper collections, profiles, backups, messages, credentials, local
paths, or identifying Git metadata to a contribution.

## Contents

- [Development setup](#development-setup)
- [Test changes](#test-changes)
- [Updater and release verification](#updater-and-release-verification)
- [Privacy review](#privacy-review)
- [Pull requests](#pull-requests)

## Development setup

Open a terminal in the repository root. Use Python 3.11 or a newer supported
Python and Node.js 24.19.0 to create the development environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install --editable ".[dev]"
```

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

## Updater and release verification

Use Python 3.11 or newer and Node.js 24.19.0. Automatic installation supports
only pipx 1.16.7 with its `pip` backend. Run installer, interruption, snapshot
replay, inherited-lock, and relaunch tests in wholly temporary synthetic pipx,
HOME/XDG, cache, log, trash, shared, bin, man, completion, and temporary roots.
Application tests continue to deny non-loopback network access. Never run a
production updater against the installed personal application as a fixture.

Keep `ARXIV_DIGEST_RELEASE_ARTIFACT_DIR` set to the exact fresh candidate build
for all artifact checks. An explicit value must be absolute; tests never fall
back to ignored `dist` artifacts when it is set. Ordinary developer use may
omit it and use `dist`. Rebuild only when candidate source changes invalidate
the artifact evidence, and validate wheel, sdist, exact package resources,
manifest, checksums, notes, and commit identity against the same candidate.

The 0.3.0 release is a manual bootstrap: its manifest has updater protocol 1,
application-data generation 2, `automatic_update: false`, and
`automatic_update_from: null`. Verify the actual 0.2.1 manual upgrade separately
from clean-wheel and public-CLI smoke. Synthetic consecutive
0.3.0 → 0.3.1 → 0.3.2 coverage must use real canonical-tag bootstrap metadata,
production discovery/coordinator/helper/recovery, and protected provenance; a
hand-authored eligibility record or a mocked installation is not that evidence.

Both macOS and Linux native installation/recovery results are required before
release, using Python 3.11 and the current Python release on each platform.
The release workflow builds one candidate bundle, then all four native jobs
validate that bundle at its verified source commit. Publication depends on
every native job succeeding. Report unavailable native coverage explicitly.
Passing mocked platform branches does not establish native platform readiness.

Follow the maintained workflows in `.github/workflows/` for the single isolated
build, verified source/tag/channel identity, privacy gates, pipx smoke,
`UPDATE_MANIFEST.json`, and `SHA256SUMS`. The read-only build job produces the
candidate. The write-enabled publish job verifies downloaded bytes using only
its embedded standard-library verifier and never executes downloaded project
code. Existing releases are verified without modification. Do not stage,
commit, tag, publish, or upload generated artifacts without authorization.

Release requirement: `arxiv-digest doctor` must remain read-only for profiles,
databases, and network access. Pending or blocked update recovery must produce
only redacted status, without running recovery or exposing local paths.
Updater preflight can initialize private
coordination directories and lock files; fresh-install smoke should allow that
while still requiring no profile, database, or running-server descriptor.

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

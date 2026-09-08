# Security policy

arXiv Digest is local software that handles interests, reading history, and
download locations. Treat profiles, databases, portable backups, and runtime
URLs as sensitive.

## Contents

- [Supported version](#supported-version)
- [Report a vulnerability privately](#report-a-vulnerability-privately)
- [Non-sensitive problems](#non-sensitive-problems)
- [Update trust and recovery](#update-trust-and-recovery)
- [Scope and expectations](#scope-and-expectations)

## Supported version

Security fixes target the current `0.3.x` line until a newer public release
policy is documented.

## Report a vulnerability privately

Use the repository's **Security** tab and open a private vulnerability report
for `yuzhangmath/arxiv-digest`. Do not open a public issue for an unpatched
vulnerability. If private reporting is temporarily unavailable, contact the
maintainer through their GitHub profile without including exploit details and
ask for a private channel.

Include the affected version, operating system, impact, minimal reproduction,
and whether the issue exposes local data or escapes the loopback boundary.
Remove paper metadata, paths, tokens, credentials, backups, and personal Git
identity from the report unless they are indispensable; replace them with
synthetic values whenever possible.

## Non-sensitive problems

Ordinary bugs with no security or privacy impact may use the public issue
tracker. Open a terminal, run `arxiv-digest doctor`, and attach only its
redacted output. An unfinished update produces a pending or blocked recovery
status without attempting recovery or exposing local paths. See
[Run redacted diagnostics](docs/troubleshooting.md#run-redacted-diagnostics).

## Update trust and recovery

Automatic updating trusts the canonical GitHub repository and its release
assets. Release manifests bind wheel names, versions, sizes, and SHA-256
hashes; HTTPS and repository access controls remain part of the trust boundary.
Checksums do not establish an independent publisher signature.

The updater supports a narrow verified pipx configuration and never accepts
commands, filesystem paths, hashes, or download URLs from browser update
requests. Private plans, provenance, journals, snapshots, backups, copied
recovery programs, and logs are sensitive local state. Terminal receipts live
only in the atomic journal. The coordinator, helper, installer guard, and
quarantined child use authenticated bounded control messages and explicit lock
ownership; a borrowed lock descriptor must close without unlocking another
owner's reference.

Useful updater reports include eligibility bypass, wheel or plan replacement,
unsafe snapshot replay, premature launcher or lock release, state-machine
confusion, unauthorized internal startup, data-recovery overwrite, and leaked
private diagnostics. Use synthetic environments and stop before changing a
real personal installation. A malformed or ambiguous recovery journal must
refuse normal startup rather than authorize a guessed recovery.

## Scope and expectations

Particularly useful reports cover authentication bypass, non-loopback network
exposure, unsafe archive handling, path traversal, command execution, private
data disclosure, or unsafe rendering of paper-controlled text. Please allow a
reasonable remediation window before public disclosure. The project does not
offer a bug bounty.

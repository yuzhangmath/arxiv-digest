# Security policy

arXiv Digest is local software that handles interests, reading history, and
download locations. Treat profiles, databases, portable backups, and runtime
URLs as sensitive.

## Supported version

Security fixes target the current `0.1.x` line until a newer public release
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
tracker. Run `arxiv-digest doctor` and attach only its redacted output.

## Scope and expectations

Particularly useful reports cover authentication bypass, non-loopback network
exposure, unsafe archive handling, path traversal, command execution, private
data disclosure, or unsafe rendering of paper-controlled text. Please allow a
reasonable remediation window before public disclosure. The project does not
offer a bug bounty.

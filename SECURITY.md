# Security

Please report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab) on
this repository. Do not open a public issue.

Scope and threat model are described in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#security-model).
In short: gq trusts your laptop and your own account on the cluster. It defends against injection via
config or discovered values, against other users on a shared filesystem, and against accidental quota
spending by coding agents (best-effort). It does not try to contain a malicious agent that already
runs as you.

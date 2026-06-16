# Security policy

## Supported versions

tracesage is currently in v0.2 alpha. Only the latest minor release receives
security fixes. Once v1.0 ships, the previous minor will receive fixes for
6 months after the next release.

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | yes (alpha)        |
| < 0.1   | no                 |

## Reporting a vulnerability

**Please do not open a public issue for security reports.**

Use GitHub's [Private Vulnerability Reporting](https://github.com/kjgpta/tracesage/security/advisories/new)
to send a confidential report. If that is unavailable, email
**kjgpta+tracesage-security@users.noreply.github.com** with:

- A description of the issue and its impact
- Steps to reproduce (a minimal proof-of-concept is ideal)
- Affected versions, if known
- Your suggested mitigation, if any

## Response timeline

- **Acknowledgement** within 3 business days
- **Initial assessment** (severity + scope) within 7 business days
- **Fix or mitigation** for high-severity issues within 30 days; lower-severity
  issues are scheduled into the next release
- **Public disclosure** coordinated with the reporter; CVE assigned if
  applicable

## Scope

In-scope:

- The tracesage Python package itself (`src/tracesage/`)
- The embedded server (FastAPI app, REST + WebSocket endpoints)
- The on-disk storage format (SQLite + gzipped blobs)
- The CLI (`tracesage` command)
- Authentication and authorization paths

Out of scope:

- Third-party dependencies — please report to the upstream project. We will
  upgrade once a fix is available.
- LangChain / LangGraph behaviour itself — only the tracesage adapter is in scope.
- Local-only attacks where the attacker already has shell access to the host.

## Hardening recommendations

If you operate tracesage in production, please review
[`docs/production.md`](../docs/production.md)
which covers:

- The hard fail-stop on non-loopback binding without `auth_token`
- Constant-time bearer-token comparison
- Path-traversal guards on blob reads
- Sampling and per-run event caps as denial-of-service mitigations

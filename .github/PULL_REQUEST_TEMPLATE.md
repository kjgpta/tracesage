<!--
Thanks for sending a PR! Please fill in the sections below.
- Keep the title in imperative mood, present tense, no period
  (e.g. "Add severity override for hardcoded secrets", not "Added ...").
- For non-trivial changes, please open an issue first to align on approach.
-->

## Summary

<!-- One-paragraph description of what this PR does and why. -->

## Type of change

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing behaviour to change)
- [ ] Documentation only
- [ ] Internal / refactor (no user-visible change)

## Related issues

<!-- "Closes #123", "Refs #456". Required for non-trivial changes. -->

## Test plan

<!-- How did you verify this works? Use the boxes below as a starter checklist. -->

- [ ] `python -m py_compile` passes on every modified Python file
- [ ] `ruff check src/ tests/ tools/` is clean
- [ ] Targeted test passes: `pytest tests/test_<your_module>.py`
- [ ] Full suite passes: `pytest tests/ --ignore=tests/stress`
- [ ] No new `ResourceWarning` during tests (no leaked DB connections / async tasks)
- [ ] Manual smoke test against an example, if user-facing

## Public-API changes

<!-- If you added/modified/removed anything exported from `tracesage`, list it here. -->

- New: …
- Changed: …
- Removed: …

## Screenshots / output

<!-- For UI changes: before/after screenshots. For CLI changes: paste the new output. Otherwise, skip. -->

## Checklist

- [ ] I have read [`CONTRIBUTING.md`](../blob/main/docs/contributing.md)
- [ ] I have updated `docs/changelog.md` under the next unreleased version
- [ ] I have updated docs in `docs/` if user-visible behaviour changed
- [ ] My commits follow the project's [convention](../blob/main/docs/contributing.md#commit-style)
# Security policy

## Supported versions

tracesage is currently in v0.1 alpha. Only the latest minor release receives
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

# Releasing tracesage

End-to-end runbook for publishing tracesage — from a fresh clone to a live
package on PyPI, with the GitHub repo, GitHub Pages docs, and a tagged
release all set up correctly.

The first release takes ~30 minutes, mostly waiting for PyPI verification.
Subsequent releases are 3 commands once everything's wired up.

---

## Phase 0 — Pre-flight

### 0.1 Verify the package name is available on PyPI

Open https://pypi.org/project/tracesage/ in a browser.

- **404 / "Project not found"** → the name is free. Continue.
- **Package listing exists** → the name is taken. Either pick a different
  name in `pyproject.toml` (`tracesage-py`, `agent-tracesage`, etc.) and
  update references throughout the codebase, or contact the existing
  owner if the project looks abandoned.

This is the first thing to check. Everything else builds on the assumption
that you can claim the name.

### 0.2 Tooling

```bash
python -m pip install --upgrade pip build twine
python -m pip install -e ".[dev,langchain]"
```

You only need `build` and `twine` for local verification — the actual
PyPI upload runs in GitHub Actions.

---

## Phase 1 — One-time GitHub setup

### 1.1 Create the repo on GitHub

Go to https://github.com/new:

- **Owner:** `kjgpta`
- **Repository name:** `tracesage`
- **Visibility:** Public (required for free GitHub Pages + free Trusted Publishing)
- **Initialize:** **leave all checkboxes unchecked.** Your local repo
  already has README, LICENSE, and .gitignore.

Click *Create repository*. Don't follow GitHub's auto-suggested commands
— do the steps below instead.

### 1.2 First push from your local repo

```bash
cd C:/Users/KSGUPTA/tracesage

# Inspect what's about to be committed.
git status
git diff --stat

# Stage and commit. Group into a few logical commits if you want; here's
# a one-shot version for the first push:
git add .
git commit -m "Add integration guide, GitHub hygiene, MkDocs site, concepts doc"

# Wire up the remote and push.
git remote add origin git@github.com:kjgpta/tracesage.git
git branch -M main
git push -u origin main
```

After this:

- Your code is on GitHub
- `ci.yml` workflow runs on the push (lint + tests on 3 OSes × 3 Python versions)
- `docs.yml` workflow tries to deploy GitHub Pages but will fail until
  Pages is enabled (next step)

### 1.3 Configure repo settings

In the GitHub web UI under **Settings**:

#### General → Features
- Enable **Issues**
- Enable **Discussions**, then create the categories the issue templates
  link to (`Q&A`, `Ideas`, `Show & tell`). Discussions → New category.
- Disable **Wiki** (you have `docs/`)

#### Branches
- Add a branch-protection rule for `main`:
  - Require a pull request before merging
  - Require status checks: `lint`, plus the `test (...)` checks once
    they've run at least once and appear in the list
  - Require linear history
  - (Optional) Require signed commits

#### Pages
- **Source: GitHub Actions**
- That's the only setting. Your `docs.yml` workflow will deploy on the
  next push to `main`. Check https://kjgpta.github.io/tracesage/ in
  ~60 seconds after the workflow goes green.

#### Environments
- Click **New environment**, name it `pypi` (must match the
  `environment: pypi` line in `.github/workflows/release.yml`)
- (Optional but recommended) **Required reviewers**: add yourself.
  This means a manual approval click is required before any tag push
  actually publishes to PyPI — a safety net against accidental tag pushes.

#### Secrets and variables → Actions
- Nothing to set right now. Trusted Publishing uses OIDC, no API tokens.

---

## Phase 2 — One-time PyPI setup

### 2.1 Create the PyPI account

https://pypi.org/account/register/

- Use a real email (verification required)
- 2FA is mandatory for new accounts. Use an authenticator app
  (Google Authenticator, 1Password, Bitwarden, etc.).
- Save your recovery codes somewhere safe.

(Optional) Repeat the same on https://test.pypi.org for a sandbox.

### 2.2 Configure Trusted Publishing on PyPI

Trusted Publishing is the modern, token-less way to publish from GitHub
Actions. PyPI verifies that the upload request came from this exact
workflow in this exact repo's `pypi` environment, via short-lived OIDC
tokens minted by GitHub. No API keys to leak.

1. Go to https://pypi.org/manage/account/publishing/
2. Click **Add a new pending publisher**
3. Fill in:
   - **PyPI Project Name:** `tracesage`
   - **Owner:** `kjgpta`
   - **Repository name:** `tracesage`
   - **Workflow name:** `release.yml`
   - **Environment name:** `pypi`
4. Save.

The "pending" wording means the package doesn't exist on PyPI yet. After
your first publish it converts to a regular publisher automatically.

> All four fields must match exactly. If any value differs from what GitHub
> sends in the OIDC claim, the upload will fail with a publisher-not-found
> error.

---

## Phase 3 — Pre-release checks

These verify the build is clean before you cut a tag. Tagging triggers
the actual publish, so you want this part to be boring.

### 3.1 Confirm version + changelog

```bash
# pyproject.toml — version is the source of truth
grep -E '^version\s*=' pyproject.toml
# version = "0.1.1"

# CHANGELOG.md — should have a "## [0.1.1]" section with the release notes,
# AND a "## [Unreleased]" placeholder above it for the next iteration.
```

If you bump the version in pyproject.toml, also bump it in the changelog
heading and the `## [Unreleased]` rollover.

### 3.2 Run the tests

```bash
pytest tests/ --ignore=tests/stress -v
```

Should be all green. CI runs the same on PRs, but a clean local run
before tagging is good hygiene.

### 3.3 Build artifacts locally

```bash
# Wipe any previous builds.
rm -rf dist/ build/ src/*.egg-info/

# Build both sdist and wheel via hatchling (configured in pyproject.toml).
python -m build
```

You should see:

```
dist/
├── tracesage-0.1.1.tar.gz             # sdist
└── tracesage-0.1.1-py3-none-any.whl   # wheel
```

### 3.4 Lint with twine

```bash
twine check dist/*
```

Catches:

- README rendering issues (PyPI uses your `README.md` as `long_description`)
- Missing/invalid metadata
- Bad classifiers

If anything fails, fix the source and re-build.

### 3.5 Inspect the wheel contents

```bash
unzip -l dist/tracesage-*.whl | head -40     # the WHEEL: lean, runtime only
tar -tzf dist/tracesage-*.tar.gz | head -40   # the SDIST: also ships sources/docs
```

The **wheel** (what `pip install` puts on a user's machine) should be lean:

- ✓ `tracesage/*.py` and `tracesage/py.typed`
- ✓ `tracesage/ui/*.html`, `*.js`, `*.css`
- ✗ no `tests/`, `examples/`, `docs/`, `tools/`, `.github/`
- ✗ no `__pycache__`, `.pyc`, `.env`

The **sdist** (source distribution) intentionally includes more so the repo is
reproducible from PyPI: `src/`, `tests/`, `examples/`, `docs/`, `README.md`,
`LICENSE`, `CHANGELOG.md`, `CONTRIBUTING.md`, `RELEASING.md`, `production_roadmap.md`.
The include lists live under `[tool.hatch.build.targets.wheel]` /
`[tool.hatch.build.targets.sdist]` in `pyproject.toml`.

If unwanted files are bundled, edit
`[tool.hatch.build.targets.wheel]` / `[tool.hatch.build.targets.sdist]`
in `pyproject.toml` and rebuild.

### 3.6 Test-install the built wheel

```bash
# A fresh venv, isolated from your dev env.
python -m venv /tmp/tl-test
source /tmp/tl-test/bin/activate    # PowerShell: .\Scripts\Activate.ps1
                                    # bash on Windows: source /tmp/tl-test/Scripts/activate

pip install "dist/tracesage-0.1.0-py3-none-any.whl[langchain]"

# Smoke checks.
python -c "from tracesage import TraceSage; print('OK')"
tracesage version
tracesage --help

deactivate
rm -rf /tmp/tl-test
```

### 3.7 (Optional) Dry-run on TestPyPI

If you want to see the rendered PyPI page before going to prod:

```bash
# Get a TestPyPI API token: https://test.pypi.org/manage/account/token/
# Save it (don't commit):
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=pypi-AgEIcDExNT...

twine upload --repository testpypi dist/*

# Verify the listing renders correctly:
# https://test.pypi.org/project/tracesage/

# Optional: install from TestPyPI to test the install-side experience.
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ \
            tracesage
```

You don't need to repeat this every release — only when something feels
off about the build.

---

## Phase 4 — Cut the release

### 4.1 Commit the version bump

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "Release v0.1.1"
git push origin main
```

CI runs again on this push. Wait until it's green before tagging.

### 4.2 Tag and push

```bash
# Annotated tag (preferred over lightweight).
git tag -a v0.1.1 -m "v0.1.1"
git push origin v0.1.1
```

This is the moment of truth. Pushing a tag matching `v*` triggers
`.github/workflows/release.yml`:

1. **`build` job** — runs `python -m build`, produces sdist + wheel,
   uploads them as a workflow artifact.
2. **`publish-pypi` job** — enters the `pypi` environment (waits for
   manual approval if you set required reviewers), downloads the
   artifact, runs `pypa/gh-action-pypi-publish` which uses OIDC to
   request a short-lived PyPI credential and uploads.

Watch the run at:
https://github.com/kjgpta/tracesage/actions/workflows/release.yml

### 4.2b Manual upload (alternative to Trusted Publishing)

If you haven't set up Trusted Publishing (Phase 2.2), or you just want to push from your
machine instead of CI, upload the built artifacts directly with **twine**:

```bash
# 1. Build fresh (rm ensures dist/ holds only the version you intend to release —
#    PyPI is immutable; you can't overwrite a version).
rm -rf dist/ build/ && python -m build

# 2. Get a PyPI API token: https://pypi.org/manage/account/token/
#    (after the first upload, re-scope it to just the `tracesage` project).
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=pypi-AgEIcD...        # your token — NEVER commit it

# 3. Push to PyPI. This uploads BOTH the sdist (.tar.gz) and the wheel (.whl).
twine upload dist/*
```

`twine upload dist/*` is the actual "push to PyPI" command. Do a dry run on TestPyPI
first (Phase 3.7) if you want to preview the rendered page. Trusted Publishing (4.2) is
preferred for repeatable releases because it needs no long-lived token; use this manual
path for one-offs or when CI isn't available.

### 4.3 Verify on PyPI

```bash
# Wait ~30s after the workflow finishes — PyPI's CDN is async.
pip install tracesage==0.1.1

# Or check the listing:
# https://pypi.org/project/tracesage/0.1.1/
```

Take a moment to read the rendered README on the PyPI page. If anything
looks wrong (broken images, formatting glitches), they'll be visible
here.

### 4.4 Create a GitHub Release

PyPI publishes the package; GitHub Releases publish the **announcement**.
They're separate but both come from the same tag.

```bash
# Easiest: use the gh CLI.
gh release create v0.1.1 \
  --title "v0.1.1" \
  --notes-file - <<'EOF'
First public release of tracesage.

## Highlights

- LangChain + LangGraph adapter — callback-driven, never raises
- Embedded FastAPI UI at `http://localhost:7842`
- SQLite + gzipped blob storage, no external infra
- 30-app before/after [examples gallery](https://kjgpta.github.io/tracesage/examples/)

## Install

```bash
pip install tracesage[langchain]
```

See the [docs](https://kjgpta.github.io/tracesage/) for the full quickstart.
EOF
```

Or use the web UI: https://github.com/kjgpta/tracesage/releases/new

- Choose the existing tag `v0.1.1`
- Title: `v0.1.1`
- Description: paste from your `docs/changelog.md` `[0.1.1]` section
- Click *Publish release*

GitHub auto-attaches the source `.tar.gz` and `.zip` from the tag.

### 4.5 Confirm GitHub Pages is live

After the first push to `main` with `mkdocs.yml`, the `docs.yml` workflow
deploys to GitHub Pages. Verify:

- https://github.com/kjgpta/tracesage/actions/workflows/docs.yml — last
  run should be green
- https://kjgpta.github.io/tracesage/ — should load

If 404: **Settings → Pages → Source: GitHub Actions** must be set. If
already set, give the CDN another minute and hard-refresh.

---

## Phase 5 — After the first release

### 5.1 Bump for next development cycle

```bash
# pyproject.toml: version = "0.1.1"  (patch) or "0.2.0" (minor)
# CHANGELOG.md: add a fresh "## [Unreleased]" section at the top.

git commit -am "Bump to 0.1.1-dev"
git push
```

### 5.2 Subsequent releases

Same as **Phase 4** with the new version. Trusted Publishing converts
from "pending" to "regular" after the first publish — future releases
are just:

```bash
# Update version in pyproject.toml + CHANGELOG.md
git commit -am "Release v0.1.1"
git push
git tag -a v0.1.1 -m "v0.1.1"
git push origin v0.1.1
gh release create v0.1.1 ...
```

### 5.3 Versioning policy

[Semantic versioning](https://semver.org), with the pre-1.0 carve-out:

- **0.1.x** — patches, bug fixes only
- **0.x.0** — minor, may include breaking changes (you're pre-1.0)
- **1.0.0** — first stable. After 1.0, breaking changes only on major bumps

---

## Troubleshooting

### "File already exists" on PyPI upload
PyPI is immutable. You can't re-upload the same version. Either:

- Bump the patch (`0.1.1` → `0.1.2`) and try again
- Yank the broken release: PyPI project page → *Manage* → *Releases* → *Yank*.
  Yanked releases stay listed but `pip install` won't pick them by default.

### "OIDC token verification failed" / "publisher not found"
Trusted Publishing config mismatch. Check that **all four fields** on the
PyPI publisher match what GitHub sends:

- Owner exactly: `kjgpta` (case-sensitive)
- Repo exactly: `tracesage`
- Workflow exactly: `release.yml`
- Environment exactly: `pypi`

If you renamed any of these, fix the publisher config on PyPI.

### "Permission denied" when the workflow tries to publish
The `pypi` environment doesn't exist in **Settings → Environments**.
Create it.

### CI fails on Windows but passes on Linux/macOS
Common causes (per `CLAUDE.md`):

- Encoding (cp1252 doesn't have non-ASCII chars — use ASCII in print
  statements that go to the console)
- Path separator hardcoded to `/` somewhere — use `pathlib.Path`
- `subprocess` without explicit `encoding="utf-8"`

### Docs site shows old content
The Pages CDN caches aggressively. After the `docs.yml` workflow finishes:

- Wait 60 seconds
- Hard-refresh (Cmd-Shift-R / Ctrl-Shift-R)
- Check the `actions/deploy-pages@v4` step output for the deployment URL

### Pre-release versions
For RC / beta releases, use a PEP 440-compatible suffix:

```
version = "0.1.1rc1"   # release candidate
version = "0.1.1b1"    # beta
version = "0.1.1a1"    # alpha
version = "0.1.1.dev1" # dev / nightly
```

Tag the same way: `git tag v0.1.1rc1`. PyPI won't show pre-releases on
the project page by default; users need `pip install tracesage --pre`
to install them.

### Yanking a bad release
- PyPI project page → *Manage* → *Releases* → click the version → *Yank release*
- Provide a reason (will be shown to anyone trying to install the version)
- Yanked versions stay installable via explicit pin (`pip install tracesage==0.1.1`)
  but won't be picked by version solvers

### Removing a release entirely (rare)
PyPI permits deletion via the project Manage page, but **don't** unless
the release contained secrets or was published in error. Yank instead.

---

## What you don't need to do

- ❌ `setup.py` — you have `pyproject.toml` (PEP 621). Hatchling reads it.
- ❌ `MANIFEST.in` — `[tool.hatch.build.targets.{wheel,sdist}]` in
  `pyproject.toml` controls inclusion.
- ❌ `requirements.txt` — dependencies live in `pyproject.toml`.
- ❌ Long-lived PyPI API tokens in repo secrets — Trusted Publishing
  replaces them with per-run OIDC.
- ❌ Manual `twine upload dist/*` from your laptop — that's the workflow's
  job. Use it only as the TestPyPI dry-run from §3.7.

---

## Quick reference card

```bash
# First-time setup (do once):
# 1. Create repo at https://github.com/new (kjgpta/tracesage, public)
# 2. PyPI account + Trusted Publisher config at
#    https://pypi.org/manage/account/publishing/
# 3. GitHub: Settings → Environments → create "pypi"
# 4. GitHub: Settings → Pages → Source = GitHub Actions

# First push:
git remote add origin git@github.com:kjgpta/tracesage.git
git branch -M main
git push -u origin main

# Each release:
# 1. Bump version in pyproject.toml + CHANGELOG.md
# 2. Commit and push to main; wait for CI to go green
# 3. Tag and push:
git tag -a v0.1.1 -m "v0.1.1"
git push origin v0.1.1
# 4. Create GitHub Release (gh release create v0.1.1 ...)
```

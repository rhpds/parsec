# Dockerfile Hardening & Quay.io Image Publishing

**Date:** 2026-06-04
**Status:** Implemented
**Scope:** Security hardening of the container image + GitHub Actions workflow for versioned image publishing to Quay.io

## Problem

The Parsec Docker image uses `ubi9/python-311:latest` as its base, producing a 1.99 GB image with 473 RPMs, 52 devel packages, gcc, pip, and libpq — none of which are needed at runtime. The image is built via OpenShift BuildConfig triggered by GitHub webhooks, outputs to an ImageStream tagged `:latest` with no version control, and offers no rollback mechanism.

## Goals

1. Reduce image size and attack surface by migrating to `ubi9-minimal` and removing unnecessary packages
2. Publish versioned images to Quay.io via GitHub Actions, coexisting with the current BuildConfig until validated
3. Zero changes to application code, `requirements.txt`, CI pipeline, or developer workflow

## Non-Goals

- Python version upgrade (staying on 3.11)
- Replacing pip with uv in the project workflow
- Decoupling mlflow from the image
- Modifying the existing `ci.yml` workflow
- Changing Helm chart default values (BuildConfig stays enabled by default)

## Key Discovery: libpq Is Unnecessary

Investigation confirmed that `libpq`, `gcc`, `gcc-c++`, and all 52 devel packages in the current image are unused:

- No Python package in `requirements.txt` depends on `libpq`
- No imports of `psycopg2`, `asyncpg`, or `sqlalchemy` exist in the codebase
- All PostgreSQL access goes through the Reporting MCP server via HTTP
- All 14 packages with C extensions have pre-built `manylinux` wheels — zero compilation needed

This eliminates the need for any build toolchain in the image.

## Image Baseline & Target

| Metric          | Baseline (current) | Hardened (actual) | Delta   |
| --------------- | ------------------ | ----------------- | ------- |
| Image size      | 1.99 GB            | 1.07 GB           | -46%    |
| RPMs            | 473                | 118               | -75%    |
| Devel packages  | 52                 | 0                 | -100%   |
| Python          | 3.11.13            | 3.11.13           | --      |
| pip             | present            | not found         | removed |
| gcc             | present            | not found         | removed |
| Package manager | dnf (full)         | microdnf (minimal)| reduced |

Image size is 1.07 GB (above the ~850 MB estimate) due to mlflow's heavy dependency tree (scipy, numpy, scikit-learn, protobuf, grpcio). Decoupling mlflow would bring the image closer to the 850 MB target but was excluded from scope to avoid invasive changes.

## Design

### 1. Dockerfile Hardening

Rewrite `dockerfiles/Dockerfile` as a clean multi-stage build on `ubi9-minimal`.

**Builder stage:**

- Base: `registry.access.redhat.com/ubi9-minimal:9.8`
- Install `python3.11` and `python3.11-pip` via `microdnf`
- Create a virtualenv at `/opt/app-root/venv` and `pip install --no-cache-dir -r requirements.txt`
- No gcc, no libpq-devel, no S2I scripts

**Runtime stage:**

- Base: `registry.access.redhat.com/ubi9-minimal:9.8`
- Install only `python3.11` via `microdnf` (no pip)
- `microdnf update -y` for security patches
- Create `python3` and `python` symlinks to `python3.11` via `ln -sf` (required by Helm CronJobs, init containers, and script shebangs; `-sf` ensures idempotent builds if a future UBI update ships `/usr/bin/python3`)
- Copy `.venv` from builder — only runtime dependency
- Copy application code: `src/`, `static/`, `config/config.yaml`, `config/prompts/`, `data/ec2_pricing.json`, `scripts/`
- Create required directories: `/app/data/reports`, `/app/data/debug`
- `USER 1001`, `chown 1001:0`, `chmod g+rw` (OpenShift-compatible)
- Environment: `PYTHONPATH=/app`, `PYTHONUNBUFFERED=1`
- Dependency smoke test: imports 7 critical packages (dynaconf, fastapi, uvicorn, anthropic, boto3, pydantic, httpx)
- Healthcheck on `/api/health` with 5s timeout
- CMD: `uvicorn src.app:app --host 0.0.0.0 --port 8000`

**BuildConfig compatibility:** The OpenShift BuildConfig uses Docker strategy (`strategy.type: Docker`), not S2I strategy. It runs `docker build` with the Dockerfile. Removing S2I scripts from inside the Dockerfile does not affect the BuildConfig — it will continue to work transparently.

### 2. .dockerignore

Create `.dockerignore` to reduce build context and prevent sensitive file leakage:

```
.git/
.venv/
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.ruff_cache/
playbooks/
helm/
docs/
tests/
.github/
.cursor/
logs/
plans/
*.md
!config/prompts/*.md
!config/prompts/**/*.md
data/reports/
data/debug/
config/config.local.yaml
.env
**/*.kubeconfig
```

### 3. GitHub Actions Workflow

Create `.github/workflows/publish.yaml` — independent from the existing `ci.yml`.

**Triggers:**

- `push: tags: ['v[0-9]*.[0-9]*.[0-9]*']` — strict semver, triggered by `bump-version.sh`
- `workflow_dispatch: inputs: version` — manual trigger with version input

**Steps:**

1. Checkout repository
2. Validate IMAGE_REGISTRY and IMAGE_REPOSITORY vars are set (fail-fast)
3. Extract semantic tags from version (e.g., `v0.1.2` produces `latest`, `v0.1`, `v0.1.2`)
4. Validate semver format for workflow_dispatch inputs
5. Verify `helm/Chart.yaml` `appVersion` matches the tag (tag events only)
6. Setup Docker Buildx
7. Login to Quay.io
8. Build and push with GHA layer cache

**Security hardening:**

- `permissions: contents: read` — least privilege, no write access to repo
- User inputs passed via `env:` block, not `${{ }}` interpolation — prevents script injection
- Semver format validated for both manual dispatch inputs and tag-push events
- All `${{ }}` expressions passed via `env:` blocks — no direct interpolation in shell scripts

**Image tags published:**

```
quay.io/rhpds/parsec:latest
quay.io/rhpds/parsec:v0.1       # major.minor
quay.io/rhpds/parsec:v0.1.2     # full semver
```

**GitHub Secrets/Variables required:**

| Name                | Type     | Value              |
| ------------------- | -------- | ------------------ |
| `IMAGE_REGISTRY`    | Variable | `quay.io`          |
| `IMAGE_REPOSITORY`  | Variable | `rhpds`            |
| `REGISTRY_USERNAME` | Secret   | `rhpds+github_action` (shared org robot) |
| `REGISTRY_PASSWORD` | Secret   | Robot account token |

### 4. Version Management

Create `bump-version.sh` following the cluster-scheduler pattern.

**Usage:**

```bash
./bump-version.sh              # auto-increment patch (v0.1.0 → v0.1.1)
./bump-version.sh v0.2.0       # explicit version
./bump-version.sh --dev v0.1.0 # allow from non-main branch
```

**Actions:**

1. Determine next version (auto-patch or explicit, strict semver grep `^v[0-9]+\.[0-9]+\.[0-9]+$` for existing tags)
2. Validate: semver format, tag uniqueness, version ordering
3. Validate: clean staging area and no uncommitted changes to target files
4. Update `helm/Chart.yaml`: `version` (without `v`) and `appVersion` (quoted, with `v`) fields
5. Update `pyproject.toml`: `version` field (without `v`, single-match guard)
6. Verify all sed substitutions succeeded (grep check)
7. Create git commit: `Release vX.Y.Z` (with recovery instructions on failure)
8. Create git tag: `vX.Y.Z` (with recovery instructions on failure)
9. Push branch + tag to origin (with recovery instructions on failure)

**Tag format:** `vX.Y.Z` (no app prefix). If a separate MLflow image is needed in the future, it would use `mlflow-vX.Y.Z` tags.

### 5. Transition Strategy

The transition from BuildConfig to Quay images is controlled entirely by existing Helm values — no new flags needed:

**Phase 1 — Current state (BuildConfig):**

```yaml
buildConfig:
  enabled: true # BuildConfig + ImageStream active
image:
  repository: "" # not set, uses ImageStream
```

**Phase 2 — After validation (Quay):**

```yaml
buildConfig:
  enabled: false # BuildConfig removed
image:
  repository: quay.io/rhpds/parsec # Quay image
```

The `parsec.image` helper in the Helm chart already handles both modes:

- `buildConfig.enabled=true` + no `image.repository` → ImageStream (`:latest`)
- `buildConfig.enabled=false` → requires `image.repository`, uses `version` as tag

No changes to the Helm chart templates or helpers are required.

## Files Changed

| File                             | Action  | Description                                  |
| -------------------------------- | ------- | -------------------------------------------- |
| `dockerfiles/Dockerfile`         | Rewrite | ubi9-minimal, multi-stage, no gcc/libpq/S2I  |
| `.dockerignore`                  | Create  | Exclude non-runtime files from build context |
| `.github/workflows/publish.yaml` | Create  | Build + push to Quay on tag/dispatch         |
| `bump-version.sh`                | Create  | Semantic versioning + tag management         |

## Files NOT Changed

- `requirements.txt` — no dependency changes
- `src/` — no application code changes
- `.github/workflows/ci.yml` — existing CI stays intact
- `helm/` — no template or values changes
- `config/` — no configuration changes

## Reference Implementation

The demo-reporting project (`~/Projects/demo-reporting/`) successfully applied this same pattern:

- `reporting-api`: 1.97 GB → 863 MB (-56%), 473 → 117 RPMs (-75%), 52 → 0 devel packages
- `reporting-mcp`: Single-stage ubi9-minimal build
- Both publish to `quay.io/rhpds/` via tag-triggered GitHub Actions workflows
- Both use `bump-version.sh` for version management

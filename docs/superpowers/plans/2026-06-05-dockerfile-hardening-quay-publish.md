# Dockerfile Hardening & Quay.io Publishing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the Parsec Docker image by migrating to ubi9-minimal (removing ~1.1 GB of unnecessary packages) and add a GitHub Actions workflow to publish versioned images to Quay.io.

**Architecture:** Multi-stage Dockerfile with ubi9-minimal base, pip in builder only, virtualenv copied to runtime. GitHub workflow triggered by semver tags (`vX.Y.Z`) or manual dispatch. `bump-version.sh` manages versioning and triggers the workflow. Coexists with current BuildConfig — no changes to Helm chart or application code.

**Tech Stack:** ubi9-minimal 9.8, Python 3.11, pip, Docker Buildx, GitHub Actions, Quay.io

**Spec:** `docs/superpowers/specs/2026-06-04-dockerfile-hardening-quay-publish-design.md`

**Workflow notes:** No per-task commits. All changes committed together after code review. User executes all git commands.

---

## File Map

| File                             | Action  | Responsibility                               |
| -------------------------------- | ------- | -------------------------------------------- |
| `dockerfiles/Dockerfile`         | Rewrite | Multi-stage ubi9-minimal build               |
| `.dockerignore`                  | Create  | Exclude non-runtime files from build context |
| `.github/workflows/publish.yaml` | Create  | Build + push to Quay.io on tag/dispatch      |
| `bump-version.sh`                | Create  | Semantic versioning + tag + push             |

---

### Task 1: Create `.dockerignore`

**Files:**

- Create: `.dockerignore`

- [ ] **Step 1: Create `.dockerignore`**

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

- [ ] **Step 2: Verify the negation patterns work**

The `*.md` exclusion with `!config/prompts/*.md` negation is critical — the agent prompt files are `.md` and must be included. Verify by checking the build context:

Run:

```bash
docker build --no-cache -f dockerfiles/Dockerfile -t parsec-test . 2>&1 | head -5
```

The context size should be significantly smaller than before (no `.git/`, no `playbooks/`, no `helm/`, etc.).

---

### Task 2: Rewrite `dockerfiles/Dockerfile`

**Files:**

- Rewrite: `dockerfiles/Dockerfile`

**Reference:** `~/Projects/demo-reporting/reporting-mcp/Dockerfile` (single-stage ubi9-minimal) and `~/Projects/demo-reporting/Dockerfile-reporting-api` (multi-stage ubi9-minimal with virtualenv)

- [ ] **Step 1: Rewrite the Dockerfile**

```dockerfile
# Stage 1: Builder — install dependencies into a virtualenv
FROM registry.access.redhat.com/ubi9-minimal:9.8 AS builder

RUN microdnf install -y python3.11 python3.11-pip && \
    microdnf clean all && \
    rm -rf /var/cache/yum

RUN python3.11 -m venv /opt/app-root/venv
ENV PATH="/opt/app-root/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt

# Stage 2: Runtime — minimal image with only the virtualenv
FROM registry.access.redhat.com/ubi9-minimal:9.8

RUN microdnf install -y python3.11 && \
    microdnf update -y && \
    microdnf clean all && \
    rm -rf /var/cache/yum && \
    ln -sf /usr/bin/python3.11 /usr/bin/python3 && \
    ln -sf /usr/bin/python3.11 /usr/bin/python

COPY --from=builder /opt/app-root/venv /opt/app-root/venv
ENV PATH="/opt/app-root/venv/bin:$PATH"

WORKDIR /app

COPY src/ src/
COPY static/ static/
COPY config/config.yaml config/config.yaml
COPY config/prompts/ config/prompts/
COPY data/ec2_pricing.json data-seed/ec2_pricing.json
COPY scripts/refresh_pricing.py scripts/refresh_pricing.py
COPY scripts/refresh_azure_billing.py scripts/refresh_azure_billing.py

RUN mkdir -p /app/data/reports /app/data/debug && \
    chown -R 1001:0 /app && \
    chmod -R g+rw /app

ENV PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1

USER 1001

RUN python3.11 -c "import dynaconf, fastapi, uvicorn, anthropic, boto3, pydantic, httpx; print('Dependencies OK')"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python3.11 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=5)"

CMD ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

Key changes from the current Dockerfile:

- Base image: `ubi9/python-311:latest` → `ubi9-minimal:9.8`
- Removed: `gcc`, `gcc-c++`, `libpq-devel`, `libpq`, S2I assemble
- Builder uses a virtualenv instead of S2I `/opt/app-root` site-packages
- Runtime has no pip, no build tools, no dnf
- Uses `python3.11` explicitly (ubi9-minimal installs as `python3.11`, not `python`)
- Creates `python3` and `python` symlinks (required by Helm CronJobs and script shebangs)

- [ ] **Step 2: Build the hardened image**

Run:

```bash
docker build --no-cache -f dockerfiles/Dockerfile -t parsec-hardened .
```

Expected: Build completes successfully, "Dependencies OK" printed during build.

- [ ] **Step 3: Verify the image works**

Run:

```bash
docker run --rm parsec-hardened python3.11 -c "from src.config import get_config; print('Config OK')"
```

Expected: "Config OK" — confirms the app can import its config module.

- [ ] **Step 4: Collect hardened image metrics and compare with baseline**

Run:

```bash
echo "=== HARDENED IMAGE ==="
echo "--- Image size ---"
docker images parsec-hardened --format "{{.Size}}"
echo "--- RPM count ---"
docker run --rm parsec-hardened rpm -qa 2>/dev/null | wc -l
echo "--- Devel packages ---"
docker run --rm parsec-hardened rpm -qa 2>/dev/null | grep -i "\-devel" | wc -l
echo "--- Python version ---"
docker run --rm parsec-hardened python3.11 --version
echo "--- pip ---"
docker run --rm parsec-hardened which pip 2>/dev/null && echo "present" || echo "not found"
echo "--- gcc ---"
docker run --rm parsec-hardened which gcc 2>/dev/null && echo "present" || echo "not found"
```

Expected results (approximate):

| Metric         | Baseline | Hardened | Delta   |
| -------------- | -------- | -------- | ------- |
| Image size     | 1.99 GB  | 1.07 GB  | -46%    |
| RPMs           | 473      | 118      | -75%    |
| Devel packages | 52       | 0        | -100%   |
| Python         | 3.11.13  | 3.11.13  | --      |
| pip            | present  | not found| removed |
| gcc            | present  | not found| removed |

- [ ] **Step 5: Verify security posture**

Run:

```bash
# Confirm non-root user
docker run --rm parsec-hardened id
# Expected: uid=1001(...) gid=0(root) groups=0(root)

# Confirm no compiler toolchain
docker run --rm parsec-hardened which gcc g++ make 2>/dev/null && echo "FAIL: build tools found" || echo "PASS: no build tools"

# Confirm no pip
docker run --rm parsec-hardened which pip pip3 pip3.11 2>/dev/null && echo "FAIL: pip found" || echo "PASS: no pip"

# Confirm no dnf (full package manager)
docker run --rm parsec-hardened which dnf 2>/dev/null && echo "FAIL: dnf found" || echo "PASS: no dnf"
```

---

### Task 3: Create `bump-version.sh`

**Files:**

- Create: `bump-version.sh`

**Reference:** `~/Projects/cluster-scheduler/bump-version.sh` — adapted for single-app repo (tags with `v` prefix, updates both Chart.yaml and pyproject.toml).

- [ ] **Step 1: Create `bump-version.sh`**

See `bump-version.sh` for the current implementation. Key features:

- Strict semver grep for existing tags (`^v[0-9]+\.[0-9]+\.[0-9]+$`)
- Clean staging area and unstaged changes checks before modifications
- `appVersion` stored quoted with `v` prefix (e.g., `appVersion: "v0.1.0"`)
- `pyproject.toml` single-match guard before sed
- All sed substitutions verified with `grep -q`
- Recovery instructions for commit, tag, and push failures
- Usage header and `--dev`/`--force` documented in error messages

- [ ] **Step 2: Make it executable**

Run:

```bash
chmod +x bump-version.sh
```

- [ ] **Step 3: Verify script syntax**

Run:

```bash
bash -n bump-version.sh
```

Expected: No output (clean parse).

---

### Task 4: Create `.github/workflows/publish.yaml`

**Files:**

- Create: `.github/workflows/publish.yaml`

**Reference:** `~/Projects/demo-reporting/.github/workflows/reporting-api.yaml` — adapted for single-app repo (no app prefix, single job).

- [ ] **Step 1: Create the workflow file**

See `.github/workflows/publish.yaml` for the current implementation. Key features:

- Single semver tag pattern: `v[0-9]*.[0-9]*.[0-9]*`
- `permissions: contents: read` (least privilege)
- Vars validation step (fail-fast if IMAGE_REGISTRY/IMAGE_REPOSITORY not set)
- User input passed via `env:` block (prevents script injection)
- Semver format validation for workflow_dispatch inputs
- appVersion extraction handles both quoted and unquoted YAML

```yaml
      - name: Build and push
        uses: docker/build-push-action@v7
        with:
          context: .
          file: dockerfiles/Dockerfile
          push: true
          tags: ${{ steps.image_tags.outputs.IMAGE_TAGS }}
          cache-from: type=gha,scope=parsec
          cache-to: type=gha,mode=max,scope=parsec
```

- [ ] **Step 2: Validate YAML syntax**

Run:

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/publish.yaml')); print('YAML OK')"
```

Expected: "YAML OK"

---

### Task 5: Build and Validate End-to-End

This task verifies everything works together before the code review cycle.

- [ ] **Step 1: Clean build from scratch**

Run:

```bash
docker build --no-cache -f dockerfiles/Dockerfile -t parsec-final .
```

Expected: Build succeeds, "Dependencies OK" printed.

- [ ] **Step 2: Verify app import**

Run:

```bash
docker run --rm parsec-final python3.11 -c "from src.config import get_config; print('Config OK')"
```

- [ ] **Step 3: Collect final metrics**

Run the metrics collection from Task 2 Step 4 against `parsec-final` and update the spec's Image Baseline & Target table with actual values.

- [ ] **Step 4: Update spec and plan to reflect final state**

Review and update both documents to reflect what was actually implemented — changes may have occurred during execution or code review:

- `docs/superpowers/specs/2026-06-04-dockerfile-hardening-quay-publish-design.md`:
  - Replace "Target (estimated)" column with actual measured values
  - Update any design decisions that changed during implementation (e.g., Dockerfile structure, workflow logic, bump-version.sh behavior)
  - Change status from "Draft" to "Implemented"
- `docs/superpowers/plans/2026-06-05-dockerfile-hardening-quay-publish.md`:
  - Update code blocks to match the actual final implementation
  - Mark completed steps, note any deviations from the original plan

---

### Task 6: Configure GitHub Repository Variables and Secrets

**Prerequisites:** Quay.io repository `rhpds/parsec` already created. Robot account: `rhpds+github_action` (shared across rhpds org repos).

- [ ] **Step 1: Create repository variables**

```bash
gh variable set IMAGE_REGISTRY --body "quay.io" --repo rhpds/parsec
gh variable set IMAGE_REPOSITORY --body "rhpds" --repo rhpds/parsec
```

- [ ] **Step 2: Create repository secrets**

```bash
gh secret set REGISTRY_USERNAME --body "rhpds+github_action" --repo rhpds/parsec
gh secret set REGISTRY_PASSWORD --repo rhpds/parsec
# Prompt will ask for value — enter the robot account token
```

- [ ] **Step 3: Verify configuration**

```bash
gh variable list --repo rhpds/parsec
gh secret list --repo rhpds/parsec
```

Expected output should show `IMAGE_REGISTRY`, `IMAGE_REPOSITORY` as variables and `REGISTRY_USERNAME`, `REGISTRY_PASSWORD` as secrets.

---

### Task 7: Code Review and Commit

**This task is user-driven.**

- [ ] **Step 1: Run code review**

User invokes `/code-review` or `/pr-review-toolkit:review-pr` to evaluate all changes.

- [ ] **Step 2: Iterate on findings**

Fix or justify each finding. Re-run review until ready-to-merge.

- [ ] **Step 3: Commit docs (spec + plan)**

User commits spec and plan as a separate commit:

```bash
git add docs/superpowers/specs/2026-06-04-dockerfile-hardening-quay-publish-design.md \
       docs/superpowers/plans/2026-06-05-dockerfile-hardening-quay-publish.md
git commit -m "docs: add spec and plan for Dockerfile hardening and Quay publishing"
```

- [ ] **Step 4: Commit implementation**

User commits all implementation files:

```bash
git add dockerfiles/Dockerfile .dockerignore .github/workflows/publish.yaml bump-version.sh
git commit -m "feat: harden Docker image with ubi9-minimal and add Quay.io publishing

- Migrate from ubi9/python-311 (1.99 GB) to ubi9-minimal (1.07 GB, -46%)
- Remove unused gcc, libpq, S2I, devel packages (473 → 118 RPMs, -75%)
- Add .dockerignore to reduce build context
- Add publish.yaml workflow for Quay.io image publishing
- Add bump-version.sh for semantic version management
- BuildConfig compatibility preserved — coexists with new workflow"
```

- [ ] **Step 5: Push and create PR**

User pushes and creates PR targeting `main`. Use the spec and plan as the basis for the PR description:

- Read `docs/superpowers/specs/2026-06-04-dockerfile-hardening-quay-publish-design.md` for the problem statement, goals, key discovery (libpq unnecessary), baseline vs actual metrics table, and transition strategy
- Read `docs/superpowers/plans/2026-06-05-dockerfile-hardening-quay-publish.md` for the file map and implementation details
- Structure the PR body with: Summary (from spec goals), Key Discovery, Image Comparison (metrics table), Files Changed (from plan file map), Transition Strategy, and Test Plan

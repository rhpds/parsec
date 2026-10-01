# Stage 1: Build — S2I assemble handles pip install into /opt/app-root
FROM registry.access.redhat.com/ubi9/python-311:latest AS builder

USER 0
RUN dnf install -y --nodocs gcc gcc-c++ libpq-devel && \
    dnf clean all

COPY requirements.txt /tmp/src/
RUN /usr/bin/fix-permissions /tmp/src

USER 1001
RUN /usr/libexec/s2i/assemble

# Stage 2: Production
FROM registry.access.redhat.com/ubi9/python-311:latest

USER 0
# Runtime contract. Whoever rebases this file onto another base image (the
# ubi9-minimal hardening in #29 rewrites it wholesale) must keep ALL of:
#   - git                 POST /api/skills/install
#   - nodejs + the pinned `claude` CLI   agent.runtime=sdk
#   - /app/.claude/skills seeded from /app/skills   SDK skill discovery
#   - a group-0 writable HOME             the CLI's ~/.claude state
# The verify RUN near the end asserts each one, so dropping any of them fails
# the image build instead of surfacing as a 501 or an inert SDK on a live pod.
RUN dnf install -y --nodocs libpq && dnf clean all

# git is for POST /api/skills/install, which clones an external skill bundle at
# a pinned ref. That endpoint is admin-gated and off by default
# (skills.install_enabled), so shipping the binary costs nothing when unused —
# without it the endpoint can only return 501. Kept on its own line so a
# base-image swap carries it over as one line (`microdnf install -y git` on
# ubi9-minimal).
RUN dnf install -y --nodocs git && dnf clean all

# Claude Agent SDK runtime: Node.js + the `claude` CLI that the SDK drives as a
# subprocess (the Python claude-agent-sdk wrapper comes via requirements.txt).
# Required for agent.runtime=sdk; harmless for the default legacy runtime.
# Pin the CLI to the version verified against the bundled claude-agent-sdk so an
# image rebuild can't silently pull a breaking release.
ARG CLAUDE_CODE_VERSION=2.1.169
RUN dnf install -y --nodocs nodejs npm && dnf clean all && \
    npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} && npm cache clean --force

COPY --from=builder /opt/app-root /opt/app-root

WORKDIR /app

COPY src/ src/
COPY static/ static/
COPY config/config.yaml config/config.yaml
COPY config/prompts/ config/prompts/
COPY skills/ skills/
COPY data/ec2_pricing.json data-seed/ec2_pricing.json
COPY scripts/refresh_pricing.py scripts/refresh_pricing.py
COPY scripts/refresh_azure_billing.py scripts/refresh_azure_billing.py
COPY scripts/verify_sdk_cli.py scripts/verify_sdk_cli.py

# Skills-layout bridge: the SDK's "project" setting source discovers skills at
# <cwd>/.claude/skills/, but Parsec ships them under skills/ and may also mount
# external skill repos at skills.plugin_paths. So the root is a real directory
# of per-skill symlinks rather than one symlink to skills/ — that lets baked and
# mounted skills coexist as siblings. Startup rebuilds it from whatever the
# loader discovers (src/skills/sdk_root.py); these links seed it so the image
# is self-consistent before the app runs.
RUN mkdir -p /app/.claude/skills && \
    for d in /app/skills/*/; do \
        [ -d "$d" ] && ln -sfn "$d" "/app/.claude/skills/$(basename "$d")"; \
    done

RUN mkdir -p /app/data/reports /app/data/debug /home/claude && \
    chgrp -R 0 /app /home/claude && \
    chmod -R g=u /app /home/claude

# HOME must be writable for the claude CLI's state (~/.claude); /home/claude is
# group-0 writable for the arbitrary OpenShift UID. NOTE: this sets HOME globally
# for the container — it points at an empty, group-writable dir so the blast radius
# is benign, but scoping HOME to only the SDK subprocess env is a follow-up to
# revisit when this Dockerfile is reconciled with the ubi9-minimal hardening (#29).
ENV PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1 \
    HOME=/home/claude \
    CLAUDE_CODE_ACCEPT_TOS=true

USER 1001

RUN python3 -c "import dynaconf, fastapi, uvicorn; print('Dependencies OK')" && \
    git --version && \
    test -w "$HOME" && \
    node --version && \
    claude --version | grep -F "${CLAUDE_CODE_VERSION}" && \
    python3 -c "import claude_agent_sdk; print('claude-agent-sdk OK')" && \
    python3 /app/scripts/verify_sdk_cli.py && \
    test -f /app/.claude/skills/icinga-triage/SKILL.md && echo "icinga-triage skill baked"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

CMD ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8000"]

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
RUN dnf install -y --nodocs libpq && dnf clean all

# Claude Agent SDK runtime: Node.js + the `claude` CLI that the SDK drives as a
# subprocess (the Python claude-agent-sdk wrapper comes via requirements.txt).
# Required for agent.runtime=sdk; harmless for the default legacy runtime.
RUN dnf install -y --nodocs nodejs npm && dnf clean all && \
    npm install -g @anthropic-ai/claude-code && npm cache clean --force

COPY --from=builder /opt/app-root /opt/app-root

WORKDIR /app

COPY src/ src/
COPY static/ static/
COPY config/config.yaml config/config.yaml
COPY config/prompts/ config/prompts/
COPY data/ec2_pricing.json data-seed/ec2_pricing.json
COPY scripts/refresh_pricing.py scripts/refresh_pricing.py
COPY scripts/refresh_azure_billing.py scripts/refresh_azure_billing.py
COPY skills/ skills/

# Skills-layout bridge: the SDK's "project" setting source discovers skills at
# <cwd>/.claude/skills/, but Parsec ships them under skills/. Symlink so the
# baked icinga-triage skill is discoverable by the SDK runtime.
RUN mkdir -p /app/.claude && ln -sfn /app/skills /app/.claude/skills

RUN mkdir -p /app/data/reports /app/data/debug /home/claude && \
    chgrp -R 0 /app /home/claude && \
    chmod -R g=u /app /home/claude

# HOME must be writable for the claude CLI's state (~/.claude); /home/claude is
# group-0 writable for the arbitrary OpenShift UID.
ENV PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1 \
    HOME=/home/claude \
    CLAUDE_CODE_ACCEPT_TOS=true

USER 1001

RUN python -c "import dynaconf, fastapi, uvicorn; print('Dependencies OK')" && \
    node --version && claude --version && \
    python -c "import claude_agent_sdk; print('claude-agent-sdk OK')" && \
    test -f /app/.claude/skills/icinga-triage/SKILL.md && echo "icinga-triage skill baked"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

CMD ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8000"]

# Stage 1: Build
FROM registry.access.redhat.com/ubi9/python-311:latest AS builder

USER 0

RUN dnf install -y --nodocs gcc gcc-c++ libpq-devel && \
    dnf clean all

COPY requirements.txt /tmp/requirements.txt
RUN python3.11 -m venv /opt/app-root/venv && \
    /opt/app-root/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/app-root/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# Stage 2: Production
FROM registry.access.redhat.com/ubi9/python-311:latest

USER 0

RUN dnf install -y --nodocs libpq && dnf clean all

COPY --from=builder /opt/app-root/venv /opt/app-root/venv
ENV VIRTUAL_ENV="/opt/app-root/venv" \
    PATH="/opt/app-root/venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY src/ src/
COPY static/ static/
COPY config/config.yaml config/config.yaml
COPY config/agent_instructions.md config/agent_instructions.md

RUN mkdir -p /app/reports && \
    chgrp -R 0 /app && \
    chmod -R g=u /app

USER 1001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

CMD ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8000"]

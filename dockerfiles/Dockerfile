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

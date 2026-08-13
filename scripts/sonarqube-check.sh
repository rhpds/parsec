#!/usr/bin/env bash
set -euo pipefail

if [ -z "${SONAR_URL:-}" ] || [ -z "${SONAR_TOKEN:-}" ]; then
    echo "⚠️  SONAR_URL or SONAR_TOKEN not set — skipping SonarQube analysis"
    exit 0
fi

if ! curl -sf --connect-timeout 5 "$SONAR_URL/api/system/status" >/dev/null 2>&1; then
    echo "⚠️  SonarQube unreachable (not on VPN?) — skipping"
    exit 0
fi

if ! command -v sonar-scanner >/dev/null 2>&1; then
    echo "⚠️  sonar-scanner not installed — skipping"
    exit 0
fi

pytest tests/ -q --cov=src --cov-report=xml:coverage.xml 2>/dev/null &&
sonar-scanner \
    -Dsonar.host.url="$SONAR_URL" \
    -Dsonar.token="$SONAR_TOKEN" \
    -Dsonar.qualitygate.wait=true \
    -Dsonar.qualitygate.timeout=300 \
    2>&1 | tail -20

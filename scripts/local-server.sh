#!/usr/bin/env bash
# Local dev server management for Parsec.
# Usage: ./scripts/local-server.sh {start|stop|restart|status}

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PIDFILE="$PROJECT_DIR/.server.pid"
LOGFILE="$PROJECT_DIR/logs/server.log"
HOST="0.0.0.0"
PORT="8000"
MCP_CONTAINER="parsec-icinga-mcp"
MCP_IMAGE="quay.io/rhpds/monitoring-mcp:v0.3.1"
MCP_PORT="3000"
REPORTING_MCP_PORT="8881"
REPORTING_MCP_PIDFILE="$PROJECT_DIR/.reporting-mcp.pid"
REPORTING_MCP_KUBECONFIG="$HOME/secrets/ocpv-infra01.dal12.infra.demo.redhat.com.kubeconfig"

cd "$PROJECT_DIR"
mkdir -p logs

_container_cmd() {
    # Use podman or docker, whichever is available
    if command -v podman >/dev/null 2>&1; then
        echo "podman"
    elif command -v docker >/dev/null 2>&1; then
        echo "docker"
    else
        echo ""
    fi
}

_mcp_is_configured() {
    # Check if Icinga env vars are set in .env or config.local.yaml
    if [ -f .env ] && grep -q 'ICINGA_API_URL=.' .env 2>/dev/null; then
        return 0
    fi
    if [ -f config/config.local.yaml ] && grep -q 'mcp_url:.*[^ ]' config/config.local.yaml 2>/dev/null; then
        return 0
    fi
    return 1
}

_mcp_start() {
    if ! _mcp_is_configured; then
        return 0
    fi
    local cmd
    cmd=$(_container_cmd)
    if [ -z "$cmd" ]; then
        echo "Warning: Icinga MCP needs podman or docker (not found)"
        return 0
    fi
    $cmd rm -f "$MCP_CONTAINER" >/dev/null 2>&1 || true

    # Pull with empty auth to avoid cached robot credentials
    local anon_auth
    anon_auth=$(mktemp)
    echo '{}' > "$anon_auth"
    $cmd pull --authfile "$anon_auth" "$MCP_IMAGE" >/dev/null 2>&1 || true
    rm -f "$anon_auth"

    echo "Starting Icinga MCP sidecar..."
    $cmd run -d --name "$MCP_CONTAINER" \
        -p "$MCP_PORT:$MCP_PORT" \
        --env-file <(grep '^ICINGA_' .env 2>/dev/null || true) \
        "$MCP_IMAGE" \
        --transport sse --host 0.0.0.0 --port "$MCP_PORT" \
        >/dev/null 2>&1 \
    && echo "Icinga MCP running on port $MCP_PORT" \
    || echo "Warning: Icinga MCP failed to start"
}

_mcp_stop() {
    local cmd
    cmd=$(_container_cmd)
    [ -z "$cmd" ] && return 0
    if $cmd inspect "$MCP_CONTAINER" >/dev/null 2>&1; then
        $cmd rm -f "$MCP_CONTAINER" >/dev/null 2>&1
        echo "Icinga MCP stopped"
    fi
}

_reporting_mcp_start() {
    if ! command -v oc >/dev/null 2>&1; then
        echo "Warning: oc not found, skipping Reporting MCP port-forward"
        return 0
    fi
    if [ ! -f "$REPORTING_MCP_KUBECONFIG" ]; then
        echo "Warning: kubeconfig not found at $REPORTING_MCP_KUBECONFIG, skipping Reporting MCP"
        return 0
    fi
    # Kill any existing port-forward on the same port
    _reporting_mcp_stop
    echo "Starting Reporting MCP port-forward on port $REPORTING_MCP_PORT..."
    oc port-forward svc/reporting-mcp "$REPORTING_MCP_PORT:8080" \
        -n demo-reporting \
        --kubeconfig="$REPORTING_MCP_KUBECONFIG" \
        >> "$PROJECT_DIR/logs/reporting-mcp.log" 2>&1 &
    echo "$!" > "$REPORTING_MCP_PIDFILE"
    # Wait briefly for port to be ready
    for i in $(seq 1 5); do
        sleep 1
        if curl -sf "http://localhost:$REPORTING_MCP_PORT" >/dev/null 2>&1; then
            echo "Reporting MCP forwarding on port $REPORTING_MCP_PORT"
            return 0
        fi
    done
    echo "Reporting MCP port-forward started (may take a moment to connect)"
}

_reporting_mcp_stop() {
    if [ -f "$REPORTING_MCP_PIDFILE" ]; then
        local pid
        pid=$(cat "$REPORTING_MCP_PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            echo "Reporting MCP port-forward stopped"
        fi
        rm -f "$REPORTING_MCP_PIDFILE"
    fi
}

_is_running() {
    if [ -f "$PIDFILE" ]; then
        local pid
        pid=$(cat "$PIDFILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$PIDFILE"
    fi
    return 1
}

start() {
    if _is_running; then
        echo "Already running (PID $(cat "$PIDFILE"))"
        return 0
    fi

    echo "Starting Parsec on $HOST:$PORT..."

    if [ -d .venv ]; then
        source .venv/bin/activate
    fi

    nohup python3 -m uvicorn src.app:app \
        --host "$HOST" --port "$PORT" \
        >> "$LOGFILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PIDFILE"

    _mcp_start
    _reporting_mcp_start

    # Wait for startup
    for i in $(seq 1 10); do
        sleep 1
        if curl -sf "http://localhost:$PORT/api/health" >/dev/null 2>&1; then
            echo "Started (PID $pid) — http://localhost:$PORT"
            return 0
        fi
    done

    echo "Warning: server started (PID $pid) but health check not responding yet."
    echo "Check logs: tail -f $LOGFILE"
}

stop() {
    if ! _is_running; then
        echo "Not running"
        return 0
    fi

    local pid
    pid=$(cat "$PIDFILE")
    echo "Stopping (PID $pid)..."
    kill "$pid" 2>/dev/null
    for i in $(seq 1 5); do
        sleep 1
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PIDFILE"
            _mcp_stop
            _reporting_mcp_stop
            echo "Stopped"
            return 0
        fi
    done
    kill -9 "$pid" 2>/dev/null
    rm -f "$PIDFILE"
    echo "Killed"
    _mcp_stop
    _reporting_mcp_stop
}

restart() {
    stop
    start
}

status() {
    if _is_running; then
        local pid
        pid=$(cat "$PIDFILE")
        echo "Running (PID $pid)"
        curl -sf "http://localhost:$PORT/api/health" 2>/dev/null && echo "" || echo "Health check failed"
    else
        echo "Not running"
    fi
}

case "${1:-}" in
    start)   start ;;
    stop)    stop ;;
    restart) restart ;;
    status)  status ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac

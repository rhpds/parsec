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

cd "$PROJECT_DIR"
mkdir -p logs

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
            echo "Stopped"
            return 0
        fi
    done
    kill -9 "$pid" 2>/dev/null
    rm -f "$PIDFILE"
    echo "Killed"
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

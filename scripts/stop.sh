#!/usr/bin/env bash
APP="$(cd "$(dirname "$0")/.." && pwd)"
PIDS="${APP}/scripts/pids"

for svc in api bot scheduler; do
    pid_file="${PIDS}/${svc}.pid"
    if [ -f "${pid_file}" ]; then
        pid=$(cat "${pid_file}")
        if kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" && echo "[STOP] ${svc} (PID: ${pid})"
        fi
        rm -f "${pid_file}"
    fi
done
echo "所有服務已停止"

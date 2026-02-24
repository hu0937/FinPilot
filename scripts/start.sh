#!/usr/bin/env bash
# 啟動所有量化平台服務
set -e

VENV="${VENV_PYTHON:-python3}"
APP="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="${APP}/data/logs"
PIDS="${APP}/scripts/pids"

mkdir -p "${LOGS}" "${PIDS}"

start_svc() {
    local name="$1" cmd="$2"
    local pid_file="${PIDS}/${name}.pid"
    local log="${LOGS}/${name}.log"

    if [ -f "${pid_file}" ]; then
        local old=$(cat "${pid_file}")
        if kill -0 "${old}" 2>/dev/null; then
            echo "[SKIP] ${name} 已在執行（PID: ${old}）"
            return
        fi
    fi

    echo "[START] ${name}..."
    nohup bash -c "${cmd}" >> "${log}" 2>&1 &
    local pid=$!
    echo "${pid}" > "${pid_file}"
    sleep 1
    if kill -0 "${pid}" 2>/dev/null; then
        echo "[OK]   ${name} PID=${pid}"
    else
        echo "[ERR]  ${name} 啟動失敗，查看 ${log}"
    fi
}

# 初始化 DB
echo "[INIT] 初始化資料庫..."
cd "${APP}"
"${VENV}" -c "

from core.database import init_db; init_db()
"

# Telegram Bot
start_svc "bot" \
    "cd ${APP} && ${VENV} bot/telegram_bot.py"

# APScheduler
start_svc "scheduler" \
    "cd ${APP} && ${VENV} scheduler/job_runner.py"

echo ""
echo "=== 啟動完成 ==="
echo "  Logs      : ${LOGS}/"

#!/usr/bin/env bash
APP="$(cd "$(dirname "$0")/.." && pwd)"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 重啟所有服務..."

# 先終止所有現有服務 process
pkill -f "telegram_bot.py"       2>/dev/null || true
pkill -f "job_runner.py"         2>/dev/null || true
sleep 2

rm -f "${APP}/scripts/pids/"*.pid
bash "${APP}/scripts/start.sh"

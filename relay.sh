#!/usr/bin/env bash
# 便利包装：所有逻辑都在 relay.py 里，跨平台一致。
# Windows 用户直接用：python relay.py <子命令>
set -e
cd "$(dirname "$0")"
exec "${PYTHON:-python3}" relay.py "$@"

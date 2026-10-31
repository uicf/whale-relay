#!/usr/bin/env bash
# whale-relay 一键启动
set -e
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
command -v "$PY" >/dev/null 2>&1 || { echo "[x] 找不到 python3，请先安装 Python 3.8+"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)' || { echo "[x] 需要 Python 3.8 或更高版本"; exit 1; }

# 1) 生成配置
if [ ! -f config.json ]; then
  cp config.minimal.json config.json
  NEWKEY="sk-relay-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  "$PY" - "$NEWKEY" <<'EOF'
import json,sys,io
p="config.json"; k=sys.argv[1]
d=json.load(open(p,encoding="utf-8"))
d["client_keys"]={k:{"name":"local","allow":"*","rpm":60,"daily":1000}}
json.dump(d,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
print("[i] 已生成 config.json，你的客户端密钥是：\n    %s\n" % k)
EOF
else
  echo "[i] 已存在 config.json，跳过生成"
fi

# 2) 检查上游 key 是否还是占位符
if grep -q "REPLACE_WITH_YOUR_UPSTREAM_KEY" config.json; then
  echo "[!] config.json 里的上游 key 还是占位符，请打开文件把 REPLACE_WITH_YOUR_UPSTREAM_KEY 换成真实密钥"
  echo "    改完再运行 ./start.sh"
  exit 1
fi

# 3) 可选依赖：只有当某个上游写了 proxy=true 才需要 PySocks
if grep -q '"proxy": true' config.json; then
  if ! "$PY" -c 'import socks' 2>/dev/null; then
    echo "[i] 检测到你启用了代理，正在安装 PySocks…"
    "$PY" -m pip install --quiet PySocks || { echo "[x] PySocks 安装失败，可手动 pip install PySocks"; exit 1; }
  fi
fi

echo "[i] 启动中…  监听 ${RELAY_HOST:-0.0.0.0}:${RELAY_PORT:-8090}"
exec "$PY" relay.py

# AGENTS.md — 自动化安装指引

本文件面向**自动化代理（AI agent）/ 脚本**，描述如何在无人值守的情况下部署 whale-relay。
人类读者请先看 `README.md`。

---

## 部署与运维须知（agent 必读）

**日志会无限增长。** `logs/access.log` 和 `logs/upstream-*.log` 是纯追加写入，
没有内置轮转。长期运行请挂 logrotate（或改用外部收集），否则迟早吃满磁盘。
临时清理可以：`: > logs/access.log`（服务运行中也能安全截断）。

**`config.json` 是唯一真相。** 所有配置都在这个文件里，改之前先复制一份
（`cp config.json config.json.bak`），CLI 的 `config set/del` 虽然是原子写，
但不会替你留历史版本，写错路径只能靠备份回滚。

**relay 本身就是一个受控代理。** `upstreams.*.host` 可以填任意域名，
它的职责就是去连外部 API。所以它天然能访问内网地址——这不是漏洞，是能力。
公网部署时请用防火墙/安全组限制它能访问的范围，不要把它暴露在不可信网络里。

**请求/响应上限可以调：**

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `RELAY_MAX_BODY_BYTES` | 1 MB | 客户端请求体上限，长上下文/多图场景可调大 |
| `RELAY_MAX_RESP_BYTES` | 16 MB | 上游响应上限，防止异常上游撑爆内存 |
| `RELAY_MAX_CONCURRENT` | 0（不限） | 最大并发连接，超出直接 503 |

**只转发明确支持的路径。** 服务端白名单为 `/v1/chat/completions`、`/v1/messages`、
`/v1/completions`，其它 `/v1/*` 一律 404。这是刻意的——别为了"兼容性好一点"
就往白名单里加路径，那等于把上游的任意端点开放给持 key 的人。

**`config list` 会脱敏。** `key`、`free_key`、以及代理串里的 `user:pass@` 都会被遮蔽。
如果你新增了带密钥的配置字段，记得同步在 `_cli_config` 的 list 分支里加遮蔽，
否则 agent 读配置时会把密钥打进日志/CI 输出。

**配额在请求合法后才扣。** 全局 daily 和每 key 配额都挪到了 JSON 校验与上游解析之后，
坏请求不再消耗日额度。改动这段逻辑时注意别把扣减挪回前面。

**`config set` 有类型校验。** `key`/`host`/`base` 只收字符串，
`rpm`/`daily`/`timeout` 只收整数，`enabled` 只收 true/false。
写错类型会直接被拒绝，不会写出坏配置——**这是刻意的，不要绕过它。**


## 0. 前置检查

```
python3 --version          # 必须 >= 3.8
```

若无 python3 → 终止并报告，不要尝试安装系统级 Python。

---

> 首次安装请看 `INSTALL_FOR_AGENT.md`（纯指令版，含验收与失败对照）。

## 1. 获取代码

```bash
git clone <REPO_URL> whale-relay && cd whale-relay
```

若已存在该目录，**不要覆盖**，先执行 `git -C whale-relay pull --ff-only`。

---

## 2. 生成配置（非交互）

**不要**调用 `start.sh`（它有交互式提示）。直接程序化生成：

```bash
cp config.minimal.json config.json
```

然后改写密钥（**每个上游用各自的密钥，绝不复用**）：

```bash
python3 - <<'PY'
import json, os, secrets, sys
p = "config.json"
d = json.load(open(p, encoding="utf-8"))

# 1) 客户端密钥：写入文件，不打印到 stdout
ck = "sk-relay-" + secrets.token_hex(12)
d["client_keys"] = {ck: {"name": "agent", "allow": "*", "rpm": 60, "daily": 1000}}

# 2) 上游密钥：按上游名分别从 UPSTREAM_KEY_<NAME> 读取
#    （如 UPSTREAM_KEY_OR / UPSTREAM_KEY_QWEN），缺哪个就移除哪个上游
name_map = {"or": "UPSTREAM_KEY_OR", "qwen": "UPSTREAM_KEY_QWEN", "zhipu": "UPSTREAM_KEY_ZHIPU"}
kept = {}
for name, u in d["upstreams"].items():
    k = os.environ.get(name_map.get(name, "UPSTREAM_KEY_" + name.upper()), "").strip()
    if k:
        u["key"] = k
        kept[name] = u
if not kept:
    print("MISSING_UPSTREAM_KEY: 至少需要一个 UPSTREAM_KEY_<NAME>", file=sys.stderr)
    sys.exit(2)
d["upstreams"] = kept

os.chmod(p, 0o600)          # 密钥文件立刻收权限
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
open(".client_key", "w").write(ck)
print("客户端密钥已写入 .client_key（权限 600）")
PY
export CLIENT_KEY="$(cat .client_key)"
```

**绝不要把同一个上游密钥写进多个上游**——那会把 A 服务商的 key 明文发到 B 服务商。
**必须由用户提供 `UPSTREAM_KEY_<NAME>`。** 缺失则终止，不允许填占位符继续。

### 可选：自定义上游

若用户要接入非默认上游，改写 `config.json` 的 `upstreams` 对象，每个条目需含：
`host`（不带协议）、`base`（如 `/v1`）、`key`、`proxy`（bool）、`aliases`（模型别名表）。

---

## 2.5 用 relay.py 管理（跨平台，推荐）

```bash
python relay.py init                      # 非交互时读 UPSTREAM_KEY_<NAME> 环境变量
python relay.py check                     # 配置校验，非 0 退出码表示不可启动
python relay.py key new --name agent --rpm 60 --daily 1000 --allow or
python relay.py key list                  # 打码，可安全写日志
python relay.py                           # 启动（会先做代理配置校验）
```

- 只有 `key new` 会把新密钥打印到 stdout，其余命令不输出完整密钥
- `key show` / `key del` 要求前缀至少 8 位，避免误匹配
- 所有写操作使用唯一临时文件 + fsync + `os.replace`，异常会清理临时文件
- `init` 中途 Ctrl+C 不会写盘；未填密钥的上游标记 `disabled` 而非删除
- Windows 兼容：读取配置用 `utf-8-sig`（认 BOM），覆盖失败会自动重试
- 非交互环境下 `init` 不会阻塞，缺 `UPSTREAM_KEY_<NAME>` 的上游会被标记停用

## 3. 条件依赖

只有当**任一** `upstreams.*.proxy` 为真值（`true` 或 `socks5://...` 字符串）时才需要 PySocks：

```bash
python3 -c "import json,sys; d=json.load(open('config.json')); sys.exit(0 if any(u.get('proxy') for u in d['upstreams'].values()) else 1)" \
  && pip install --quiet PySocks
```

---

## 4. 启动

**前台验证**（推荐先这样跑，便于看报错）：

```bash
RELAY_HOST=127.0.0.1 RELAY_PORT=8090 python3 relay.py
```

> 默认监听 `0.0.0.0`，本地验证请显式绑 `127.0.0.1`，避免服务意外暴露到局域网/公网。

**PEP 668 环境**（部分系统 Python 受管）：`pip install` 会报 `externally-managed-environment`，
此时改用 `python3 -m venv .venv && . .venv/bin/activate && pip install PySocks`，再激活虚拟环境运行。

**后台常驻**（先建日志目录）：

```bash
mkdir -p logs
RELAY_HOST=127.0.0.1 nohup python3 relay.py > logs/stdout.log 2>&1 &
echo $! > relay.pid
```

**Docker 方式**（二选一）：

```bash
docker compose up -d
```

---

## 5. 验证（必须全部通过才算成功）

```bash
# ① 健康检查：期望 200 + {"ok": true}
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8090/healthz

# ② 错误密钥应被拒绝：期望 401
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8090/v1/models \
  -H "Authorization: Bearer wrong-key"

# ③ 正确密钥拉模型表：期望 200 且返回 JSON
curl -s http://127.0.0.1:8090/v1/models -H "Authorization: Bearer $CLIENT_KEY" | head -c 200

# ④ 真实对话：期望 200 且含 choices
curl -s http://127.0.0.1:8090/v1/chat/completions \
  -H "Authorization: Bearer $CLIENT_KEY" -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku","messages":[{"role":"user","content":"say ok"}],"max_tokens":16}'
```

| 检查项 | 期望 | 不符时的含义 |
|---|---|---|
| ① | `200` | 进程未启动 / 端口被占 |
| ② | `401` | 认证逻辑异常，勿继续上线 |
| ③ | `200` | 见下方「模型表卡住」 |
| ④ | `200` + `choices` | 上游密钥错误或网络不通 |

> 第 ④ 步会产生**真实费用**（几分钱）。若用户要求零成本验证，跳过该步并说明原因。
> 示例中的 `claude-haiku` 别名须在 `upstreams.*.aliases` 中存在，否则会 404。

---

## 6. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动即退出，提示 `[FATAL] 找不到 ...` | 没有 `config.json` | 回到第 2 步 |
| 启动即退出，提示 `内容不完整` | `client_keys` 或 `upstreams` 为空 | 检查 JSON 结构 |
| `/v1/models` 卡住数秒 | 上游不可达，正在探测（已限 5 秒） | 正常降级，返回本地别名表；可调 `RELAY_MODELS_TIMEOUT` |
| `/v1/models` 一直空 | 上游 key 无效 | 用 `/v1/chat/completions` 直接验证上游 |
| 所有请求 401 | 客户端密钥不匹配 | 用第 2 步打印的 `CLIENT_KEY` |
| 偶发 429 | 触发 RPM 限制 | 调高 `client_keys.*.rpm` |
| 提示 PySocks 未安装 | 某上游启用了代理 | `pip install PySocks` |
| `[FATAL] 无法解析的上游 proxy 地址` | `proxy` 写了不支持的协议 | 改为 `socks5://host:port` 或 `socks5h://user:pass@host:port` |
| 限流不生效 | 部署在反代后且未开信任 | 设 `RELAY_TRUST_PROXY=1` |

---

## 7. 代理必须遵守的约束

1. **不要**把 `config.json` 提交到任何仓库，也不要打印其内容到日志。
2. **不要**将服务直接绑定到公网明文端口；必须由用户确认已配置 HTTPS 反代。
3. **不要**修改 `relay.py` 的认证、限流逻辑来"方便调试"。
4. **不要**在未获用户明确同意时更换 `UPSTREAM_KEY` 或添加新上游。
5. 安装失败时**如实报告错误原文**，不要用占位配置假装成功。
6. `config.json` 权限 `600` 已在第 2 步设置；若手工创建，请自行 `chmod 600`。
7. 不要把客户端密钥打印到日志或标准输出。

---

## 8. 环境变量速查

| 变量 | 默认 | 用途 |
|---|---|---|
| `RELAY_HOST` | `0.0.0.0` | 监听地址 |
| `RELAY_PORT` | `8090` | 监听端口 |
| `RELAY_CONFIG` | `./config.json` | 配置路径（**不会回退到示例配置**） |
| `RELAY_LOG_DIR` | `./logs` | 日志目录 |
| `RELAY_SOCKS_HOST` / `RELAY_SOCKS_PORT` | `127.0.0.1` / `1080` | SOCKS5 |
| `RELAY_REQUEST_TIMEOUT` | `30` | 单请求超时（秒） |
| `RELAY_TRUST_PROXY` | `0` | `1` 才采信 X-Forwarded-For |
| `RELAY_MODELS_TIMEOUT` | `5` | 上游模型表探测超时 |
| `RELAY_MODELS_CACHE_TTL` | `300` | 模型表缓存秒数 |
| `RELAY_WINDOW_OPEN_HOUR` / `_MINUTE` | `0` / `0` | 服务时段起点，与终点相同=全天 |
| `RELAY_WINDOW_CLOSE_HOUR` / `_MINUTE` | `0` / `0` | 服务时段终点，支持跨零点 |
| `RELAY_INSECURE_TLS` | `0` | `1` 关闭上游证书校验（不建议） |

---

## 9. 停止与清理

```bash
kill "$(cat relay.pid)" && rm -f relay.pid     # 后台方式
docker compose down                            # Docker 方式
```

清理**不要**删除 `config.json`，除非用户明确要求。

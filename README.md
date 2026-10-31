# whale-relay

单文件 OpenAI 兼容中转服务。约 1000 行，纯标准库，一个 Python 文件就能跑。

把多个大模型供应商统一成一个 `/v1` 接口，按模型名前缀自动分流，
并给每个客户端 Key 单独限速、限额。

```
客户端 ──► whale-relay :8090 ──┬──► 上游 A（如 OpenRouter）
                              ├──► 上游 B（如阿里百炼）
                              └──► 上游 C（自定义）
```

## 特性

- **零框架依赖**：不用 flask / fastapi，`python3 relay.py` 直接跑
- **多上游路由**：`qwen/xxx` 走上游 A，`zhipu/xxx` 走上游 B，未匹配的走默认上游
- **协议双兼容**：`/v1/chat/completions`（OpenAI）与 `/v1/messages`（Anthropic 原生透传）
- **Key 分级**：每个客户端 Key 可限定可用上游、每分钟请求数、每日额度、有效期
- **基础防护**：IP 限流、认证失败封禁、请求体上限、连接超时、访问日志
- **可选 SOCKS5**：只有上游配 `proxy: true` 时才需要 PySocks

> 自动化部署请见 [`AGENTS.md`](./AGENTS.md)（面向 agent / 脚本的无人值守安装步骤）。

> **让 AI agent 帮你装？** 把 [`INSTALL_FOR_AGENT.md`](INSTALL_FOR_AGENT.md) 给它，
> 那是一份纯指令版说明书（无背景介绍、含失败对照表）。本文件是给人看的。

## 快速开始

只需要 Python 3.8+，无第三方依赖（启用代理时才需要 PySocks）。

```bash
python relay.py init    # 交互式填上游密钥，自动生成客户端密钥
python relay.py         # 启动
```

Windows / Linux / macOS 命令完全一致。`relay.sh` 只是 Linux/macOS 上的便利包装，
等价于 `python3 relay.py "$@"`。

管理客户端密钥：

```bash
python relay.py key new --name 朋友 --rpm 10 --daily 200 --allow or
python relay.py key list        # 打码显示
python relay.py key show <前缀> # 至少 8 位前缀
python relay.py key del <前缀>
python relay.py check           # 校验配置
```

> 配置里没有密钥的上游会被标记 `"disabled": true` 而不是删除，
> 之后填上密钥再跑一次 `init` 即可恢复。

### 方式一：脚本（推荐）

```bash
git clone <repo> && cd whale-relay
chmod +x start.sh
./start.sh
```

首次运行会自动：生成 `config.json` → 随机生成一个客户端 Key 并打印 → 提示你填上游密钥。
填完再跑一次即可。

### 方式二：Docker

```bash
cp config.example.json config.json   # 然后编辑填入密钥
docker compose up -d
```

### 方式三：手动

```bash
cp config.minimal.json config.json
# 编辑 config.json：把 REPLACE_WITH_YOUR_UPSTREAM_KEY 换成真实密钥
pip install PySocks          # 仅当 proxy=true 需要
python3 relay.py
```

## 验证

```bash
curl http://127.0.0.1:8090/healthz          # {"ok": true}
```

```bash
curl http://127.0.0.1:8090/v1/chat/completions \
  -H "Authorization: Bearer sk-relay-你的密钥" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku","messages":[{"role":"user","content":"hi"}],"max_tokens":20}'
```
## 客户端接入

下游客户端（Cherry Studio / NextChat / Dify / 任意 OpenAI SDK）只需填两项：

| 项 | 填什么 |
|---|---|
| **Base URL** | `http://<relay地址>:8090/v1` |
| **API Key** | 自己的客户端密钥（`python relay.py key new` 生成） |

`<relay地址>` 按部署位置替换：

| 场景 | Base URL |
|---|---|
| 与 relay 同机 | `http://127.0.0.1:8090/v1` |
| 局域网其他设备 | `http://192.168.x.x:8090/v1`（relay 机器的内网 IP，需放行 8090 端口） |
| 公网访问 | `https://你的域名/v1`（**必须走 HTTPS 反代**） |

> 默认监听 `0.0.0.0`，所有网卡可达。仅本机使用时建议 `RELAY_HOST=127.0.0.1`。

模型名请先请求 `/v1/models`，按返回列表填写。

## 端点

| 路径 | 认证 | 说明 |
|---|---|---|
| `/healthz` | 否 | 健康检查，不计数不限流 |
| `/v1/models` | 是 | 列出当前 Key 可见的模型 |
| `/v1/chat/completions` | 是 | OpenAI 协议 |
| `/v1/messages` | 是 | Anthropic 原生透传 |

## 配置文件

`config.example.json` 是完整示例，`config.minimal.json` 是最小可用配置。

```jsonc
{
  "client_keys": {
    "sk-relay-xxx": {
      "name": "local",       // 备注名，日志里用于区分
      "allow": "*",          // "*" 或 ["or","qwen"]，限定可用上游
      "rpm": 60,             // 每分钟请求上限
      "daily": 3000          // 每日请求上限
    }
  },
  "upstreams": {
    "or": {
      "host": "openrouter.ai",
      "base": "/api/v1",
      "key": "sk-or-v1-xxxx",
      "proxy": false,        // true 则经 SOCKS5 出网
      "rpm": 20,
      "daily": 500,
      "aliases": {           // 客户端模型名 → 上游真实模型名
        "claude-haiku": "anthropic/claude-haiku-5.5"
      }
    }
  }
}
```

| 上游字段 | 说明 |
|---|---|
| `host` | 域名，不带 `https://` |
| `base` | 路径前缀，如 `/api/v1` |
| `key` | 上游密钥 |
| `proxy` | 代理开关，见下方「代理配置」 |
| `aliases` | 客户端模型名 → 上游真实模型名 |

### 代理配置

`proxy` 字段支持三种写法，可**逐上游单独设置**：

```jsonc
"proxy": false                              // 不走代理
"proxy": true                               // 用全局 SOCKS（RELAY_SOCKS_HOST/PORT）
"proxy": "socks5://1.2.3.4:1080"            // 该上游专用
"proxy": "socks5h://user:pass@1.2.3.4:1080" // 带认证，h = DNS 也在代理端解析
```

启用任一写法都需先装 PySocks：`pip install PySocks`。
目前仅支持 SOCKS5（含认证），不支持 HTTP 代理。

## 可选：夜间停止对外服务（默认关闭）

relay **默认 7×24 全天对外**，不做任何时段限制。如果你只想在特定时段开放，
在 `config.json` 里加一段 `window`：

```json
"window": {
  "enabled": true,
  "open": "08:30",
  "close": "23:55"
}
```

| 写法 | 效果 |
|---|---|
| 不写 `window` 或 `enabled: false` | 全天开放（默认） |
| `open` 与 `close` 相同 | 全天开放 |
| `"open": "22:00", "close": "06:00"` | 支持跨零点 |

时段外的请求返回：

```json
{"error": {"message": "relay closed (open 08:30 - 23:55)", "code": 403}}
```

也可以用环境变量，优先级高于配置文件：

```bash
RELAY_WINDOW_OPEN_HOUR=8 RELAY_WINDOW_OPEN_MINUTE=30 \
RELAY_WINDOW_CLOSE_HOUR=23 RELAY_WINDOW_CLOSE_MINUTE=55
```

改完重启 relay 生效。时段判断使用 `TZ_OFFSET_HOURS`（默认 +8，北京时间），
不会修改系统时区。


## 可选：限流（默认不限制）

relay 默认 **不限流、不限日总量**。你的站跑什么负载、给谁用，只有你自己知道，
所以这里不预设立场——需要防护时再显式写上。

在 `config.json` 里加一段：

```json
"limits": {
  "ip_rpm": 60,
  "daily_total": 1500,
  "auth_fail_limit": 5,
  "ban_seconds": 600
}
```

| 字段 | 默认 | 含义 |
|---|---|---|
| `ip_rpm` | `0` | 每个 IP 每分钟请求数，0 = 不限 |
| `daily_total` | `0` | 全部 key 合计的每日请求上限，0 = 不限 |
| `auth_fail_limit` | `0` | 同 IP 认证失败多少次触发封禁，0 = 不封 |
| `ban_seconds` | `600` | 触发后的封禁时长 |

**任何一项填 0 就是关掉这一项**，不写 `limits` 整段等于全部关闭。

对应的环境变量（优先级更高）：

```bash
RELAY_IP_RPM=60 RELAY_DAILY_TOTAL=1500 \
RELAY_AUTH_FAIL_LIMIT=5 RELAY_BAN_SECONDS=600
```

另外，每个客户端 key 还能在 `client_keys` 里单独设 `rpm` / `daily`，
这是**按 key** 的限制，和上面的**按 IP**、**全局**限制同时生效，取最先撞到的那条。

> 注意 `daily_total` 是全局计数：一旦跑满，**所有 key 一起返回 429**，
> 第二天 0 点自动重置。如果是多个人共用，这个值要留足余量。

## 用 agent 改配置（不用手编 JSON）

所有配置项都能通过 CLI 读写，agent 和脚本可以直接调：

```bash
python relay.py config list                      # 看全部（密钥自动打码）
python relay.py config get limits.ip_rpm         # 读一项
python relay.py config set limits.ip_rpm 120     # 改一项
python relay.py config set window.enabled true
python relay.py config set timezone.offset_hours 8
python relay.py config set upstreams.or.proxy "socks5h://127.0.0.1:1080"
python relay.py config del limits.daily_total    # 删一项
```

路径用点号分隔，`set` 会自动创建中间层级，值会自动识别类型
（`true`/`false` → 布尔，`123` → 数字，`{...}` → JSON，其余当字符串）。
写入是原子的、权限 0600。**改完重启 relay 生效。**

## 时区：跟随系统

relay 不做任何时区换算，全部按**系统时区**判断。容器里如果系统是 UTC，
想把服务时段按北京时间算，就去设容器的 `TZ=Asia/Shanghai`（或挂载 `/etc/localtime`），
不需要改 relay 的任何配置。

## 代理：默认直连

每个上游独立配置，**默认 `false`**（直连）：

```json
"proxy": false                              // 不走代理（默认）
"proxy": "socks5h://127.0.0.1:1080"         // 代理端解析 DNS（推荐）
"proxy": "socks5://user:pass@host:1080"     // 本地解析 DNS，支持认证
"proxy": true                               // 用全局 RELAY_SOCKS_HOST/PORT
```

走 SOCKS5 需要额外装依赖：

```bash
pip install PySocks
```

**没装 PySocks 又配了代理，启动时会直接报错退出**，不会带着错误配置跑起来。
## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `RELAY_HOST` | `0.0.0.0` | 监听地址 |
| `RELAY_PORT` | `8090` | 监听端口 |
| `RELAY_CONFIG` | `./config.json` | 配置路径 |
| `RELAY_SOCKS_HOST` | `127.0.0.1` | SOCKS5 地址 |
| `RELAY_SOCKS_PORT` | `1080` | SOCKS5 端口 |
| `RELAY_LOG_DIR` | `./logs` | 日志目录 |
| `RELAY_REQUEST_TIMEOUT` | `30` | 单请求 socket 超时（秒） |
| `RELAY_TRUST_PROXY` | `0` | 设为 `1` 才采信 `X-Forwarded-For`（取最右一跳） |
| `RELAY_WINDOW_OPEN_HOUR` / `_MINUTE` | `0` / `0` | 服务时段起点；与终点相同 = 全天开放 |
| `RELAY_WINDOW_CLOSE_HOUR` / `_MINUTE` | `0` / `0` | 服务时段终点，支持跨零点 |
| `RELAY_INSECURE_TLS` | `0` | `1` 关闭上游 TLS 证书校验（不建议） |
| `RELAY_MAX_BODY_BYTES` | `1048576` | 客户端请求体上限（字节） |
| `RELAY_MAX_RESP_BYTES` | `16777216` | 上游响应上限（字节），防内存撑爆 |
| `RELAY_MAX_CONCURRENT` | `0` | 最大并发连接，0 为不限，超出回 503 |


## 模型路由

前缀决定走哪个上游，映射表在 `relay.py` 顶部的 `MODEL_ROUTE`。
未命中的模型走 `DEFAULT_UPSTREAM`。

## 安全须知

⚠️ **这不是生产级网关**，是自用工具。公开部署前务必读完：

1. **必须放在 HTTPS 反代后面**。默认是明文 HTTP，直接暴露公网等于把
   客户端 Key 和全部对话内容裸奔。本地验证时请显式 `RELAY_HOST=127.0.0.1`。
2. **上游 TLS 默认做完整校验**。设置 `RELAY_INSECURE_TLS=1` 会关闭校验，
   这意味着上游密钥在链路上可被中间人截获，除非你完全清楚后果，否则不要开。
3. **`RELAY_TRUST_PROXY` 默认关闭**。只有在 Nginx / CDN 后面才打开，
   否则任何人都能伪造 `X-Forwarded-For` 绕过限流。
4. **`config.json` 永不入版本库**，已在 `.gitignore` 屏蔽。文件权限建议 `600`。
5. **所有上游密钥集中在一台机器**，这台机器被攻破 = 全部上游沦陷。
6. **访问日志不含请求体**，但会记录模型名与 IP；如涉敏感场景请自行脱敏。
7. 内置限流、封禁只是基础防护，不是 WAF。对外开放前请自行加审计。
8. `client_keys` 里的 `rpm` / `daily` 是**每个客户端密钥独立**的额度，叠加在
   IP 限流与全局日限之上；不填则不限制。

## 风险与免责

- 本工具本质是**可信中间人**：它能看到经过它的每一条 prompt。不要用它
  转发你不信任的第三方的隐私数据。
- 转发/转售上游 API 往往**违反上游服务条款**（OpenAI / Anthropic / OpenRouter 等
  普遍禁止转售），可能导致上游封号。请自行评估后再使用。
- 本项目仅供个人学习与自用。使用前请确认所接入上游的条款及所在地区规定。
  作者不承担任何使用后果。

## 自用参考（非推广）

以下是我个人在用的接入渠道，仅作存档记录，不构成推荐。

> 价格、支付方式、可用性均以各官网当前公示为准。
> 分销类服务存在停止服务、账号封禁等风险，请自行控制余额，勿大额充值。

| 渠道 | 类型 | 支付方式（以官网为准） | 备注 |
|---|---|---|---|
| [OpenRouter](https://openrouter.ai) | 官方聚合平台 | 信用卡、加密货币 | 有公司主体，定价公开，美元计价 |
| [非线智能](https://api.nonelinear.com) | API 转发 | 支付宝（免手续费） | 支付方便，适合小额 |
| [BazaarLink](https://bazaarlink.ai/) | API 转发 | 微信、PayPal、加密货币 | 以美元计价，含手续费与服务费，建议小额充值 |

以上仅为个人使用记录，不评价稳定性，不提供任何担保。

## License

MIT

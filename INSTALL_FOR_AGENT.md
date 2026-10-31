# 给 AI Agent 的安装说明

> 你正在替用户部署 whale-relay（一个 API 中转服务）。
> **本文档只讲执行，不讲背景。** 严格按步骤走。
> 看到 `⛔ 停下来问` 标记，必须停止并告知用户缺什么，不要用占位符继续。

---

## 0. 动手前，确认这三个文件存在

```bash
ls relay.py config.minimal.json requirements.txt
```

三个都在 → 继续。缺任何一个 → `⛔ 停下来问`：仓库不完整。

> 提醒：`config.json`（由 init 生成）**含明文密钥**，之后任何步骤都不要提交、
> 上传或粘贴到聊天里。

**然后读配置模板，确定上游名：**

```bash
python3 -c "import json;d=json.load(open('config.minimal.json',encoding='utf-8'));print(list(d['upstreams'].keys()))"
```

输出形如 `['or']`。记住这些名字，第 3 步要用。

---

## 1. 检查 Python（不满足就停）

```bash
python3 --version
```

要求 **3.8 或更高**。

- 输出版本号 ≥ 3.8 → 继续
- 命令不存在 → 试 `python --version`、`py --version`
- 都不行 → `⛔ 停下来问`：需要用户安装 Python 3.8+
- Windows 上 `python` 弹出应用商店 → 改用 `py`

本项目**只需要标准库**，正常情况不需要 `pip install`。

`requirements.txt` 里只有一个**可选**依赖 `PySocks`：
仅当配置中某个上游写了 `"proxy": "socks5..."` 或 `"proxy": true` 时才需要它。
第 5 步的 `check` 与启动报错会提示你是否缺这个包。

---

## 2. 决定绑定地址（安全相关的硬性判断）

问用户一句话：

> 「你的客户端和这台机器是同一台吗？还是要从别的设备连进来？」

对应处理：

| 场景 | 设置 |
|---|---|
| 同一台机器 | `RELAY_HOST=127.0.0.1`（**默认，最安全**） |
| 同一局域网的其他设备 | `RELAY_HOST=0.0.0.0`，并告知用户需要防火墙放行 |
| 公网访问 | `⛔ 停下来问`：必须先有 HTTPS 反向代理，不允许直接暴露 8090 |

不知道答案时，**用 `127.0.0.1`**。事后改一个环境变量即可。

---

## 3. 获取上游密钥（必须由用户提供）

```bash
python3 -c "import json;d=json.load(open('config.minimal.json',encoding='utf-8'));[print(k) for k in d['upstreams']]"
```

为每个上游名准备对应环境变量，规则是 `UPSTREAM_KEY_<上游名大写>`：

| 上游名 | 环境变量 |
|---|---|
| `or` | `UPSTREAM_KEY_OR` |
| `qwen` | `UPSTREAM_KEY_QWEN` |
| `zhipu` | `UPSTREAM_KEY_ZHIPU` |

**至少提供一个。** 一个都没有 → `⛔ 停下来问`：

> 「需要你提供至少一个上游的 API 密钥（例如 OpenRouter 的 key），没有密钥服务即使启动也无法转发请求。」

**绝对不要**用 `sk-xxx`、`your-key-here` 之类占位符继续。

---

## 4. 初始化配置

`init` 会做三件事：从 `config.minimal.json` 复制出 `config.json`、把密钥写进去、
生成一个客户端密钥。**之后启动只读 `config.json`，不再需要这些环境变量。**

**方式 A（bash / zsh，密钥不进 shell 历史）——仅适用于有人值守：**
（无人值守的 agent 不要用这条，`read -rsp` 会等输入而挂起；改用方式 B）

```bash
read -rsp "上游密钥: " UPK && export UPSTREAM_KEY_OR="$UPK" && unset UPK
python3 relay.py init
```

**方式 B（一条命令，密钥会留在 shell 历史里）：**

```bash
UPSTREAM_KEY_OR='真实密钥' python3 relay.py init
```

**方式 C（PowerShell）：**

```powershell
$env:UPSTREAM_KEY_OR = "真实密钥"
python3 relay.py init
Remove-Item Env:UPSTREAM_KEY_OR
```

> 密钥会写入 `config.json`。该文件**含明文密钥，绝不能提交到 git**。

（多个上游就写多个环境变量，用空格隔开。）

**预期输出包含：**

```
[i] 已保存 1 个上游：or
[i] 已生成客户端密钥……
    sk-relay-xxxxxxxxxxxxxxxxxxxxxxxx
[i] 启动服务：python relay.py
```

**把这个 `sk-relay-...` 记录下来**，用户以后要用它连接。

**没有看到密钥输出**时：

```bash
python3 relay.py key list        # 打码显示，只能确认"有密钥"
python3 relay.py key show sk-relay-   # 显示完整密钥（前缀至少 8 位）
```

`key list` 故意打码，**要完整密钥必须用 `key show`**。
如果列表为空，说明 init 没成功，回第 3 步检查密钥。

**如果用户中途按了 Ctrl+C**，配置不会被写入。重新执行本步骤即可，不会产生半成品。

---


> **时段限制是可选项，默认不启用。** relay 默认全天对外，不会自己在半夜关门。
> 只有 `config.json` 里 `window.enabled = true` 才会限时，详见 README「夜间停止对外服务」。

> **限流默认全部关闭。** `limits` 不写就是不限速、不限量，装完不会突然被 429。
> 需要防护再按 README「可选：限流」填数字。

> **所有配置都能用 CLI 改，不必手编 JSON：**
> `python relay.py config set <路径> <值>`，路径如 `limits.ip_rpm`、`window.enabled`、
> `upstreams.or.proxy`。改完重启生效。

## 5. 自检（必须通过才能启动）

```bash
python3 relay.py check
```

**通过的样子：**

```
[✓] 配置检查通过（1 个客户端密钥，1 个上游）
```

**没通过：** 按输出的 `[x]` 逐条修。常见三条：

| 报错 | 处理 |
|---|---|
| `上游 X 的 key 未填写` | 指 **config.json 里**该上游的 key 为空。回第 3 步补环境变量后重跑 init |
| `client_keys 缺失或为空` | 回第 4 步 |
| `配置不是合法 JSON` | 文件被手工改坏了，重新跑 init |

`[!]` 开头的警告不阻断启动。

---

## 6. 启动

⚠️ **本文件是给 agent 看的，直接用下面这一条，不要改，不要用前台方式跑。**

```bash
mkdir -p logs
RELAY_HOST=127.0.0.1 RELAY_PORT=8090 setsid python3 relay.py > logs/stdout.log 2>&1 < /dev/null &
sleep 3
cat logs/stdout.log
```

三个细节**一个都不能省**：

| 写法 | 省了会怎样 |
|---|---|
| `setsid` | 进程绑在当前会话上，执行器一超时就跟着死 |
| `> logs/stdout.log 2>&1` | 输出灌进管道，执行器一直等 |
| `< /dev/null` | **执行器等 stdin 关闭，这条命令永不返回** |

**启动成功的标志**（两个都要有）：

```bash
cat logs/stdout.log | grep 监听
curl -s http://127.0.0.1:8090/healthz
# 期望: {"ok": true}
```

**如果 3 秒后 healthz 连不上**：先 `cat logs/stdout.log` 看报错，
**不要重复启动**——端口会被占，表现为 `Address already in use`。

Windows 用户用 PowerShell，其余步骤相同：

```powershell
New-Item -ItemType Directory -Force -Path logs | Out-Null
$env:RELAY_HOST="127.0.0.1"; $env:RELAY_PORT="8090"
$p = Start-Process -NoNewWindow -PassThru python -ArgumentList "relay.py" -RedirectStandardOutput "logs\stdout.log"
$p.Id | Out-File -Encoding ascii relay.pid
```

---

## 7. 验收（三条全过才算装好）

```bash
# ① 存活
curl -s http://127.0.0.1:8090/healthz
# 期望: {"ok": true}

# ② 鉴权生效（错误密钥应被拒）
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8090/v1/models -H "Authorization: Bearer wrong"
# 期望: 401

# ③ 正确密钥能拿到模型表
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8090/v1/models -H "Authorization: Bearer sk-relay-你记录的密钥"
# 期望: 200

# ③b 但 200 可能是假的！两条判据，任选其一
curl -s -D - -o /dev/null http://127.0.0.1:8090/v1/models \
  -H "Authorization: Bearer sk-relay-你记录的密钥" | grep X-Relay-Models-Source
# 期望: X-Relay-Models-Source: upstream
# 若是 local-fallback = 上游没连上，模型表是本地别名
# 响应头里还会带 X-Relay-Fallback-Reason 说明具体原因

grep MODELS_FETCH_FAIL logs/access.log
# 期望: 没有输出

# ④ 可选：验证上游密钥是否真的有效（会真实调用，可能产生费用，需用户同意）
curl -s -m 30 http://127.0.0.1:8090/v1/chat/completions \
  -H "Authorization: Bearer sk-relay-你记录的密钥" -H "Content-Type: application/json" \
  -d '{"model":"<配置里的模型名>","messages":[{"role":"user","content":"ping"}],"max_tokens":5}'
# 期望: 200 且返回内容；若返回 502/超时，说明上游密钥或网络有问题
```

> ⚠️ **第 ③ 步返回 200 不代表上游密钥有效。**
> relay 在上游不可达时会**降级为本地别名表**，`/v1/models` 仍然返回 200 和一串模型名。
>
> **实测**：把上游指向一个 TLS 握手失败的地址，`/v1/models` 照样返回 **66 个模型**，
> 日志里是 `MODELS_FETCH_FAIL` + `MODELS n=66`。所以 ③ 单独通过毫无意义。
>
> 判据：**模型数异常多（60+）且 ③b 命中 `MODELS_FETCH_FAIL`，就是本地兜底。**
> 想确认上游真的能用，必须看第 ④ 步。

三条（或四条）都对 → **装好了**。把 `sk-relay-...` 和访问地址交给用户。

> 不要为了验证而真实调用付费模型，除非用户明确同意。第 ③ 步不花钱。

---

## 环境差异提示

部分容器/精简系统没有 `ps`、`pgrep`、`netstat`。检查进程改用：

```bash
for p in /proc/[0-9]*; do tr '\0' ' ' < $p/cmdline 2>/dev/null | grep -q relay.py && echo ${p#/proc/}; done
```

检查端口改用：

```bash
timeout 2 bash -c '</dev/tcp/127.0.0.1/8090' && echo 在听 || echo 没听
```

## 失败对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动即退出，提示 `找不到 config.json` | 没跑 init | 回第 4 步 |
| `Address already in use` | 端口被占 | 换 `RELAY_PORT=8091`，或停掉旧进程 |
| healthz 200 但 models 401 | 用了错误密钥 | 核对 `key list` |
| models 502 | 上游不可达 / 密钥无效 | 检查网络与该上游密钥 |
| models 200 但对话 502 | 模型表来自本地兜底，上游其实没通 | 核对上游密钥、代理、网络 |
| `ModuleNotFoundError: socks` | 配置启用了代理 | `pip install PySocks` 或把该上游 `"proxy"` 改 `false` |
| 配置读不了，提示 BOM | 文件被记事本改过 | 用 `python relay.py check` 重新确认，必要时备份后重跑 init |

---

## 绝对不要做的事

1. **不要**把 `config.json` 提交到 git、上传、或写进日志
2. **不要**把上游密钥打印到终端历史里（本工具的 `key list` 已打码）
3. **不要**默认绑 `0.0.0.0`——先问用户
4. **不要**在没有 HTTPS 反代的情况下把端口暴露到公网
5. **不要**用占位符密钥「先跑起来再说」
6. **不要**修改 `relay.py` 源码来绕过检查

---

## 管理命令速查

```bash
python3 relay.py key new --name 名字 --rpm 60 --daily 1000 --allow or
python3 relay.py key list
python3 relay.py key show <至少8位前缀>
python3 relay.py key del <至少8位前缀>
python3 relay.py check
```

新密钥和吊销**都要重启服务才生效**。

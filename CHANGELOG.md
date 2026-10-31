# 变更记录

## 2026-10-31 — 安全审计轮（sonnet + gpt-5.5 双复核）

### 审计过程
- **opus-5.5**：3 轮全失败（思考过程耗尽 max_tokens / 超时），花费 ¥3.11，零产出
  - 教训：带思考的模型必须先给思考预留额度，不能只按「我要多少输出」给 max_tokens
- **sonnet-5.5**：一次成功，审出 12 条（¥0.68）
- **gpt-5.5**：在修复后的版本上复核，判定 sonnet 结论 + 新增 6 条（¥1.67）

### 代码修复（13 项）

| # | 问题 | 严重度 | 处理 |
|---|---|---|---|
| 1 | 任意 `/v1/*` 都可转发到上游 | **P1** | 白名单仅 `chat/completions`、`messages`、`completions` |
| 2 | 上游响应体无读取上限 | P1 | 新增 `_read_capped()`，默认 16MB |
| 3 | 无并发连接上限（线程耗尽 DoS） | P1 | 信号量，超出回 503，`RELAY_MAX_CONCURRENT` 可调 |
| 4 | 流式写失败仍尝试返回 502 | P1 | 捕获 `BrokenPipe/ConnectionReset`，记 `CLIENT_ABORT` |
| 5 | 上游未禁用压缩，可能干扰 SSE 分帧 | P1 | 请求头加 `Accept-Encoding: identity` |
| 6 | `config set` 不校验类型 | P1 | `_CFG_TYPES` 表，写错直接拒绝 |
| 7 | `MAX_BODY_BYTES` 硬编码 | P2 | `RELAY_MAX_BODY_BYTES` 环境变量 |
| 8 | `_fetch_models()` 读取无上限 | P2 | 改用 `_read_capped(resp, 8MB)` |
| 9 | models 缓存无锁（并发穿透） | P2 | 新增 `_models_lock` |
| 10 | `allow` 字段拒绝合法列表 | P2 | 类型放宽为 `(str, list)` |
| 11 | `config list` 未遮蔽 `free_key` / 代理凭据 | P2 | 统一遮蔽 `key`、`free_key`、`//user:pass@` |
| 12 | 配额在请求校验前扣减 | P2 | 移到 JSON 校验 + 上游解析之后 |
| 13 | `MAX_RESP_BYTES` 新增 | — | 环境变量，默认 16MB |

### 文档
- `AGENTS.md` 新增「部署与运维须知」：日志轮转、配置备份、代理边界、路径白名单、脱敏规则、配额扣减时机
- `README.md` 环境变量表补 3 项

### 未修复（有意保留）
- **认证时序侧信道**：自建站风险极低，改动会动热路径
- **限流三处检查非原子**：属「近似限流」设计，已在文档定性
- **`/v1/models` 缓存惊群**：最多多发一次上游请求，影响可忽略
- **SSRF（`upstream.host` 可配任意地址）**：这是 relay 的能力不是漏洞，公网部署需网络隔离

### 成本
| 项 | 花费 |
|---|---|
| opus 三轮（全空） | ¥3.11 |
| sonnet 审计 | ¥0.68 |
| gpt-5.5 复核 | ¥1.67 |
| **合计** | **¥5.46** |

### 验证
- 单元：`config set` 类型拒绝、`allow` 列表、脱敏泄露检查（False）
- 黑盒：`/v1/chat/completions` → 502（假上游正常转发）、`/v1/unknown` → 404、`/v1/files` → 404
- 语法：`ast.parse` 通过

## 2026-10-31 — 生产 relay 移植（whale_relay_v2.py）

按「不影响使用 + 性能优先」原则移植，改动全部经验证零回归。

### 已移植
| 项 | 做法 | 性能影响 |
|---|---|---|
| 上游连接失败 | 捕获 `ConnectionError/TimeoutError/OSError/gaierror/HTTPException` 返回 502（原来直接断连） | 无 |
| 响应体上限 | `_read_capped()` 默认 64MB（生产内容可能较大，故放宽） | 分块读取，可忽略 |
| 请求体上限 | `RELAY_MAX_BODY_BYTES` 环境变量，默认仍 1MB | 无 |
| 并发上限 | 信号量，默认 **128**（自用远达不到，只防线程堆积） | acquire/release 纳秒级 |
| 流式写保护 | 捕获 `BrokenPipe/ConnectionReset` 记 `CLIENT_ABORT` | 无 |
| SSE 压缩 | **仅流式**请求加 `Accept-Encoding: identity`，非流式保持默认可压缩 | 非流式带宽不变 |
| models 缓存 | 加锁 + 8MB 读取上限 | 仅 /v1/models 路径 |
| 配额扣减 | daily 挪到 JSON 校验 + model 解析之后 | 无 |

### 有意没做
- **路径硬白名单**：生产日志显示实际使用 `/api/v1/`、`/compatible-mode/v1/` 等多种前缀，
  硬拦会误伤。改为「未知路径只记录不拦截」。
- **config set 类型校验**：生产版没有 config CLI，不适用。

### 验证
- 对照测试（备份版 vs 新版）：`/health` `/v1/models` 均 200，`/v1/chat/*` 行为一致
- 新版改进：上游不可达时返回 **502**（原为连接直接断开 / curl 000）
- 坏 JSON → 400，无未捕获异常

### 备份
- `whale_relay_v2.py.bak_sec_20261030_165255`

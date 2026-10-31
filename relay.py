#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
whale-relay —— 单文件 OpenAI 兼容中转服务

特性：
  · 对外提供 /v1/chat/completions、/v1/messages（Anthropic 原生透传）
  · 多上游路由（按模型名前缀分流到不同供应商）
  · 客户端 Key 分级（每个 key 可限定可用的上游、RPM、每日额度）
  · 基础防护：IP 限流、认证失败封禁、请求体上限、访问日志
  · 纯标准库实现，唯一可选依赖：PySocks（仅在使用 SOCKS5 代理时需要）

依赖：Python 3.8+
配置：同目录 config.json（参考 config.example.json）
"""
import json
import secrets
import ssl
import socket
import http.client
import http.server
import socketserver
import sys
import time
import threading
import os
import re
import posixpath
import shutil
import tempfile
try:
    import getpass
except Exception:
    getpass = None
import urllib.parse
from collections import defaultdict, deque

try:                      # 可选依赖：仅当上游配置里 proxy=true 时才需要
    import socks as sockslib
    SOCKS_AVAILABLE = True
except ImportError:
    sockslib = None
    SOCKS_AVAILABLE = False

# ===== Claude 协议（OpenRouter 原生透传）=====
# 说明：早期实现的协议转换已移除，当前直接透传原生协议。
# 现在 OpenRouter 原生支持 /api/v1/messages，无需任何转换，直接透传。

def is_anthropic_path(path):
    """是否是 Anthropic 原生协议路径"""
    return path.startswith("/v1/messages")


def is_claude_or(model):
    """哪些模型走 Anthropic 原生透传

    · claude-*（不带 nl/ 前缀）→ OpenRouter 原生 /api/v1/messages
    · nl/claude-*              → 备用通道，走 OpenAI 协议（该上游不支持 /v1/messages）
    · 其他模型                  → 一律 OpenAI 协议
    """
    if not model:
        return False
    low = model.lower()
    return "claude" in low and not low.startswith("nl/")

# ============ 配置 ============
_HERE = os.path.dirname(os.path.abspath(__file__))
LISTEN_HOST = os.environ.get("RELAY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("RELAY_PORT", "8090"))

# （旧的单 key 变量已移除，统一走 keys.json 里的 CLIENT_KEYS）

# ====== v2：Key 分级（按上游隔离）======
# ====== 密钥与上游配置（外置到 keys.json，权限 600）======
# 代码本身不含任何密钥，可安全备份/分享
KEYS_PATH = os.environ.get("RELAY_CONFIG") or os.path.join(_HERE, "config.json")


# 带子命令时进入 CLI 管理模式，此时允许配置缺失（init 需要从零创建）
_CLI_MODE = len(sys.argv) > 1


def _load_keys():
    """读取 client_keys 与 upstreams。

    启动服务时配置缺失/不完整 → 直接拒绝启动。
    CLI 管理模式 → 返回空表，由具体命令自行处理。
    """
    if not os.path.exists(KEYS_PATH):
        if _CLI_MODE:
            return {}, {}
        raise SystemExit("[FATAL] 找不到 %s，relay 无法启动（先运行 init）" % KEYS_PATH)
    try:
        with open(KEYS_PATH, encoding="utf-8-sig") as f:   # 兼容记事本 BOM
            d = json.load(f)
    except Exception as e:
        raise SystemExit("[FATAL] 读取 %s 失败：%s" % (KEYS_PATH, e))
    ck = d.get("client_keys") or {}
    up = d.get("upstreams") or {}
    if isinstance(up, dict):
        # disabled 的上游保留在配置里但不下发，方便随时重新启用
        up = {k: v for k, v in up.items()
              if not (isinstance(v, dict) and v.get("disabled"))}
    if (not ck or not up) and not _CLI_MODE:
        raise SystemExit("[FATAL] %s 内容不完整（缺少 client_keys 或 upstreams）" % KEYS_PATH)
    return ck, up


CLIENT_KEYS, UPSTREAMS = _load_keys()



# ====== 上游配置（v2：国内分流）======
SOCKS_HOST = os.environ.get("RELAY_SOCKS_HOST", "127.0.0.1")
SOCKS_PORT = int(os.environ.get("RELAY_SOCKS_PORT", "1080"))



MODELS_TIMEOUT = int(os.environ.get("RELAY_MODELS_TIMEOUT", "5"))
MODELS_CACHE_TTL = int(os.environ.get("RELAY_MODELS_CACHE_TTL", "300"))
_MODELS_CACHE = {"at": 0.0, "upstream": None, "data": [], "ok": None, "reason": None}

def _allow_ok(allow, up):
    """allow 允许 "*" 或上游名单列表/字符串。字符串按整体匹配，不做子串匹配。"""
    if allow is None or allow == "*":
        return True
    if isinstance(allow, str):
        return allow == up
    try:
        return up in allow
    except TypeError:
        return False


def build_upstream_path(base, sub):
    """拼接上游路径并规范化，拒绝 .. 穿越与越出 base 的路径"""
    q = ""
    if "?" in sub:
        sub, q = sub.split("?", 1)
        q = "?" + q
    base_c = "/" + base.strip("/")
    joined = posixpath.normpath(base_c + "/" + sub.lstrip("/"))
    if not (joined == base_c or joined.startswith(base_c + "/")):
        raise ValueError("illegal upstream path")
    return joined + q


DEFAULT_UPSTREAM = "or"

# ====== 模型路由：带前缀的走国内，未列出的走 OpenRouter ======
MODEL_ROUTE = {
    # 特殊映射示例：客户端名与实际上游模型不同
    "nl/grok-4.5": "qwen",
    # 上游 A（国内兼容模式）
    "qwen/glm-5.3":          "qwen",
    "qwen/glm-5.3-prime":    "qwen",
    "qwen/glm-5.3-flash":    "qwen",
    "qwen/glm-5.3-flashx":   "qwen",
    "qwen/qwen3.8-max":      "qwen",
    "qwen/qwen3.8-flash":    "qwen",
    "qwen/qwen3.6-flash":    "qwen",
    "qwen/kimi-k3":          "qwen",
    "qwen/minimax-m3":       "qwen",
    "qwen/mimo-v2.6-pro":    "qwen",
    "qwen/deepseek-v4-pro":     "qwen",
    "qwen/deepseek-v4.1-flash": "qwen",
    # 上游 B
    "zhipu/glm-4.7":         "zhipu",
    "zhipu/glm-4.5-air":     "zhipu",
    # 上游 C（备用通道）
    "nl/claude-opus-5.5":    "nl",   # 旗舰·最强
    "nl/claude-fable-5":     "nl",   # 旗舰·创作
    "nl/claude-sonnet-5.5":  "nl",   # 旗舰·均衡
    "nl/claude-haiku-5.5":   "nl",   # 便宜·日常
    "nl/claude-sonnet-5":    "nl",   # 便宜·中档
    "nl/claude-haiku-4.5":   "nl",   # 便宜·最省
    "nl/gemini-4-argon":     "nl",   # Gemini 4 旗舰（活动价）
    "nl/gemini-3.8-flash":   "nl",   # Gemini 便宜
    "nl/gemini-3.1-pro-preview": "nl",
    "nl/gemini-2.5-pro":     "nl",
    "nl/gemini-3-flash-preview": "nl",
    # 上游 C · GPT 系列
    "nl/gpt-6.1-sol":        "nl",
    "nl/gpt-6-astra":        "nl",
    "nl/gpt-6-sol":          "nl",
    "nl/gpt-6-luna":         "nl",
    "nl/gpt-5.5-pro":        "nl",
    "nl/gpt-5.4-mini":       "nl",
    # 上游 D
    "doubao/seed-2.1-pro":   "doubao",
    "doubao/seed-2.1-turbo": "doubao",
    "doubao/seed-2.1-lite":  "doubao",
}

# 部分上游的模型名带日期后缀，用别名表维护真实名
# 上游 A 别名表（须与 MODEL_ROUTE 对应，否则前缀会原样发出）

# 上游 C：去掉前缀即可

# 上游 B 别名

# 旧字段保留（兼容）。CLI 管理模式下配置可能为空，这里不能硬取下标。
_du = UPSTREAMS.get(DEFAULT_UPSTREAM) or {}
UPSTREAM_HOST = _du.get("host", "")
UPSTREAM_KEY = _du.get("key", "")

# 防护参数
def _limits_from_config():
    """从配置文件 limits 段读取限流参数。

    默认全部为 0 = 不限制。自建站不应该被别人的调参结果绑架：
    需要哪层防护，由使用者自己在 limits 里显式写。0 也用于单独关掉某一项。
    """
    try:
        with open(KEYS_PATH, encoding="utf-8") as f:
            lim = (json.load(f) or {}).get("limits") or {}
    except Exception:
        lim = {}

    def _n(key, default=0):
        try:
            return max(0, int(lim.get(key, default)))
        except Exception:
            return default

    return (_n("ip_rpm"), _n("daily_total"),
            _n("auth_fail_limit"), _n("ban_seconds", 600))

_L_IP_RPM, _L_DAILY, _L_AUTHFAIL, _L_BANSEC = _limits_from_config()
RATE_LIMIT_PER_MIN = int(os.environ.get("RELAY_IP_RPM", str(_L_IP_RPM)))
DAILY_REQUEST_LIMIT = int(os.environ.get("RELAY_DAILY_TOTAL", str(_L_DAILY)))
AUTH_FAIL_LIMIT = int(os.environ.get("RELAY_AUTH_FAIL_LIMIT", str(_L_AUTHFAIL)))
AUTH_BAN_SECONDS = int(os.environ.get("RELAY_BAN_SECONDS", str(_L_BANSEC)))
MAX_BODY_BYTES = int(os.environ.get("RELAY_MAX_BODY_BYTES", str(1024 * 1024)))
# 上游响应体读取上限：防止异常上游返回超大 body 撑爆内存（0 = 不限制）
MAX_RESP_BYTES = int(os.environ.get("RELAY_MAX_RESP_BYTES", str(16 * 1024 * 1024)))
# 最大并发连接数：0 = 不限制（默认），设了之后超出直接 503，防线程耗尽
MAX_CONCURRENT = int(os.environ.get("RELAY_MAX_CONCURRENT", "0"))
# 只允许这些路径转发到上游，避免认证用户借用 relay 打别的端点
ALLOWED_POST_PATHS = {"/v1/chat/completions", "/v1/messages", "/v1/completions"}  # 1MB

# 时区换算（系统保持 UTC，只在这里做偏移；改这个数就能换时区）
# 时区：完全跟随系统，不做任何偏移。
# 容器里若是 UTC 又想按本地时间判断时段，请设置容器/系统时区
# （TZ=Asia/Shanghai 或挂载 /etc/localtime），relay 本身不提供偏移配置。
TZ_OFFSET_HOURS = 0

# 服务时段：默认全天开放，可用 RELAY_WINDOW_* 限制
def _window_from_config():
    """从 config.json 的 window 段读取服务时段。

    默认不设限（全天开放）：open 与 close 相同即视为全天。
    想在夜间停服就设 {"window": {"enabled": true, "open": "08:30", "close": "23:55"}}。
    """
    p = os.environ.get("RELAY_CONFIG") or os.path.join(_HERE, "config.json")
    try:
        with open(p, encoding="utf-8") as f:
            w = (json.load(f) or {}).get("window") or {}
        if not w.get("enabled"):
            return 0, 0, 0, 0
        def hm(s, dh, dm):
            h, m = str(s).strip().split(":")
            return int(h) % 24, int(m) % 60
        try:
            oh, om = hm(w.get("open") or "08:30", 8, 30)
            ch, cm = hm(w.get("close") or "23:55", 23, 55)
        except Exception:
            oh, om, ch, cm = 8, 30, 23, 55
        return oh, om, ch, cm
    except Exception:
        return 0, 0, 0, 0
_W_OH, _W_OM, _W_CH, _W_CM = _window_from_config()
WINDOW_OPEN_HOUR = int(os.environ.get("RELAY_WINDOW_OPEN_HOUR", str(_W_OH)))
WINDOW_OPEN_MINUTE = int(os.environ.get("RELAY_WINDOW_OPEN_MINUTE", str(_W_OM)))
WINDOW_CLOSE_HOUR = int(os.environ.get("RELAY_WINDOW_CLOSE_HOUR", str(_W_CH)))
WINDOW_CLOSE_MINUTE = int(os.environ.get("RELAY_WINDOW_CLOSE_MINUTE", str(_W_CM)))

LOG_DIR = os.environ.get("RELAY_LOG_DIR", os.path.join(_HERE, "logs"))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "access.log")     # 总日志
LOG_PATH_OR = os.path.join(LOG_DIR, "upstream-a.log")   # 上游 A 专用
LOG_PATH_CN = os.path.join(LOG_DIR, "upstream-b.log")   # 上游 B 专用

MODEL_ALIAS = {
    # ---------- Claude ----------
    "claude-haiku-5.5":        "anthropic/claude-haiku-5.5",
    "claude-sonnet-5.5":       "anthropic/claude-sonnet-5.5",
    "claude-opus-5.5":         "anthropic/claude-opus-5.5",
    "claude-fable-5.1":        "anthropic/claude-fable-5.1",

    # ---------- OpenAI ----------
    "gpt-6-luna":              "openai/gpt-6-luna",
    "gpt-6-sol":               "openai/gpt-6-sol",
    "gpt-6.1-sol":             "openai/gpt-6.1-sol",
    "gpt-6-astra":             "openai/gpt-6-astra",
    "gpt-6-astra-pro":         "openai/gpt-6-astra-pro",
    "gpt-oss-20b":             "openai/gpt-oss-20b",

    # ---------- Google Gemini ----------
    "gemini-2.5-lite":         "google/gemini-2.5-flash-lite",
    "gemini-3.1-lite":         "google/gemini-3.1-flash-lite",
    "gemini-3-flash":          "google/gemini-3-flash-preview",
    "gemini-2.5-pro":          "google/gemini-2.5-pro",
    "gemini-3.1-pro":          "google/gemini-3.1-pro-preview",

    # ---------- xAI Grok ----------
    "grok-build-0.1":          "x-ai/grok-build-0.1",
    "grok-4.20":               "x-ai/grok-4.20",
    "grok-4.7":                "x-ai/grok-4.7",

    # ---------- 免费兜底（2026-10-29）----------
    "or/free":                 "openrouter/free",

    # ---------- 国产（v2 已迁到国内直连，见 MODEL_ROUTE）----------
    # 以下保留仅作兼容；实际走国内上游

    # ---------- 最便宜 ----------
    "cheap-glm":               "z-ai/glm-5.3-flash",
    "cheap-nex":               "nex-agi/nex-n2.5-mini",
    "cheap-gemma":             "google/gemma-3-4b-it",
    "cheap-glm4.7":            "z-ai/glm-4.7-flash",
    "cheap-qwen":              "qwen/qwen3.5-flash-02-23",
    "cheap-nex-pro":           "nex-agi/nex-n2.5-pro",
}

# 价格标签（只用于列表展示，不影响调用）
PRICE_TAG = {
    "claude-haiku-5.5":   "$0.1/$0.5",
    "claude-sonnet-5.5":  "$2/$10",
    "claude-opus-5.5":    "$4/$20",
    "claude-fable-5.1":   "$10/$50",
    "gpt-6-luna":         "$0.1/$0.5",
    "gpt-6-sol":          "$2/$10",
    "gpt-6.1-sol":        "$2/$10",
    "gpt-6-astra":        "$10/$50",
    "gpt-6-astra-pro":    "$10/$50",
    "gpt-oss-20b":        "$0.02/$0.09",
    "gemini-2.5-lite":    "$0.1/$0.4",
    "gemini-3.1-lite":    "$0.25/$1.5",
    "gemini-3-flash":     "$0.5/$3",
    "gemini-2.5-pro":     "$1.25/$10",
    "gemini-3.1-pro":     "$2/$12",
    "grok-build-0.1":     "$1/$2",
    "grok-4.20":          "$1.25/$2.5",
    "grok-4.7":           "$2/$6",
    "glm-5.3-flash":      "$0.014/$0.16",
    "glm-5.3-flashx":     "$0.37/$1.25",
    "qwen-3.7-flash":     "$0.03/$0.13",
    "qwen-3.8-max":       "$4/$12",
    "kimi-k3":            "$0.24/$11.9",
    "kimi-k2.7-code":     "$0.67/$3.35",
    "deepseek-v4.1":      "$0.3/$1.2",
    "minimax-m3":         "$0.3/$1.2",
    "seed-2.1-turbo":     "$0.5/$2.5",
    "mimo-v2.6-pro":      "$0.44/$0.87",
    "cheap-glm":          "$0.014/$0.16",
    "cheap-nex":          "$0.025/$0.1",
    "cheap-gemma":        "$0.04/$0.05",
    "cheap-glm4.7":       "$0.06/$0.4",
    "cheap-qwen":         "$0.065/$0.26",
    "cheap-nex-pro":      "$0.075/$0.25",
}

SSL_CTX = ssl.create_default_context()
# 默认做完整证书校验。仅当显式设置 RELAY_INSECURE_TLS=1 才关闭（不建议用于生产）。
if os.environ.get("RELAY_INSECURE_TLS", "0") == "1":
    SSL_CTX.check_hostname = False
    SSL_CTX.verify_mode = ssl.CERT_NONE
    print("[warn] RELAY_INSECURE_TLS=1：已关闭上游证书校验", file=sys.stderr)

# ============ 状态 ============
_lock = threading.Lock()
_key_hits = defaultdict(deque)     # 每客户端 key 的分钟窗口
_key_daily = {}                    # 每客户端 key 的当日计数
_ip_hits = defaultdict(deque)      # ip -> deque[timestamp]
_auth_fails = defaultdict(list)    # ip -> [timestamp]
_banned = {}                       # ip -> ban_until
_daily = {"date": time.strftime("%Y-%m-%d"), "count": 0}



def local_ts():
    """返回按 TZ_OFFSET_HOURS 偏移后的时间戳"""
    return time.time() + TZ_OFFSET_HOURS * 3600


def cost_currency(upstream):
    """上游返回的 cost 用什么币种：or 是美元，其余（nl/qwen/zhipu/doubao）是人民币"""
    return "usd" if upstream == "or" else "cny"


def log_line(msg, upstream=None):
    msg = str(msg).replace("\r", " ").replace("\n", " ")   # 防日志行注入
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(local_ts()))} {msg}\n"
    targets = [LOG_PATH]
    if upstream == "or":
        targets.append(LOG_PATH_OR)
    elif upstream in ("qwen", "zhipu", "doubao"):
        targets.append(LOG_PATH_CN)
    for p in targets:
        try:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass



def in_open_window():
    """服务时段判断。起点==终点视为全天开放（默认）。支持跨零点。"""
    ov = (WINDOW_OPEN_HOUR * 60 + WINDOW_OPEN_MINUTE) % 1440
    cv = (WINDOW_CLOSE_HOUR * 60 + WINDOW_CLOSE_MINUTE) % 1440
    if ov == cv:
        return True
    now = time.localtime(local_ts())
    cur = now.tm_hour * 60 + now.tm_min
    return (ov <= cur < cv) if ov < cv else (cur >= ov or cur < cv)


TRUST_PROXY = os.environ.get("RELAY_TRUST_PROXY", "0") == "1"


def client_ip(handler):
    """取客户端 IP。只有在反向代理后面（RELAY_TRUST_PROXY=1）才采信 X-Forwarded-For，
    否则任何人都能伪造该头绕过限流。"""
    if TRUST_PROXY:
        xff = handler.headers.get("X-Forwarded-For")
        if xff:
            parts = [x.strip() for x in xff.split(",") if x.strip()]
            if parts:
                return parts[-1]      # 取最右一跳（最近代理写入，最左可被客户端伪造）
    return handler.client_address[0]


MAX_TRACKED_IPS = int(os.environ.get("RELAY_MAX_TRACKED_IPS", "10000"))


def check_rate(ip):
    now = time.time()
    with _lock:
        if len(_ip_hits) > MAX_TRACKED_IPS:       # 防止字典无界增长
            for _k in [_k for _k, _q in _ip_hits.items() if not _q or now - _q[-1] > 120]:
                _ip_hits.pop(_k, None)
        q = _ip_hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if RATE_LIMIT_PER_MIN and len(q) >= RATE_LIMIT_PER_MIN:
            return False
        q.append(now)
        return True


def is_banned(ip):
    now = time.time()
    with _lock:
        until = _banned.get(ip, 0)
        if until > now:
            return True, int(until - now)
        if until:
            _banned.pop(ip, None)
            _auth_fails.pop(ip, None)
        return False, 0


def record_auth_fail(ip):
    now = time.time()
    with _lock:
        lst = _auth_fails[ip]
        lst = [t for t in lst if now - t < 300]
        lst.append(now)
        _auth_fails[ip] = lst
        if AUTH_FAIL_LIMIT and len(lst) >= AUTH_FAIL_LIMIT:
            _banned[ip] = now + AUTH_BAN_SECONDS
            return True
    return False


def daily_ok():
    today = time.strftime("%Y-%m-%d", time.localtime(local_ts()))
    with _lock:
        if _daily["date"] != today:
            _daily["date"] = today
            _daily["count"] = 0
        if DAILY_REQUEST_LIMIT and _daily["count"] >= DAILY_REQUEST_LIMIT:
            return False, _daily["count"]
        _daily["count"] += 1
        return True, _daily["count"]


def resolve_proxy(u):
    """解析上游 proxy 配置，返回 (host, port, user, password, rdns) 或 None

    "proxy": false                               → 不走代理
    "proxy": true                                → 用全局 RELAY_SOCKS_HOST/PORT
    "proxy": "socks5://host:1080"                → 本地 DNS 解析
    "proxy": "socks5h://user:pass@host:1080"     → 代理端解析 DNS，支持认证
    """
    p = u.get("proxy")
    if p is None or p is False:
        return None
    if p is True:
        return (SOCKS_HOST, SOCKS_PORT, None, None, True)
    if isinstance(p, str):
        s = p.strip()
        if s.lower() in ("", "false", "0", "no", "off"):
            return None
        m = re.match(
            r"^(?P<scheme>socks5h?)://(?:(?P<user>[^:@/]*)(?::(?P<pwd>[^@/]*))?@)?"
            r"(?P<host>\[[^\]]+\]|[^:/]+)(?::(?P<port>\d+))?/?$", s, re.I)
        if not m:
            raise ValueError(f"无法解析的 proxy 地址：{p}（仅支持 socks5:// 或 socks5h://）")
        scheme = m.group("scheme").lower()
        host = m.group("host").strip("[]")
        port = int(m.group("port") or 1080)
        user = urllib.parse.unquote(m.group("user")) if m.group("user") else None
        pwd = urllib.parse.unquote(m.group("pwd")) if m.group("pwd") else None
        return (host, port, user, pwd, scheme == "socks5h")
    raise ValueError(f"proxy 字段类型不支持：{type(p).__name__}")


def open_upstream(upstream="or", timeout=120):
    """按上游建立连接：proxy=true 走 socks5，否则直连。timeout 可按用途单独收紧。"""
    u = UPSTREAMS.get(upstream) or UPSTREAMS[DEFAULT_UPSTREAM]
    host = u["host"]
    px = resolve_proxy(u)
    if px:
        if not SOCKS_AVAILABLE:
            raise RuntimeError("该上游启用了代理，但未安装 PySocks（pip install PySocks）")
        s = sockslib.socksocket()
        s.set_proxy(sockslib.SOCKS5, px[0], px[1], rdns=px[4],
                    username=px[2], password=px[3])
        try:
            s.settimeout(timeout)
            s.connect((host, 443))
            return SSL_CTX.wrap_socket(s, server_hostname=host)
        except Exception:
            s.close()
            raise
    # 直连
    s = socket.create_connection((host, 443), timeout=timeout)
    try:
        return SSL_CTX.wrap_socket(s, server_hostname=host)
    except Exception:
        s.close()
        raise



# ===== Prompt Caching 自动注入（仅 or 的 claude；nl 自动缓存不需要）=====
CACHE_INJECT_ENABLED = True     # 总开关
CACHE_MIN_CHARS = 2000          # 请求体低于此字符数不注入（约 1024 token 门槛）

def _has_cache_ctl(obj):
    try:
        return "cache_control" in json.dumps(obj, ensure_ascii=False)
    except Exception:
        return False

def inject_claude_cache(payload, upstream):
    """给 claude 请求注入 cache_control 断点：system 末尾 + 最后一条消息（最多 2-3 个，低于 Anthropic 上限 4）"""
    if not CACHE_INJECT_ENABLED or upstream != "or":
        return payload
    model = str(payload.get("model") or "")
    if "claude" not in model.lower():
        return payload
    if _has_cache_ctl(payload.get("messages") or []) or _has_cache_ctl(payload.get("system") or ""):
        return payload          # 客户端已自带断点，不重复加
    try:
        if len(json.dumps(payload, ensure_ascii=False)) < CACHE_MIN_CHARS:
            return payload
    except Exception:
        return payload
    cc = {"type": "ephemeral"}
    marked = 0
    sysv = payload.get("system")                       # Anthropic 原生格式
    if isinstance(sysv, str) and sysv:
        payload["system"] = [{"type": "text", "text": sysv, "cache_control": dict(cc)}]
        marked += 1
    elif isinstance(sysv, list) and sysv:
        for blk in reversed(sysv):
            if isinstance(blk, dict) and blk.get("type") == "text":
                blk["cache_control"] = dict(cc); marked += 1; break
    msgs = payload.get("messages")
    if isinstance(msgs, list) and msgs:
        if marked == 0 and isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
            c = msgs[0].get("content")                 # OpenAI 格式 system message
            if isinstance(c, str) and c:
                msgs[0]["content"] = [{"type": "text", "text": c, "cache_control": dict(cc)}]; marked += 1
            elif isinstance(c, list) and c:
                for blk in reversed(c):
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        blk["cache_control"] = dict(cc); marked += 1; break
        last = msgs[-1]                                # 最后一条消息打点：下一轮命中的关键
        if isinstance(last, dict):
            c = last.get("content")
            if isinstance(c, str) and c:
                last["content"] = [{"type": "text", "text": c, "cache_control": dict(cc)}]; marked += 1
            elif isinstance(c, list) and c:
                for blk in reversed(c):
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        blk["cache_control"] = dict(cc); marked += 1; break
    if marked:
        log_line(f"CACHE_INJECT up={upstream} model={model} breakpoints={marked}", upstream=upstream)
    return payload


_models_lock = threading.Lock()


def _read_capped(resp, limit=MAX_RESP_BYTES):
    """读取上游响应体，超过 limit 直接报错，避免整段进内存。"""
    if limit <= 0:
        return resp.read()
    buf = bytearray()
    while True:
        chunk = resp.read(65536)
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
        if len(buf) > limit:
            raise ValueError("upstream response exceeds %d bytes" % limit)


def strip_v1(path):
    """去掉客户端路径里的 /v1 前缀，因为上游 base 已含版本段"""
    if path.startswith("/v1/"):
        return path[3:]
    if path == "/v1":
        return "/"
    return path


def upstream_of(model, path="/chat/completions"):
    """模型名 → 上游 key"""
    if not model:
        return DEFAULT_UPSTREAM
    if model in MODEL_ROUTE:
        return MODEL_ROUTE[model]
    if "/" in model:
        pre = model.split("/", 1)[0]
        if pre in UPSTREAMS:
            return pre
    return DEFAULT_UPSTREAM


def real_model(upstream, model):
    """别名 → 上游真实模型名"""
    u = UPSTREAMS.get(upstream) or UPSTREAMS[DEFAULT_UPSTREAM]
    alias = u.get("aliases") or {}
    return alias.get(model, model)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = int(os.environ.get("RELAY_REQUEST_TIMEOUT", "30"))   # 防慢速连接占满线程

    def log_message(self, fmt, *args):
        pass  # 用自己的日志

    def _send_json(self, code, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        if code >= 400:
            self.close_connection = True   # 错误响应可能未读完请求体，关闭连接避免下个请求错位
        self.end_headers()
        self.wfile.write(body)

    def _auth_key(self):
        """返回匹配的 key 配置 dict，失败返回 None
        同时支持 Authorization: Bearer 和 Anthropic 的 x-api-key
        """
        h = self.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            tok = h[7:].strip()
        else:
            tok = (self.headers.get("x-api-key") or "").strip()
        if not tok:
            return None
        self._tok = tok
        cfg = None
        for _k, _v in CLIENT_KEYS.items():
            if secrets.compare_digest(_k, tok):
                cfg = _v
                break
        if not cfg:
            return None
        if not cfg.get("enabled", True):
            return None   # 已禁用 → 视为无效 key
        exp = cfg.get("expires_at")
        if exp:
            try:
                exp = float(exp)
            except (TypeError, ValueError):
                log_line(f"CONFIG_INVALID expires_at={exp!r}（应为时间戳）")
                return None   # 配置错误按无效处理，避免 500
            if time.time() > exp:
                return None   # 已过期 → 视为无效 key
        return cfg

    def _auth_ok(self):
        return self._auth_key() is not None

    def _key_quota_ok(self):
        """客户端 key 自身的 rpm / daily 限额（与全局、IP 限流叠加）

        注意：锁内只做计数与判定，响应在锁外发送，避免慢客户端阻塞全局锁。
        """
        cfg = self.kcfg or {}
        tok = getattr(self, "_tok", None)
        if not tok:
            return True
        try:
            rpm = int(cfg["rpm"]) if cfg.get("rpm") else 0
            daily = int(cfg["daily"]) if cfg.get("daily") else 0
        except (TypeError, ValueError):
            log_line(f"CONFIG_INVALID quota key={cfg.get('name')}（rpm/daily 必须为整数）")
            rpm = daily = 0
        if not rpm and not daily:
            return True

        now = time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(local_ts()))
        deny = None
        with _lock:
            if rpm:
                q = _key_hits[tok]
                while q and now - q[0] > 60:
                    q.popleft()
                if len(q) >= rpm:
                    deny = f"key rate limit exceeded ({rpm}/min)"
                else:
                    q.append(now)
            if not deny and daily:
                d = _key_daily.setdefault(tok, {"date": today, "count": 0})
                if d["date"] != today:
                    d["date"], d["count"] = today, 0
                if d["count"] >= daily:
                    deny = f"key daily limit exceeded ({daily}/day)"
                else:
                    d["count"] += 1

        if deny:
            self._send_json(429, {"error": {"message": deny, "code": 429}})
            return False
        return True

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self.close_connection = True
            return None
        if n < 0 or n > MAX_BODY_BYTES:      # 负数 / 超限一律拒绝
            self.close_connection = True
            return None
        if n == 0:
            return b""
        data = self.rfile.read(n)
        if len(data) != n:                    # 客户端中断或长度不符
            self.close_connection = True
            return None
        return data

    def _guard(self):
        """通用防护检查，返回 (ok, ip)"""
        ip = client_ip(self)

        # 时间窗口先判
        if not in_open_window():
            self._send_json(403, {"error": {
                "message": "relay closed (outside service window)",
                "code": 403}})
            log_line(f"WINDOW_CLOSED ip={ip}")
            return False, ip

        banned, wait = is_banned(ip)
        if banned:
            self._send_json(429, {"error": {"message": f"banned, retry in {wait}s", "code": 429}})
            log_line(f"BANNED ip={ip}")
            return False, ip
        if not check_rate(ip):
            self._send_json(429, {"error": {"message": f"rate limit exceeded ({RATE_LIMIT_PER_MIN}/min)", "code": 429}})
            log_line(f"RATE ip={ip}")
            return False, ip
        return True, ip

    def _fetch_models(self, upstream="or"):
        """从上游拉模型元数据。带缓存与短超时：上游不可达时快速失败，
        不阻塞 /v1/models（本地别名表照常返回）。"""
        now = time.time()
        with _models_lock:
            if (_MODELS_CACHE["upstream"] == upstream
                    and now - _MODELS_CACHE["at"] < MODELS_CACHE_TTL):
                return (_MODELS_CACHE["data"], _MODELS_CACHE.get("ok"),
                        _MODELS_CACHE.get("reason"))
        u = UPSTREAMS.get(upstream) or UPSTREAMS[DEFAULT_UPSTREAM]
        try:
            conn = http.client.HTTPSConnection(u["host"], timeout=MODELS_TIMEOUT)
            conn.sock = open_upstream(upstream, timeout=MODELS_TIMEOUT)
            conn.request("GET", u["base"] + "/models", headers={
                "Authorization": "Bearer " + u["key"],
                "Accept": "application/json",
            })
            resp = conn.getresponse()
            data = json.loads(_read_capped(resp, 8 << 20).decode(errors="ignore"))
            conn.close()
            out = data.get("data") or []
            with _models_lock:
                _MODELS_CACHE.update(at=now, upstream=upstream, data=out, ok=True, reason=None)
            return out, True, None
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            log_line(f"MODELS_FETCH_FAIL {reason} (降级为本地别名表)")
            _MODELS_CACHE.update(at=now, upstream=upstream, data=[],
                                 ok=False, reason=reason)
            return [], False, reason

    def _anthropic_event(self, evt_type, obj):
        """按 Anthropic SSE 格式写出一个事件"""
        data = json.dumps(obj, ensure_ascii=False)
        self.wfile.write(f"event: {evt_type}\ndata: {data}\n\n".encode())
        self.wfile.flush()

    def _proxy_anthropic(self, body, upstream, model, ip="-"):
        """Anthropic /v1/messages 透传（只给 OpenRouter 用）

        OpenRouter 原生支持 /api/v1/messages，不需要任何转换。
        部分上游不支持该协议，那条线继续走 OpenAI 协议（见 do_POST 分支）。
        """
        u = UPSTREAMS.get(upstream) or UPSTREAMS[DEFAULT_UPSTREAM]
        real = MODEL_ALIAS.get(model, model)   # claude-sonnet-5.5 → anthropic/claude-sonnet-5.5

        payload = dict(body)
        payload["model"] = real
        payload = inject_claude_cache(payload, upstream)
        want_stream = bool(body.get("stream"))

        conn = http.client.HTTPSConnection(u["host"], timeout=u.get("timeout", 180))
        try:
            conn.sock = open_upstream(upstream)
            headers = {
                "Authorization": "Bearer " + u["key"],
                "anthropic-version": body.get("anthropic_version") or "2023-06-01",
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if want_stream else "application/json",
                "Accept-Encoding": "identity",
            }
            if upstream == "or":
                headers["HTTP-Referer"] = "https://whale-relay.local"
                headers["X-Title"] = "whale-relay"

            body_bytes = json.dumps(payload, ensure_ascii=False).encode()
            log_line(f"ANTHRO_PASS ip={ip} up={upstream} model={real} stream={want_stream} bytes={len(body_bytes)}",
                     upstream=upstream)
            conn.request("POST", "/api/v1/messages", body=body_bytes, headers=headers)
            resp = conn.getresponse()

            if resp.status != 200:
                try:
                    errbody = _read_capped(resp, 1 << 20).decode(errors="ignore")[:600]
                except ValueError:
                    errbody = "(upstream error body too large)"
                log_line(f"ANTHRO_PASS_ERR ip={ip} status={resp.status}", upstream=upstream)
                self._send_json(resp.status, {"type": "error", "error": {
                    "type": "api_error", "message": errbody}})
                return

            if not want_stream:
                try:
                    data = _read_capped(resp)
                except ValueError as _e:
                    log_line(f"ANTHRO_TOO_LARGE ip={ip} {_e}", upstream=upstream)
                    self._send_json(502, {"type": "error", "error": {"type": "api_error", "message": "upstream response too large"}})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
                log_line(f"ANTHRO_PASS_OK ip={ip} model={real}", upstream=upstream)
                return

            # 流式：逐行透传，同时按 chunked 编码
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.close_connection = True

            n = 0
            while True:
                line = resp.readline()
                if not line:
                    break
                try:
                    self.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    log_line(f"CLIENT_ABORT ip={ip} (anthropic stream)")
                    return
                n += 1
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            log_line(f"ANTHRO_PASS_STREAM_END ip={ip} model={real} lines={n}", upstream=upstream)
        except Exception as e:
            log_line(f"ANTHRO_PASS_EXC ip={ip} {type(e).__name__}: {e}", upstream=upstream)
            try:
                self._send_json(502, {"type": "error", "error": {
                    "type": "api_error", "message": "upstream request failed"}})
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _proxy(self, method, path, body=None, stream=False, ip="-", upstream="or", real_model_name=None):
        u = UPSTREAMS.get(upstream) or UPSTREAMS[DEFAULT_UPSTREAM]
        conn = http.client.HTTPSConnection(u["host"], timeout=u.get("timeout", 180))
        try:
            conn.sock = open_upstream(upstream)
        except Exception as e:
            log_line(f"UPSTREAM_CONNECT_FAIL ip={ip} {type(e).__name__}: {e}", upstream=upstream)
            self._send_json(502, {"error": {"message": "upstream connect failed", "code": 502}})
            return
        # 免费额度模型可走独立密钥（free_key），付费模型走主密钥
        _use_key = u["key"]
        _m = ""
        try:
            if body:
                _b = json.loads(body.decode() if isinstance(body, bytes) else body)
                _m = str(_b.get("model") or "")
            elif real_model_name:
                _m = str(real_model_name)
        except Exception:
            _m = ""
        if u.get("free_key") and _m:
            _ml = _m.lower().strip()
            _fset = {str(x).lower() for x in (u.get("free_models") or [])}
            # 精确判定：仅 ":free" 后缀或显式白名单，避免 carefree 之类误判
            if _ml.endswith(":free") or _ml in _fset:
                _use_key = u["free_key"]
                log_line(f"KEY_PICK tier=free model={_m}", upstream=upstream)
            else:
                log_line(f"KEY_PICK tier=paid model={_m}", upstream=upstream)
        elif u.get("free_key"):
            log_line(f"KEY_PICK tier=paid(no-model) path={path}", upstream=upstream)
        headers = {
            "Authorization": "Bearer " + _use_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "Accept-Encoding": "identity",
        }
        # OpenRouter 特有的头，只对它发
        if upstream == "or":
            headers["HTTP-Referer"] = "https://whale-relay.local"
            headers["X-Title"] = "whale-relay"
        # 注意：部分上游免费额度用完后会自动转付费，需显式关闭回退
        # 这里强制关闭付费回退：额度用尽直接 429，绝不偷偷花钱
        if upstream in ("bz", "bzf"):
            headers["X-Free-Fallback"] = "false"
        # 国内上游不需要 /api/v1 前缀，path 由调用方拼好
        # 部分上游要求使用完整模型名
        if body and real_model_name:
            try:
                b = json.loads(body.decode() if isinstance(body, bytes) else body)
                b["model"] = real_model_name
                body = json.dumps(b).encode()
            except Exception:
                pass
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            # 上游若仍发生免费→付费降级，记录并透传给客户端
            _ff = None
            try:
                _ff = resp.getheader("x-bl-free-fallback")
            except Exception:
                pass
            if _ff:
                log_line(f"FREE_FALLBACK upstream={upstream} trigger={_ff} ip={ip} model={real_model_name or '?'}")
                try:
                    self._free_fb = _ff
                except Exception:
                    pass

            if stream and resp.status == 200:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")   # 无 CL 也无 chunked，必须靠关闭连接标记结束
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.close_connection = True
                while True:
                    line = resp.readline()   # 逐行读，SSE 一行一行来
                    if not line:
                        break
                    try:
                        self.wfile.write(line)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        log_line(f"CLIENT_ABORT ip={ip} {path} (stream)")
                        return

                    # 解析 usage（流式最后会带 cost）
                    if b'"usage"' in line:
                        try:
                            raw = line.decode(errors="ignore").strip()
                            if raw.startswith("data:"):
                                raw = raw[5:].strip()
                            if raw and raw != "[DONE]":
                                obj = json.loads(raw)
                                u = obj.get("usage") or {}
                                cost = u.get("cost")
                                model = obj.get("model", "?")
                                if cost is not None:
                                    log_line(f"OK ip={ip} key={self.kcfg.get('name')} up={upstream} "
                                             f"model={model} cost_{cost_currency(upstream)}={cost} "
                                             f"in={u.get('prompt_tokens')} out={u.get('completion_tokens')} "
                                             f"path={path} (stream)", upstream=upstream)
                        except Exception:
                            pass
                log_line(f"STREAM-END ip={ip} {path}")
                return

            try:
                data = _read_capped(resp)
            except ValueError as _e:
                log_line(f"UPSTREAM_TOO_LARGE ip={ip} {_e}", upstream=upstream)
                self._send_json(502, {"error": {"message": "upstream response too large", "code": 502}})
                return
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

            # 记录成本
            try:
                d = json.loads(data)
                cost = (d.get("usage") or {}).get("cost")
                model = d.get("model", "?")
                if cost is not None:
                    log_line(f"OK ip={ip} key={self.kcfg.get('name')} up={upstream} "
                             f"model={model} cost_{cost_currency(upstream)}={cost} path={path}",
                             upstream=upstream)
                else:
                    log_line(f"OK ip={ip} key={self.kcfg.get('name')} up={upstream} "
                             f"model={model} path={path}", upstream=upstream)
            except Exception:
                log_line(f"OK ip={ip} key={self.kcfg.get('name')} up={upstream} "
                         f"{path} status={resp.status}", upstream=upstream)
        except Exception as e:
            self._send_json(502, {"error": {"message": "upstream request failed", "code": 502}})
            log_line(f"ERR ip={ip} {type(e).__name__}: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_GET(self):
        if self.path == "/healthz":          # 健康检查：不需要 key，不计数
            self._send_json(200, {"ok": True})
            return
        # 其余端点都要 key（含 /health、/v1/models）
        ok, ip = self._guard()
        if not ok:
            return

        kcfg = self._auth_key()
        if not kcfg:
            if record_auth_fail(ip):
                log_line(f"AUTH_FAIL_BAN ip={ip} (GET)")
            self._send_json(401, {"error": {"message": "invalid api key", "code": 401}})
            return
        self.kcfg = kcfg
        if not self._key_quota_ok():
            return

        if self.path in ("/", "/health"):
            with _lock:
                today_cnt = _daily["count"]
            self._send_json(200, {
                "ok": True, "relay": "whale", "version": "v2", "upstreams": list(UPSTREAMS.keys()),
                "daily_used": today_cnt, "daily_limit": DAILY_REQUEST_LIMIT,
            })
            log_line(f"HEALTH ip={ip}")
            return

        # /v1/models —— 返回自己的别名表（Chatbox 可一键获取）
        if self.path.startswith("/v1/models"):
            if "full=1" in self.path:
                u0 = UPSTREAMS[DEFAULT_UPSTREAM]
                self._proxy("GET", u0["base"] + "/models", ip=ip, upstream=DEFAULT_UPSTREAM)
                return

            with _lock:
                pass
            import time as _t
            now = int(_t.time())
            upstream, _m_ok, _m_reason = self._fetch_models()
            meta_map = {m.get("id"): m for m in upstream if m.get("id")}
            data = []
            for alias, real in MODEL_ALIAS.items():
                base = dict(meta_map.get(real) or {})
                item = {
                    "id": alias,
                    "object": "model",
                    "created": base.get("created", now),
                    "owned_by": "whale-relay",
                    "name": alias,
                    "description": f"{real} · {PRICE_TAG.get(alias, '')}".strip(" ·"),
                }
                # 透传上游的关键元数据（Chatbox 靠这些判断能力）
                for k in ("context_length", "architecture", "top_provider",
                          "supported_parameters", "pricing", "per_request_limits"):
                    if k in base:
                        item[k] = base[k]
                # 显式声明能力（兼容不同客户端）
                item["capabilities"] = {
                    "tools": True,
                    "vision": "image" in str((base.get("architecture") or {}).get("input_modalities", "")),
                    "streaming": True,
                }
                item["supports_tools"] = True
                data.append(item)

            # ===== v2：追加国内模型 =====
            CN_ITEMS = [
                ("qwen/glm-5.3",          "GLM-5.3 旗舰（强制思考）"),
                ("qwen/glm-5.3-prime",    "GLM-5.3 Prime（最快）"),
                ("qwen/glm-5.3-flash",    "GLM-5.3 Flash"),
                ("qwen/glm-5.3-flashx",   "GLM-5.3 FlashX（加速）"),
                ("qwen/qwen3.8-max",      "千问旗舰"),
                ("qwen/qwen3.8-flash",    "千问便宜"),
                ("qwen/qwen3.6-flash",    "千问更省"),
                ("qwen/kimi-k3",          "Kimi K3"),
                ("qwen/minimax-m3",       "MiniMax M3"),
                ("qwen/mimo-v2.6-pro",    "小米 Mimo"),
                ("qwen/deepseek-v4-pro",     "DeepSeek v4 Pro"),
                ("qwen/deepseek-v4.1-flash", "DeepSeek v4.1 Flash"),
                ("zhipu/glm-4.7",         ""),
                ("zhipu/glm-4.5-air",     ""),
                ("nl/claude-opus-5.5",    "旗舰 Opus 5.5"),
                ("nl/claude-fable-5",     "旗舰 Fable 5"),
                ("nl/claude-sonnet-5.5",  "旗舰 Sonnet 5.5"),
                ("nl/claude-haiku-5.5",   "便宜 Haiku 5.5"),
                ("nl/claude-sonnet-5",    "中档 Sonnet 5"),
                ("nl/claude-haiku-4.5",   "便宜 Haiku 4.5"),
                ("nl/gemini-4-argon",     "Gemini 4 Argon"),
                ("nl/gemini-3.8-flash",   "Gemini 3.8 Flash"),
                ("nl/gemini-3.1-pro-preview", "Gemini 3.1 Pro"),
                ("nl/gemini-2.5-pro",     "Gemini 2.5 Pro"),
                ("nl/gemini-3-flash-preview", "Gemini 3 Flash"),
                ("nl/gpt-6.1-sol",        "GPT-6.1 Sol 旗舰"),
                ("nl/gpt-6-astra",        "GPT-6 Astra 旗舰"),
                ("nl/gpt-6-sol",          "GPT-6 Sol"),
                ("nl/gpt-6-luna",         "GPT-6 Luna 轻量"),
                ("nl/gpt-5.5-pro",        "GPT-5.5 Pro"),
                ("nl/gpt-5.4-mini",       "GPT-5.4 Mini 便宜"),
                ("doubao/seed-2.1-pro",   "Seed 2.1 Pro"),
                ("doubao/seed-2.1-turbo", "Seed 2.1 Turbo"),
                ("doubao/seed-2.1-lite",  "Seed 2.1 Lite"),
                # 上游 C · Grok
                ("nl/grok-4.7",                    "Grok 4.7 旗舰"),
                ("nl/grok-4.6",                    "Grok 4.6"),
                ("nl/grok-4.5",                    "Grok 4.5"),
                ("nl/grok-4-1-fast-reasoning",     "Grok 4.1 Fast（思考）"),
                ("nl/grok-4-1-fast-non-reasoning", "Grok 4.1 Fast（直答）"),
                ("nl/grok-4-fast-reasoning",       "Grok 4 Fast（思考）"),
                ("nl/grok-4-fast-non-reasoning",   "Grok 4 Fast（直答）"),
            ]
            for mid, desc in CN_ITEMS:
                _item = {
                    "id": mid,
                    "object": "model",
                    "created": now,
                    "owned_by": "whale-relay-cn",
                    "name": mid,
                    "description": desc,
                    "capabilities": {"tools": True, "vision": False, "streaming": True},
                    "supports_tools": True,
                }
                # Chatbox 判定"工作模式"实际读的是 supported_parameters 里有没有 tools
                # 该前缀下的模型均支持工具调用
                if mid.startswith("nl/"):
                    _item["supported_parameters"] = [
                        "tools", "tool_choice", "max_tokens",
                        "temperature", "top_p", "stop", "stream",
                    ]
                data.append(_item)

            # ===== v2.1（2026-10-30）：动态追加 keys.json 里所有 upstream 的别名 =====
            seen = {i["id"] for i in data}
            for _up_name, _u in UPSTREAMS.items():
                if _up_name == DEFAULT_UPSTREAM:
                    continue
                for _alias, _real in (_u.get("aliases") or {}).items():
                    if _alias in seen:
                        continue
                    seen.add(_alias)
                    _fi = ":free" in str(_real) or "free" in _alias.lower()
                    data.append({
                        "id": _alias, "object": "model", "created": now,
                        "owned_by": f"whale-relay-{_up_name}", "name": _alias,
                        "description": f"{_up_name} · {_real}" + ("（免费）" if _fi else ""),
                        "capabilities": {"tools": True, "vision": False, "streaming": True},
                        "supports_tools": True,
                    })

            # 按 key 的 allow 过滤（只能看到被允许上游的模型）
            _allow = (self.kcfg or {}).get("allow") or "*"
            if _allow != "*":
                def _up_of(mid):
                    return mid.split("/")[0] if "/" in mid else DEFAULT_UPSTREAM
                data = [m for m in data if _allow_ok(_allow, _up_of(m["id"]))]

            _src = "upstream" if _m_ok else ("local-fallback" if _m_ok is False else "uncertain")
            _hdr = {"X-Relay-Models-Source": _src}
            if _m_ok is False and _m_reason:
                _hdr["X-Relay-Fallback-Reason"] = str(_m_reason)[:180].replace("\n", " ")
            self._send_json(200, {
                "object": "list", "data": data,
                "_relay": {
                    "source": _src,
                    "upstream": DEFAULT_UPSTREAM,
                    "upstream_ok": _m_ok,
                    "fallback_reason": _m_reason,
                },
            }, extra=_hdr)
            log_line(f"MODELS ip={ip} n={len(data)} source={_src}"
                     + (f" reason={str(_m_reason)[:80]}" if _m_ok is False else ""))
            return

        self._send_json(404, {"error": {"message": "not found", "code": 404}})

    def do_POST(self):
        ok, ip = self._guard()
        if not ok:
            return

        _p = (self.path or "").split("?")[0].rstrip("/")
        if _p not in ALLOWED_POST_PATHS:
            self._send_json(404, {"error": {"message": "not found", "code": 404}})
            return

        # 认证（支持分级 key）
        kcfg = self._auth_key()
        if not kcfg:
            if record_auth_fail(ip):
                log_line(f"AUTH_FAIL_BAN ip={ip}")
            self._send_json(401, {"error": {"message": "invalid api key", "code": 401}})
            return
        self.kcfg = kcfg

        raw = self._read_body()
        if raw is None:
            self._send_json(413, {"error": {"message": "body too large (max 1MB)", "code": 413}})
            return
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            self._send_json(400, {"error": {"message": "invalid json", "code": 400}})
            return

        m = payload.get("model") or ""
        if not m:
            self._send_json(400, {"error": {"message": "missing required field: model", "code": 400}})
            return
        up = upstream_of(m)
        u = UPSTREAMS.get(up) or UPSTREAMS[DEFAULT_UPSTREAM]

        # 请求合法、上游确定后再扣配额：坏请求不该吃掉日额度
        if not self._key_quota_ok():
            return
        dok, cnt = daily_ok()
        if not dok:
            self._send_json(429, {"error": {"message": f"daily limit reached ({cnt}/{DAILY_REQUEST_LIMIT})", "code": 429}})
            log_line(f"DAILY_LIMIT ip={ip} count={cnt}")
            return

        # ===== Anthropic 原生协议分支（仅 nl/ 的 claude 模型）=====
        if is_anthropic_path(self.path) and is_claude_or(m):
            allow = self.kcfg.get("allow", "*")
            if not _allow_ok(allow, up):
                log_line(f"KEY_DENY ip={ip} key={self.kcfg.get('name')} up={up} model={m}", upstream=up)
                self._send_json(403, {"type": "error", "error": {
                    "type": "permission_error",
                    "message": f"this key ({self.kcfg.get('name')}) cannot access upstream '{up}'"}})
                return
            log_line(f"ANTHRO_REQ ip={ip} key={self.kcfg.get('name')} up={up} model={m}", upstream=up)
            self._proxy_anthropic(payload, up, m, ip=ip)
            return

        # ===== 以下为原有 OpenAI 协议路径（完全未改）=====
        if up == "or":
            # OpenRouter：优先 keys.json 的 or.aliases（可热改），其次兼容旧 MODEL_ALIAS
            _alias = (u.get("aliases") or {})
            if m in _alias:
                payload["model"] = _alias[m]
            elif m in MODEL_ALIAS:
                payload["model"] = MODEL_ALIAS[m]
            final_model = payload["model"]
            path = build_upstream_path(u["base"], strip_v1(self.path))
            real_for_upstream = None
        else:
            # 国内：用别名表映射成官方真实名
            real = real_model(up, m)
            payload["model"] = real
            final_model = real
            path = build_upstream_path(u["base"], strip_v1(self.path))
            real_for_upstream = None   # 已经写进 payload 了

        # ===== 分级 key：校验是否允许访问该上游 =====
        allow = self.kcfg.get("allow", "*")
        if not _allow_ok(allow, up):
            log_line(f"KEY_DENY ip={ip} key={self.kcfg.get('name')} up={up} model={m}", upstream=up)
            self._send_json(403, {"error": {
                "message": f"this key ({self.kcfg.get('name')}) cannot access upstream '{up}'",
                "code": 403}})
            return

        payload = inject_claude_cache(payload, up)
        log_line(f"REQ ip={ip} key={self.kcfg.get('name')} up={up} model={final_model}", upstream=up)

        stream = bool(payload.get("stream"))
        self._proxy("POST", path, json.dumps(payload).encode(),
                    stream=stream, ip=ip, upstream=up,
                    real_model_name=real_for_upstream)


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    _sem = threading.Semaphore(MAX_CONCURRENT) if MAX_CONCURRENT > 0 else None

    def process_request(self, request, client_address):
        # 并发数打满时立刻回 503，不排队、不建线程
        sem = type(self)._sem
        if sem is not None and not sem.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Type: application/json\r\n"
                                b"Content-Length: 2\r\n"
                                b"Connection: close\r\n\r\n{}")
            except Exception:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        sem = type(self)._sem
        try:
            super().process_request_thread(request, client_address)
        finally:
            if sem is not None:
                sem.release()



# ==================== 命令行管理（跨平台，仅标准库） ====================
# 用法：
#   python relay.py              启动服务
#   python relay.py init         初始化配置（填上游密钥 + 生成客户端密钥）
#   python relay.py check        校验配置
#   python relay.py key new [--name N] [--rpm N] [--daily N] [--allow or,qwen]
#   python relay.py key list
#   python relay.py key show <前缀>
#   python relay.py key del <前缀>

def _cli_console():
    """尽量让 Windows 控制台正确显示中文；失败则忽略。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _cli_usage():
    name = os.path.basename(sys.argv[0]) or "relay.py"
    print("""用法:
  python %s              启动服务
  python %s init         初始化配置（填上游密钥 + 生成客户端密钥）
  python %s check        校验配置
  python %s key new [--name N] [--rpm N] [--daily N] [--allow or,qwen]
  python %s key list     列出客户端密钥（打码）
  python %s key show <前缀>   显示完整密钥
  python %s key del <前缀>    吊销密钥
  python %s config list           查看全部配置（密钥打码）
  python %s config get <路径>     例: config get limits.ip_rpm
  python %s config set <路径> <值> 例: config set window.enabled true
  python %s config del <路径>     删除某个配置项""" % ((name,) * 11))


def _cfg_read(path):
    # utf-8-sig：兼容 Windows 记事本保存时带的 BOM
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _cfg_write(path, data):
    """原子写入配置。

    - 用唯一临时文件 + O_EXCL 创建，权限 0600，避免被其他用户读到密钥
    - 落盘前 flush + fsync，防止掉电得到空文件
    - os.replace 在 Windows 上可能因文件被占用失败，做短暂重试
    - 任何异常都会清理临时文件
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".whale-cfg-", suffix=".tmp")
    except OSError as e:
        raise SystemExit("[x] 无法在 %s 创建临时文件：%s" % (d, e))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass   # Windows 语义有限
        last = None
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                last = None
                break
            except PermissionError as e:
                last = e
                time.sleep(0.2)
        if last is not None:
            raise SystemExit("[x] 覆盖 %s 失败（可能被编辑器或杀毒软件占用），已保留原文件" % path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # POSIX 下对目录 fsync，确保 rename 落地；Windows 上会失败，忽略
    try:
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _cli_mask(k):
    if not isinstance(k, str):
        return str(k)
    return (k[:14] + "…" + k[-4:]) if len(k) > 20 else k


def _cli_ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ""          # 管道输入结束 = 跳过，不视为错误
    # KeyboardInterrupt 不吞：交给调用方决定是否中止且不写盘


def _cli_ask_secret(prompt):
    """读取密钥：TTY 下不回显；非 TTY 或 getpass 不可用时退回普通输入。"""
    if getpass is not None and sys.stdin.isatty():
        for _ in range(3):
            try:
                return getpass.getpass(prompt)
            except EOFError:
                return ""
            except Exception:
                break
    return _cli_ask(prompt)


def _cli_need_cfg():
    path = KEYS_PATH
    if not os.path.exists(path):
        print("[x] 还没有配置文件 %s" % path)
        print("    先运行：python %s init" % (os.path.basename(sys.argv[0]) or "relay.py"))
        return None
    return path


def _cli_init():
    path = KEYS_PATH
    if not os.path.exists(path):
        tpl = os.path.join(_HERE, "config.minimal.json")
        if not os.path.exists(tpl):
            print("[x] 缺少模板 config.minimal.json（应与 relay.py 放在同一目录）")
            return 1
        shutil.copyfile(tpl, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        print("[i] 已从模板生成 %s" % path)

    try:
        d = _cfg_read(path)
    except Exception as e:
        print("[x] 配置文件不是合法 JSON：%s" % e)
        return 1

    ups = d.get("upstreams") or {}
    if not isinstance(ups, dict) or not ups:
        print("[x] 配置里没有 upstreams 段")
        return 1

    print("")
    print("=== whale-relay 初始化 ===")
    print("")
    print("现在填写上游密钥（直接回车 = 该上游标记为停用，定义会保留）")
    print("")

    interactive = sys.stdin.isatty()
    kept = {}
    try:
        for name in list(ups.keys()):
            u = ups[name]
            if not isinstance(u, dict):
                print("  - %s 不是对象，保留原样" % name)
                kept[name] = u
                continue
            cur = str(u.get("key") or "")
            placeholder = (not cur) or cur.startswith("REPLACE_WITH") or cur.startswith("sk-CHANGE")
            disabled = bool(u.get("disabled"))
            if interactive:
                tip = "（尚未填写，回车=标记停用）" if placeholder else "（已填写，回车保留）"
                if disabled:
                    tip = "（当前已停用，直接回车保持停用）"
                val = _cli_ask_secret("  %s [%s] 密钥%s: " % (name, u.get("host", "?"), tip)).strip()
            else:
                val = (os.environ.get("UPSTREAM_KEY_" + name.upper(), "") or "").strip()

            if val:
                u["key"] = val
                u.pop("disabled", None)
            elif placeholder:
                # 关键：不删除上游定义，只标记停用，避免误删 host/base/proxy
                u["disabled"] = True
                print("  - %s 未填密钥，已标记停用（定义保留）" % name)
            kept[name] = u
    except KeyboardInterrupt:
        print("")
        print("[i] 已中断，配置未做任何改动")
        return 130

    active = [n for n, u in kept.items() if not (isinstance(u, dict) and u.get("disabled"))]
    if not active:
        print("")
        print("[x] 没有任何启用中的上游，配置未改动。")
        print("    可重新运行 init 填写密钥，或手动编辑 %s" % path)
        return 2

    d["upstreams"] = kept

    ks = d.get("client_keys") or {}
    need_key = (not ks) or all(("CHANGE" in str(k)) for k in ks)
    new_key = None
    if need_key:
        new_key = "sk-relay-" + secrets.token_hex(12)
        d["client_keys"] = {new_key: {"name": "local", "allow": "*", "rpm": 60, "daily": 1000}}

    _cfg_write(path, d)

    print("")
    print("[i] 已保存 %d 个上游：%s" % (len(kept), ", ".join(kept)))
    if new_key:
        print("[i] 已生成客户端密钥（稍后可运行 key show <前缀> 再次查看）：")
        print("")
        print("    %s" % new_key)
        print("")
    else:
        print("[i] 已有客户端密钥，未改动（用 key list 查看）")
    print("[i] 启动服务：python %s" % (os.path.basename(sys.argv[0]) or "relay.py"))
    return 0


def _cli_check():
    path = _cli_need_cfg()
    if not path:
        return 1
    try:
        d = _cfg_read(path)
    except Exception as e:
        print("[x] 配置不是合法 JSON：%s" % e)
        return 1

    errs, warns = [], []
    ks = d.get("client_keys") or {}
    ups = d.get("upstreams") or {}

    if not isinstance(ks, dict) or not ks:
        errs.append("client_keys 缺失或为空")
    if not isinstance(ups, dict) or not ups:
        errs.append("upstreams 缺失或为空")

    if isinstance(ks, dict):
        for k, v in ks.items():
            if not isinstance(k, str) or not k.startswith("sk-"):
                warns.append("客户端密钥 %s 不以 sk- 开头" % _cli_mask(k))
            if "CHANGE" in str(k):
                errs.append("客户端密钥仍是模板占位值 %s" % _cli_mask(k))
            if not isinstance(v, dict):
                errs.append("客户端密钥 %s 的值不是对象" % _cli_mask(k))
                continue
            for f in ("rpm", "daily"):
                if f in v:
                    val = v[f]
                    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                        errs.append("密钥 %s 的 %s 必须是非负整数" % (_cli_mask(k), f))
            if "expires_at" in v and not isinstance(v["expires_at"], (int, float)):
                warns.append("密钥 %s 的 expires_at 应为数字时间戳" % _cli_mask(k))

    if isinstance(ups, dict):
        for name, u in ups.items():
            if not isinstance(u, dict):
                errs.append("上游 %s 的值不是对象" % name)
                continue
            if u.get("disabled"):
                warns.append("上游 %s 已停用，启动时不会加载" % name)
                continue
            key = str(u.get("key") or "")
            if (not key) or key.startswith("REPLACE_WITH") or key.startswith("sk-CHANGE"):
                errs.append("上游 %s 的 key 未填写" % name)
            if not u.get("host"):
                errs.append("上游 %s 缺少 host" % name)
            if not u.get("base"):
                warns.append("上游 %s 缺少 base（建议填 /v1）" % name)
            px = u.get("proxy")
            if isinstance(px, str) and px.strip().lower() not in ("", "false", "0", "no", "off"):
                if not px.strip().lower().startswith(("socks5://", "socks5h://")):
                    errs.append("上游 %s 的 proxy 只支持 socks5:// 或 socks5h://" % name)

    for w in warns:
        print("[!] " + w)
    for e in errs:
        print("[x] " + e)
    if errs:
        print("")
        print("检查未通过：%d 个错误，%d 个警告" % (len(errs), len(warns)))
        return 1
    print("[✓] 配置检查通过（%d 个客户端密钥，%d 个上游）" % (len(ks), len(ups)))
    if warns:
        print("    有 %d 个警告，不阻断启动" % len(warns))
    return 0


def _cli_key_new(argv):
    path = _cli_need_cfg()
    if not path:
        return 1
    opts = {"name": "client", "rpm": "", "daily": "", "allow": "*"}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--name", "--rpm", "--daily", "--allow"):
            if i + 1 >= len(argv):
                print("[x] %s 需要一个值" % a)
                return 1
            opts[a[2:]] = argv[i + 1]
            i += 2
        elif a in ("-h", "--help"):
            print("用法: python relay.py key new [--name N] [--rpm N] [--daily N] [--allow or,qwen]")
            return 0
        else:
            print("[x] 未知参数：%s" % a)
            return 1

    rpm = daily = None
    try:
        if opts["rpm"]:
            rpm = int(opts["rpm"])
        if opts["daily"]:
            daily = int(opts["daily"])
    except (TypeError, ValueError):
        print("[x] rpm / daily 必须是整数")
        return 1
    if rpm is not None and rpm < 0:
        print("[x] rpm 不能为负")
        return 1
    if daily is not None and daily < 0:
        print("[x] daily 不能为负")
        return 1

    try:
        d = _cfg_read(path)
    except Exception as e:
        print("[x] 读配置失败：%s" % e)
        return 1
    if not isinstance(d, dict):
        print("[x] 配置文件顶层必须是对象")
        return 1
    if d.get("client_keys") is None:
        d["client_keys"] = {}
    if not isinstance(d.get("client_keys"), dict):
        print("[x] client_keys 必须是对象")
        return 1

    key = "sk-relay-" + secrets.token_hex(12)
    allow_raw = opts["allow"].strip()
    if allow_raw == "*":
        allow = "*"
    else:
        parts = [x.strip() for x in allow_raw.split(",") if x.strip()]
        if not parts:
            print("[x] --allow 不能为空；不限制请写 --allow '*'")
            return 1
        allow = parts
    entry = {"name": opts["name"], "allow": allow}
    if rpm is not None:
        entry["rpm"] = rpm
    if daily is not None:
        entry["daily"] = daily

    d.setdefault("client_keys", {})[key] = entry
    _cfg_write(path, d)
    print(key)
    print("[i] 新密钥需重启服务后生效", file=sys.stderr)
    return 0


def _cli_key_list():
    path = _cli_need_cfg()
    if not path:
        return 1
    try:
        d = _cfg_read(path)
    except Exception as e:
        print("[x] 读配置失败：%s" % e)
        return 1
    ks = d.get("client_keys") or {}
    if not ks:
        print("（没有任何客户端密钥）")
        return 0
    print("%-28s %-10s %-16s %5s %7s" % ("密钥", "名称", "允许上游", "rpm", "daily"))
    print("-" * 72)
    for k, v in ks.items():
        if not isinstance(v, dict):
            v = {}
        allow = v.get("allow", "*")
        if allow == "*" or allow is None:
            allow_s = "*"
        elif isinstance(allow, (list, tuple)):
            allow_s = ",".join([str(x) for x in allow])
        else:
            allow_s = str(allow)
        print("%-28s %-10s %-16s %5s %7s" % (
            _cli_mask(k), str(v.get("name", "-")), allow_s,
            str(v.get("rpm", "-")), str(v.get("daily", "-"))))
    return 0


def _cli_find_key(path, prefix):
    try:
        d = _cfg_read(path)
    except Exception as e:
        print("[x] 读配置失败：%s" % e)
        return None
    ks = d.get("client_keys") or {}
    hits = [k for k in ks if isinstance(k, str) and k.startswith(prefix)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        print("[x] 没有匹配的密钥")
        return None
    print("[x] 匹配到 %d 个，请给更长的前缀：" % len(hits))
    for h in hits[:10]:
        print("    " + _cli_mask(h))
    return None


def _cli_key_show(argv):
    path = _cli_need_cfg()
    if not path:
        return 1
    if not argv:
        print("用法: python relay.py key show <前缀>")
        return 1
    if len(argv[0]) < 8:
        print("[x] 前缀至少 8 位，避免误匹配")
        return 1
    full = _cli_find_key(path, argv[0])
    if not full:
        return 1
    print(full)
    return 0


def _cli_key_del(argv):
    path = _cli_need_cfg()
    if not path:
        return 1
    if not argv:
        print("用法: python relay.py key del <前缀>")
        return 1
    if len(argv[0]) < 8:
        print("[x] 前缀至少 8 位，避免误删")
        return 1
    full = _cli_find_key(path, argv[0])
    if not full:
        return 1
    ans = _cli_ask("确认吊销 %s ？[y/N] " % _cli_mask(full))
    if ans.strip().lower() not in ("y", "yes"):
        print("[i] 已取消")
        return 0
    try:
        d = _cfg_read(path)
    except Exception as e:
        print("[x] 读配置失败：%s" % e)
        return 1
    ks = d.get("client_keys") or {}
    if full not in ks:
        print("[x] 未找到该密钥")
        return 1
    ks.pop(full)
    d["client_keys"] = ks
    _cfg_write(path, d)
    print("[i] 已吊销，重启服务后生效")
    return 0


def _cfg_get_path(d, path):
    cur = d
    for k in path.split("."):
        cur = cur[int(k)] if isinstance(cur, list) else cur[k]
    return cur


def _cfg_parse_scalar(s):
    low = s.strip().lower()
    if low in ("true", "yes"): return True
    if low in ("false", "no"): return False
    if low in ("null", "none"): return None
    try: return int(s)
    except ValueError: pass
    try: return float(s)
    except ValueError: pass
    if s[:1] in ("{", "["):
        try: return json.loads(s)
        except Exception: pass
    return s


def _mask_secret(v):
    s = str(v)
    return (s[:8] + "…" + s[-4:]) if len(s) > 16 else ("***" if s else "")


# 常见字段的期望类型：写错类型会让服务在运行时崩，不如写入时直接拦住
_CFG_TYPES = {
    "key": str, "host": str, "base": str, "allow": (str, list), "name": str,
    "free_key": str, "mode": str, "open": str, "close": str,
    "proxy": (bool, str),
    "rpm": int, "daily": int, "timeout": int, "ip_rpm": int,
    "daily_total": int, "auth_fail_limit": int, "ban_seconds": int,
    "enabled": bool,
}


def _cfg_type_ok(last, val):
    exp = _CFG_TYPES.get(last)
    if exp is None:
        return True
    if exp is int:
        return isinstance(val, int) and not isinstance(val, bool)
    if exp is bool:
        return isinstance(val, bool)
    if isinstance(exp, tuple):
        return isinstance(val, exp)
    return isinstance(val, exp)


def _cfg_expect_name(last):
    exp = _CFG_TYPES.get(last)
    if exp is None: return "任意"
    if exp is int: return "整数"
    if exp is bool: return "true/false"
    if isinstance(exp, tuple): return "/".join(x.__name__ for x in exp)
    return exp.__name__


def _cli_config(args):
    """读改任意配置项，供 agent / 脚本调用。"""
    path = KEYS_PATH
    try:
        d = _cfg_read(path)
    except Exception as e:
        print("[x] 读配置失败：%s" % e)
        return 1
    if not args or args[0] in ("list", "ls", "show"):
        safe = json.loads(json.dumps(d))
        for k in list((safe.get("client_keys") or {}).keys()):
            safe["client_keys"][_mask_secret(k)] = safe["client_keys"].pop(k)
        for up in (safe.get("upstreams") or {}).values():
            if not isinstance(up, dict):
                continue
            for _f in ("key", "free_key"):
                if up.get(_f):
                    up[_f] = _mask_secret(up[_f])
            # 代理串里可能带账号密码：//user:pass@host
            _px = up.get("proxy")
            if isinstance(_px, str) and "@" in _px:
                up["proxy"] = re.sub(r"//[^/@]+@", "//***@", _px)
        print("配置文件：%s\n" % path)
        print(json.dumps(safe, ensure_ascii=False, indent=2))
        return 0
    sub = args[0]
    if sub == "get":
        if len(args) < 2:
            print("用法: python relay.py config get <路径>"); return 1
        try:
            v = _cfg_get_path(d, args[1])
        except Exception:
            print("[x] 路径不存在：%s" % args[1]); return 1
        print(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
        return 0
    if sub in ("set", "add"):
        if len(args) < 3:
            print("用法: python relay.py config set <路径> <值>"); return 1
        val = _cfg_parse_scalar(args[2])
        keys = args[1].split(".")
        if not _cfg_type_ok(keys[-1], val):
            print("[x] 字段 %s 期望 %s，收到 %r（%s）。已拒绝写入。"
                  % (keys[-1], _cfg_expect_name(keys[-1]), val, type(val).__name__))
            return 1
        cur = d
        for k in keys[:-1]:
            if isinstance(cur, dict):
                cur = cur.setdefault(k, {})
            else:
                cur = cur[int(k)]
        last = keys[-1]
        if isinstance(cur, dict):
            cur[last] = val
        else:
            cur[int(last)] = val
        _cfg_write(path, d)
        print("[✓] %s = %s（重启服务后生效）" % (args[1], json.dumps(val, ensure_ascii=False)))
        return 0
    if sub in ("del", "rm"):
        if len(args) < 2:
            print("用法: python relay.py config del <路径>"); return 1
        keys = args[1].split(".")
        cur = d
        for k in keys[:-1]:
            cur = cur[int(k)] if isinstance(cur, list) else cur[k]
        last = keys[-1]
        try:
            if isinstance(cur, dict): cur.pop(last, None)
            else: cur.pop(int(last))
            _cfg_write(path, d)
            print("[✓] 已删除 %s（重启服务后生效）" % args[1]); return 0
        except Exception as e:
            print("[x] 删除失败：%s" % e); return 1
    print("[x] 未知子命令：%s" % sub); return 1


def main_cli(argv):
    """返回进程退出码。"""
    _cli_console()
    if not argv:
        _cli_usage()
        return 0
    cmd = argv[0]
    rest = argv[1:]
    if cmd in ("-h", "--help", "help"):
        _cli_usage()
        return 0
    if cmd == "init":
        return _cli_init()
    if cmd in ("check", "verify"):
        return _cli_check()
    if cmd == "config":
        return _cli_config(rest)
    if cmd == "key":
        if not rest:
            print("用法: python relay.py key {new|list|show|del}")
            return 1
        sub, a = rest[0], rest[1:]
        if sub == "new":
            return _cli_key_new(a)
        if sub in ("list", "ls"):
            return _cli_key_list()
        if sub == "show":
            return _cli_key_show(a)
        if sub in ("del", "rm"):
            return _cli_key_del(a)
        print("[x] 未知子命令：%s" % sub)
        return 1
    print("[x] 未知命令：%s" % cmd)
    _cli_usage()
    return 1


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(main_cli(sys.argv[1:]))

    # 启动前校验代理配置，配置写错就明确报错退出，而不是运行中才发现
    for _un, _uc in UPSTREAMS.items():
        try:
            resolve_proxy(_uc)
        except ValueError as _e:
            raise SystemExit("[FATAL] 上游 %s: %s" % (_un, _e))

    srv = ThreadedServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print("[i] whale-relay 监听 %s:%s（%d 个上游）" % (LISTEN_HOST, LISTEN_PORT, len(UPSTREAMS)))
    log_line("START host=%s port=%s upstreams=%s" % (LISTEN_HOST, LISTEN_PORT, ",".join(UPSTREAMS)))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[i] 已停止")
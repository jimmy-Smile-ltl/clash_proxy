# -*- coding: utf-8 -*-
"""clash_subscriptions.py — 多订阅源拉取 / 校验 / 合并 / 定期刷新。

解决什么问题
------------
一个机场订阅节点不够用，或者想同时用两个机场时，手工合并 `proxies` 很痛苦：
还要处理重复节点、同名不同配置、订阅里的「剩余流量 / 套餐到期」垃圾条目，
以及订阅链接偶尔打不开。

本模块提供一条命令完成：

    读取来源列表 → 自动识别 URL / 文件 / 目录 → 拉取（失败用缓存或手动文件）
    → 解析 Clash YAML / JSON（兼容 base64 / URI 列表的有限格式）
    → 过滤无效与订阅元数据 → 跨源去重 / 同名改名 → 合并
    → 写入 config/config.yaml（并保留一份 merged-proxies.yaml）
    → 可选重载 Clash + 逐个节点测延迟

来源列表
--------
默认读取 `local/subscriptions.txt`（整个 `local/` 已被 .gitignore 忽略）：

    # 每行一个来源，支持 URL / 文件 / 目录
    https://example.com/api/v1/client/subscribe?token=...
    C:/Users/你/clash_private/another.yaml
    local/subscriptions/extra/          # 目录：扫描其中的 yaml/yml/json/txt

URL 拉取失败时依次回退：
1. `local/subscriptions/cache/<hash>.raw`（上次成功拉取的缓存）
2. `local/subscriptions/manual/<hash>.yaml`（你手动复制进去的内容）
3. 都没有 → 报错并告诉你手动文件的准确路径；默认中止合并（`--allow-partial` 可跳过）

隐私
----
真实订阅 URL / 节点凭据只存在于 `local/` 和 `config/config.yaml`，两者均被
.gitignore 忽略。日志里的 URL 会被打码（只显示 host）。

依赖
----
- requests（必选）
- PyYAML  （解析 Clash 配置，可选依赖 `pip install .[subscriptions]`）

用法
----
::

    python clash_cli.py sub list
    python clash_cli.py sub add https://example.com/sub?token=...
    python clash_cli.py sub sync --reload --test
    python clash_cli.py sub watch --interval 21600
    python clash_cli.py sub test

也可以单独调用：

    from clash_subscriptions import sync, load_sources
"""

import base64
import copy
import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import requests

try:
    import yaml
except ImportError:  # 订阅合并需要 PyYAML；核心库不依赖它
    yaml = None

logger = logging.getLogger("clash_subscriptions")

# 与主库保持一致的节点过滤规则（导入失败时用本地兜底）
try:
    from clash_manager import _INVALID_NODE_PATTERNS, _NON_NODE_TYPES
except Exception:  # pragma: no cover - 主库总是随仓库提供
    _INVALID_NODE_PATTERNS = (
        "最新网址", "剩余流量", "过期时间", "订阅", "到期",
        "官网", "网址", "发布", "://", "试用", "加群", "邀请", "防失联",
    )
    _NON_NODE_TYPES = {
        "Selector", "URLTest", "Fallback", "LoadBalance", "Relay",
        "Direct", "Reject", "RejectDrop", "Pass", "PassRule", "Compatible",
    }

# 订阅里 type 的大小写不统一（direct / Direct），比较时统一小写
_NON_NODE_LOWER = {t.lower() for t in _NON_NODE_TYPES}

# 订阅里常见的「非节点」条目。注意不要单用「流量」误杀真实节点名。
_SUBSCRIPTION_METADATA = tuple(_INVALID_NODE_PATTERNS) + (
    "剩余", "距离下次", "重置", "套餐", "流量剩余", "到期时间",
)

# 策略组里的特殊条目（不是节点，也不应该被过滤掉）
_SPECIAL_GROUP_ENTRIES = {
    "DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL",
}

_SUPPORTED_FILE_EXT = (".yaml", ".yml", ".json", ".txt")

# 服务器地址明显是本地/占位时跳过
_LOCAL_SERVERS = {"127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]"}


# ======================== 数据结构 ========================

@dataclass
class SourceSpec:
    """一个订阅来源：URL、单个文件或目录展开后的文件。"""
    raw: str
    kind: str                       # url | file | dir | unknown
    label: str                      # 日志用（URL 已打码）
    path: Optional[str] = None      # file 用
    url: Optional[str] = None       # url 用


@dataclass
class ParsedSource:
    """一个来源的拉取/解析结果。"""
    spec: SourceSpec
    ok: bool = False
    fmt: str = ""
    proxies: List[Dict[str, Any]] = field(default_factory=list)
    config: Optional[Dict[str, Any]] = None
    error: str = ""
    from_cache: bool = False
    cache_path: str = ""
    fetched_at: float = 0.0
    unsupported_uris: int = 0


# ======================== 基础工具 ========================

def mask_source(raw: str) -> str:
    """把订阅 URL 打码成 ``https://host/***``，避免日志泄露 token。"""
    raw = (raw or "").strip()
    if raw.lower().startswith(("http://", "https://")):
        parsed = urlparse(raw)
        host = parsed.hostname or "?"
        return "%s://%s/***" % (parsed.scheme, host)
    if os.path.isdir(raw):
        return "dir:%s/" % os.path.basename(os.path.normpath(raw))
    return "file:%s" % os.path.basename(raw)


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _abs_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _source_kind(value: str) -> str:
    """自动识别来源类型（URL / 文件 / 目录）。"""
    value = (value or "").strip()
    if value.lower().startswith(("http://", "https://")):
        return "url"
    if os.path.isdir(os.path.expanduser(value)):
        return "dir"
    if os.path.isfile(os.path.expanduser(value)):
        return "file"
    return "unknown"


def _dir_sources(path: str) -> List[SourceSpec]:
    """把目录展开成文件来源（不递归，跳过 manifest / 缓存 / 手动目录）。"""
    out: List[SourceSpec] = []
    try:
        names = sorted(os.listdir(path))
    except OSError as e:
        logger.warning("[⚠️] 读取目录失败 %s: %s", path, e)
        return out
    for name in names:
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        low = name.lower()
        if low in ("subscriptions.txt", "readme.md"):
            continue
        if not low.endswith(_SUPPORTED_FILE_EXT):
            continue
        out.append(SourceSpec(raw=full, kind="file", label=mask_source(full), path=full))
    logger.info("[🔍] 目录 %s 展开为 %s 个文件来源", path, len(out))
    return out


# ======================== 来源列表 ========================

def load_sources(manifest_path: str) -> List[SourceSpec]:
    """读取订阅来源列表；文件不存在时创建带注释的模板。"""
    manifest_path = _abs_path(manifest_path)
    if not os.path.exists(manifest_path):
        os.makedirs(os.path.dirname(manifest_path) or ".", exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write(
                "# 订阅来源列表（本文件在 local/ 下，已被 .gitignore 忽略）\n"
                "# 每行一个来源：http(s) URL / 文件路径 / 目录；# 开头为注释\n"
            )
        logger.warning("[⚠️] 未找到订阅来源列表，已创建模板: %s", manifest_path)
        return []

    specs: List[SourceSpec] = []
    with open(manifest_path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            kind = _source_kind(line)
            if kind == "url":
                specs.append(SourceSpec(raw=line, kind="url", label=mask_source(line), url=line))
            elif kind == "file":
                full = _abs_path(line)
                specs.append(SourceSpec(raw=line, kind="file", label=mask_source(full), path=full))
            elif kind == "dir":
                specs.extend(_dir_sources(_abs_path(line)))
            else:
                logger.warning("[⚠️] 第 %s 行来源无法识别（既不是 URL 也不是已存在文件）: %s",
                               lineno, line[:60])
                specs.append(SourceSpec(raw=line, kind="unknown", label=line[:60]))
    logger.info("[🔍] 订阅来源: %s 个（来自 %s）", len(specs), manifest_path)
    return specs


def add_source(manifest_path: str, value: str) -> None:
    """向来源列表追加一行（自动创建文件与父目录）。"""
    manifest_path = _abs_path(manifest_path)
    os.makedirs(os.path.dirname(manifest_path) or ".", exist_ok=True)
    with open(manifest_path, "a", encoding="utf-8") as f:
        f.write(value.strip() + "\n")
    logger.info("[✅] 已添加来源: %s", mask_source(value))


def remove_source(manifest_path: str, index: int) -> bool:
    """按「第 N 个来源」（1 起，忽略注释）删除一行。"""
    manifest_path = _abs_path(manifest_path)
    if not os.path.exists(manifest_path):
        return False
    with open(manifest_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    seen = 0
    target = None
    for i, line in enumerate(lines):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        seen += 1
        if seen == index:
            target = i
            break
    if target is None:
        logger.warning("[⚠️] 来源序号超出范围: %s（共 %s 个）", index, seen)
        return False
    removed = lines.pop(target)
    with open(manifest_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    logger.info("[✅] 已删除来源: %s", mask_source(removed.strip()))
    return True


# ======================== 拉取与缓存 ========================

def _looks_like_html(text: str) -> bool:
    head = (text or "")[:1200].lstrip().lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<html" in head[:200]


def _http_get(url: str, timeout: int, ua: str,
              proxy: Optional[str] = None) -> Tuple[str, str]:
    """拉取 URL，返回 (text, content_type)。异常直接抛出。"""
    session = requests.Session()
    session.trust_env = False                      # 不叠加系统代理环境变量
    try:
        resp = session.get(
            url,
            timeout=timeout,
            headers={"User-Agent": ua, "Accept": "*/*"},
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )
        if resp.status_code != 200:
            raise RuntimeError("HTTP %s" % resp.status_code)
        text = resp.text or ""
        if not text.strip():
            raise RuntimeError("响应为空")
        if _looks_like_html(text):
            raise RuntimeError("返回 HTML（订阅过期 / 需要登录 / 被重定向）")
        return text, resp.headers.get("content-type", "")
    finally:
        session.close()


def _cache_paths(cache_dir: str, source: str) -> Tuple[str, str, str]:
    key = _sha16(source)
    raw = os.path.join(cache_dir, key + ".raw")
    meta = os.path.join(cache_dir, key + ".meta.json")
    manual = os.path.join(os.path.dirname(cache_dir), "manual", key + ".yaml")
    return raw, meta, manual


def fetch_source(spec: SourceSpec, cache_dir: str, timeout: int = 30,
                 ua: str = "clash-verge/v2.0.0",
                 proxy: str = "") -> Tuple[Optional[str], bool, str, str]:
    """读取一个来源。

    :return: ``(text, from_cache, note, cache_path)``；失败时 text=None。
    """
    if spec.kind == "file":
        try:
            with open(spec.path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(), False, "", spec.path or ""
        except OSError as e:
            return None, False, "读取文件失败: %s" % e, ""

    if spec.kind != "url":
        return None, False, "来源无法识别（不存在或不是 URL）", ""

    os.makedirs(cache_dir, exist_ok=True)
    cache_raw, cache_meta, manual = _cache_paths(cache_dir, spec.url or spec.raw)

    attempts = [None]
    if proxy:
        attempts = [None, proxy]
    errors: List[str] = []
    for attempt in attempts:
        try:
            text, ctype = _http_get(spec.url or spec.raw, timeout, ua, attempt)
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_raw, "w", encoding="utf-8") as f:
                f.write(text)
            with open(cache_meta, "w", encoding="utf-8") as f:
                json.dump({
                    "source_hash": _sha16(spec.url or spec.raw),
                    "masked": spec.label,
                    "fetched_at": time.time(),
                    "content_type": ctype,
                    "size": len(text),
                    "via_proxy": bool(attempt),
                }, f, ensure_ascii=False, indent=2)
            return text, False, "", cache_raw
        except Exception as e:
            errors.append("%s%s" % ("直连" if attempt is None else "代理",
                                    ": %s" % e))

    # 失败回退：缓存 / 手动文件，取较新的那个
    candidates = []
    for path, label in ((cache_raw, "缓存"), (manual, "手动文件")):
        if os.path.isfile(path):
            try:
                candidates.append((os.path.getmtime(path), path, label))
            except OSError:
                pass
    if candidates:
        candidates.sort(reverse=True)
        mtime, path, label = candidates[0]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            age = max(0, time.time() - mtime)
            note = "%s（%s，%.1f 小时前）" % ("; ".join(errors), label, age / 3600)
            logger.warning("[⚠️] %s 拉取失败(%s)，使用%s: %s",
                           spec.label, "; ".join(errors), label, path)
            return text, True, note, path
        except OSError:
            pass

    # 都没有：告诉用户手动文件的准确路径
    os.makedirs(os.path.dirname(manual), exist_ok=True)
    msg = ("%s。没有可用缓存。请手动复制订阅内容保存到 %s，"
           "然后重新运行（无需修改来源列表）" % ("; ".join(errors), manual))
    return None, False, msg, ""


# ======================== 解析 ========================

def _base64_text(text: str) -> Optional[str]:
    """尝试把整体 base64 解码成文本。"""
    compact = "".join((text or "").split())
    if not compact or len(compact) < 16:
        return None
    pad = "=" * ((4 - len(compact) % 4) % 4)
    try:
        raw = base64.b64decode(compact + pad, validate=False)
        decoded = raw.decode("utf-8", "replace")
    except Exception:
        return None
    if not decoded:
        return None
    return decoded if ("://" in decoded or "proxies:" in decoded) else None


def _proxy_from_uri(uri: str) -> Optional[Dict[str, Any]]:
    """把 http/socks/ss/trojan URI 转成 Clash proxy 字典；不支持返回 None。"""
    uri = (uri or "").strip()
    if not uri or "://" not in uri:
        return None
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    try:
        port = int(parsed.port or 0)
    except ValueError:
        return None
    name = unquote(parsed.fragment or "") or "%s:%s" % (host or "?", port or "?")

    if scheme in ("http", "https", "socks5", "socks5h", "socks4"):
        if not host or not port:
            return None
        ptype = "http" if scheme in ("http", "https") else "socks5"
        p = {"name": name, "type": ptype, "server": host, "port": port}
        if parsed.username:
            p["username"] = unquote(parsed.username)
        if parsed.password:
            p["password"] = unquote(parsed.password)
        if scheme == "https":
            p["tls"] = True
        return p

    if scheme == "ss":
        body = uri[len("ss://"):].split("#", 1)[0]
        if "@" not in body:
            try:
                body = base64.b64decode(body + "=" * ((4 - len(body) % 4) % 4)).decode("utf-8")
            except Exception:
                return None
        if "@" not in body:
            return None
        cred, hostport = body.rsplit("@", 1)
        # 常见格式: ss://base64(method:password)@host:port
        if ":" not in cred:
            try:
                cred = base64.b64decode(cred + "=" * ((4 - len(cred) % 4) % 4)).decode("utf-8")
            except Exception:
                return None
        try:
            method, password = cred.split(":", 1)
            h, p = hostport.rsplit(":", 1)
            port = int(p)
        except ValueError:
            return None
        return {"name": name, "type": "ss", "server": h, "port": port,
                "cipher": method, "password": password, "udp": True}

    if scheme == "trojan":
        # trojan://password@host:port —— 冒号前是密码，不是 username
        password = parsed.username or parsed.password
        if not host or not port or not password:
            return None
        qs = parse_qs(parsed.query)
        p = {"name": name, "type": "trojan", "server": host, "port": port,
             "password": unquote(password), "udp": True}
        if qs.get("sni"):
            p["sni"] = qs["sni"][0]
        if qs.get("allowInsecure"):
            p["skip-cert-verify"] = qs["allowInsecure"][0] == "1"
        return p

    return None


def _parse_uri_lines(text: str) -> Tuple[List[Dict[str, Any]], int]:
    """解析纯 URI 列表；返回 (proxies, 不支持数量)。"""
    proxies: List[Dict[str, Any]] = []
    unsupported = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "://" not in line:
            # 兼容 host:port:user:pass / host:port
            try:
                from clash_sources import parse_proxy_line
                line = parse_proxy_line(line) or ""
            except Exception:
                line = ""
            if not line:
                unsupported += 1
                continue
        p = _proxy_from_uri(line)
        if p is None:
            unsupported += 1
        else:
            proxies.append(p)
    return proxies, unsupported


def _obj_to_proxies(obj: Any) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """从 YAML/JSON 对象里提取 proxies；返回 (proxies, 完整配置或 None)。"""
    if isinstance(obj, dict):
        proxies = obj.get("proxies")
        if isinstance(proxies, list):
            return [p for p in proxies if isinstance(p, dict)], obj
        return [], None
    if isinstance(obj, list):
        return [p for p in obj if isinstance(p, dict)], None
    return [], None


def parse_subscription(text: str) -> Tuple[str, List[Dict[str, Any]],
                                          Optional[Dict[str, Any]], int]:
    """解析订阅文本。

    :return: ``(format, proxies, full_config, unsupported_uri_count)``
    """
    if yaml is None:
        raise RuntimeError("缺少 PyYAML，无法解析订阅。请安装: pip install pyyaml")

    text = (text or "").lstrip("\ufeff").strip()
    if not text:
        raise ValueError("内容为空")
    if _looks_like_html(text):
        raise ValueError("返回 HTML（订阅过期 / 需要登录 / 被重定向）")

    # ① JSON
    if text.startswith(("{", "[")):
        try:
            obj = json.loads(text)
            proxies, cfg = _obj_to_proxies(obj)
            if proxies:
                return ("clash-json" if cfg else "json-list"), proxies, cfg, 0
        except Exception:
            pass

    # ② YAML（支持多文档）
    try:
        docs = list(yaml.safe_load_all(text))
        for doc in docs:
            proxies, cfg = _obj_to_proxies(doc)
            if proxies:
                return ("clash-yaml" if cfg else "yaml-list"), proxies, cfg, 0
    except Exception:
        pass

    # ③ base64 编码的订阅
    decoded = _base64_text(text)
    if decoded:
        if decoded.lstrip().startswith(("{", "[")) or "proxies:" in decoded:
            try:
                return parse_subscription(decoded)
            except Exception:
                pass
        proxies, unsupported = _parse_uri_lines(decoded)
        if proxies:
            return "base64-uri-list", proxies, None, unsupported

    # ④ 纯 URI 列表
    if "://" in text:
        proxies, unsupported = _parse_uri_lines(text)
        if proxies:
            return "uri-list", proxies, None, unsupported

    raise ValueError("无法识别的订阅格式（既不是 Clash YAML/JSON，也不是可解析的 URI 列表）")


# ======================== 校验 / 去重 ========================

def _valid_proxy(proxy: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """校验并规范化一个 proxy；返回 (clean_proxy, 错误原因)。"""
    if not isinstance(proxy, dict):
        return None, "不是字典"
    p = copy.deepcopy(proxy)
    name = str(p.get("name") or "").strip()
    ptype = str(p.get("type") or "").strip()
    server = str(p.get("server") or "").strip()

    if not name:
        name = "%s://%s:%s" % (ptype or "?", server or "?", p.get("port") or "?")
    if any(pat in name for pat in _SUBSCRIPTION_METADATA):
        return None, "订阅元数据条目"
    if not ptype:
        return None, "缺少 type"
    if ptype.lower() in _NON_NODE_LOWER:
        return None, "非节点类型 %s" % ptype
    if not server:
        return None, "缺少 server"
    if server.lower() in _LOCAL_SERVERS:
        return None, "本地地址 %s" % server
    try:
        port = int(p.get("port"))
    except (TypeError, ValueError):
        return None, "端口无效"
    if port < 1 or port > 65535:
        return None, "端口越界 %s" % port

    p["name"] = name
    p["type"] = ptype
    p["server"] = server
    p["port"] = port
    return p, ""


def fingerprint(proxy: Dict[str, Any]) -> str:
    """节点指纹：除 name 外全部字段（同名不同配置 = 不同节点）。"""
    data = {k: v for k, v in proxy.items() if k != "name"}
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def merge_proxies(parsed_sources: Sequence[ParsedSource]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """跨来源校验、去重、同名改名，返回 (merged, stats)。"""
    merged: List[Dict[str, Any]] = []
    seen_fp: Dict[str, str] = {}
    used_names: Dict[str, str] = {}
    stats_sources: List[Dict[str, Any]] = []

    for parsed in parsed_sources:
        stat = {
            "source": parsed.spec.label,
            "format": parsed.fmt,
            "raw": len(parsed.proxies),
            "valid": 0,
            "invalid": 0,
            "metadata": 0,
            "duplicate": 0,
            "renamed": 0,
            "unsupported_uris": parsed.unsupported_uris,
            "from_cache": parsed.from_cache,
        }
        for proxy in parsed.proxies:
            clean, why = _valid_proxy(proxy)
            if clean is None:
                if "元数据" in why:
                    stat["metadata"] += 1
                else:
                    stat["invalid"] += 1
                    logger.debug("[⏭️] 跳过节点(%s): %s", why, proxy.get("name"))
                continue

            fp = fingerprint(clean)
            if fp in seen_fp:
                stat["duplicate"] += 1
                continue

            name = clean["name"]
            if name in used_names:
                # 同名但配置不同 → 加序号，保证 Clash 配置合法
                n = 2
                new_name = "%s [%s]" % (name, n)
                while new_name in used_names:
                    n += 1
                    new_name = "%s [%s]" % (name, n)
                clean["name"] = new_name
                stat["renamed"] += 1
                name = new_name

            used_names[name] = fp
            seen_fp[fp] = name
            merged.append(clean)
            stat["valid"] += 1
        stats_sources.append(stat)

    type_counts: Dict[str, int] = {}
    for p in merged:
        type_counts[p["type"]] = type_counts.get(p["type"], 0) + 1

    return merged, {
        "sources": stats_sources,
        "total_unique": len(merged),
        "type_counts": type_counts,
        "duplicates": sum(s["duplicate"] for s in stats_sources),
        "metadata": sum(s["metadata"] for s in stats_sources),
        "invalid": sum(s["invalid"] for s in stats_sources),
        "renamed": sum(s["renamed"] for s in stats_sources),
    }


# ======================== 配置合并 ========================

def _load_yaml_file(path: str) -> Dict[str, Any]:
    if yaml is None:
        raise RuntimeError("缺少 PyYAML，无法读取模板。请安装: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("模板不是 YAML 字典: %s" % path)
    return data


def _rebuild_groups(cfg: Dict[str, Any], proxy_names: List[str],
                    group_name: str) -> None:
    """把策略组里的旧节点引用替换/清理，并确保管理组包含全部新节点。"""
    groups = cfg.get("proxy-groups")
    if not isinstance(groups, list):
        groups = []
        cfg["proxy-groups"] = groups

    group_names = {g.get("name") for g in groups if isinstance(g, dict)}
    managed = None

    for g in groups:
        if not isinstance(g, dict):
            continue
        # provider 驱动的组不要动（它自己会拉节点）
        if g.get("use") or g.get("include-all"):
            if g.get("name") == group_name:
                logger.warning("[⚠️] 管理组 %s 是 provider/include-all 组，已强制改为静态 select", group_name)
                g.pop("use", None)
                g.pop("include-all", None)
            else:
                continue

        entries = list(g.get("proxies") or [])
        kept = [x for x in entries
                if x in proxy_names or x in group_names or x in _SPECIAL_GROUP_ENTRIES]

        if g.get("name") == group_name:
            managed = g
            non_node = [x for x in kept if x not in proxy_names]
            g["proxies"] = _dedupe(proxy_names + non_node)
            if g.get("type") != "select":
                logger.warning("[⚠️] 管理组 %s 类型为 %s，已改为 select", group_name, g.get("type"))
                g["type"] = "select"
        elif not kept:
            # 组里引用的旧节点全没了：用合并后的节点填充，保证配置合法
            g["proxies"] = list(proxy_names)
            logger.warning("[⚠️] 策略组 %s 的旧节点已全部失效，改用全部合并节点", g.get("name"))
        else:
            g["proxies"] = kept

        # 去掉空组里的旧节点引用后可能只剩特殊项；保持不变
        if not g.get("proxies"):
            g["proxies"] = list(proxy_names)

    if managed is None:
        groups.insert(0, {"name": group_name, "type": "select",
                          "proxies": list(proxy_names)})
        logger.warning("[⚠️] 模板/基底里没有管理组 %s，已新建", group_name)
        rules = cfg.get("rules")
        if not isinstance(rules, list):
            rules = []
            cfg["rules"] = rules
        if not any(str(r).upper().startswith("MATCH,") for r in rules):
            rules.append("MATCH,%s" % group_name)
            logger.info("[✅] 已补 MATCH 规则指向管理组 %s", group_name)

    # 基底里的 proxy-providers 可能与合并节点重名；提醒用户
    if cfg.get("proxy-providers"):
        logger.warning("[⚠️] 基底配置包含 proxy-providers，可能引入额外节点；"
                       "如需纯合并节点请用 --base template")


def _dedupe(items: Sequence[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def build_config(merged: List[Dict[str, Any]],
                 parsed_sources: Sequence[ParsedSource],
                 template_path: str,
                 group_name: str = "🚀节点选择",
                 base: str = "auto",
                 mode: str = "base",
                 api_listen: str = "",
                 mixed_port: str = "",
                 secret: str = "") -> Tuple[Dict[str, Any], str]:
    """生成合并后的完整 Clash 配置。返回 (config, base_description)。"""
    proxy_names = [p["name"] for p in merged]
    base_cfg: Optional[Dict[str, Any]] = None
    base_desc = "模板 %s" % template_path
    base_idx: Optional[int] = None

    if base and base != "template":
        if str(base).isdigit():
            idx = int(base) - 1
            if 0 <= idx < len(parsed_sources) and parsed_sources[idx].config:
                base_cfg = parsed_sources[idx].config
                base_idx = idx
        elif base == "auto":
            for idx, parsed in enumerate(parsed_sources):
                cfg = parsed.config or {}
                if cfg.get("proxy-groups") and cfg.get("rules"):
                    base_cfg = cfg
                    base_idx = idx
                    break
        if base_cfg is not None:
            base_desc = "来源 #%s (%s)" % (base_idx + 1, parsed_sources[base_idx].spec.label)
            logger.info("[🔍] 使用基底配置: %s", base_desc)
    if base_cfg is None:
        base_cfg = _load_yaml_file(template_path)
        logger.info("[🔍] 使用模板配置: %s", template_path)

    cfg = copy.deepcopy(base_cfg)
    cfg["proxies"] = copy.deepcopy(merged)
    _rebuild_groups(cfg, proxy_names, group_name)

    if mode and mode != "base":
        if mode not in ("rule", "global", "direct"):
            raise ValueError("mode 只能是 base/rule/global/direct: %s" % mode)
        cfg["mode"] = mode

    if api_listen:
        cfg["external-controller"] = api_listen
    if mixed_port:
        try:
            cfg["mixed-port"] = int(mixed_port)
        except ValueError:
            logger.warning("[⚠️] mixed-port 不是整数: %s", mixed_port)
    if secret:
        cfg["secret"] = secret

    # 记住 GLOBAL 组选中的节点：重载 / 重启后不会退回 DIRECT
    profile = cfg.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    profile.setdefault("store-selected", True)
    profile.setdefault("store-fake-ip", True)
    cfg["profile"] = profile

    return cfg, base_desc


def _atomic_write(path: str, text: str) -> None:
    path = _abs_path(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_outputs(cfg: Dict[str, Any], merged: List[Dict[str, Any]],
                  out_path: str, proxies_path: str, meta_path: str,
                  summary: Dict[str, Any]) -> None:
    """写合并配置 + 纯节点列表 + 同步元数据；写前备份旧配置。"""
    if yaml is None:
        raise RuntimeError("缺少 PyYAML，无法写出 YAML")
    out_path = _abs_path(out_path)
    if os.path.exists(out_path):
        try:
            shutil.copy2(out_path, out_path + ".bak")
        except OSError as e:
            logger.warning("[⚠️] 备份旧配置失败: %s", e)

    _atomic_write(out_path, yaml.safe_dump(
        cfg, allow_unicode=True, sort_keys=False, default_flow_style=False))
    _atomic_write(_abs_path(proxies_path), yaml.safe_dump(
        {"proxies": merged}, allow_unicode=True, sort_keys=False, default_flow_style=False))
    summary["written_at"] = time.time()
    summary["config_path"] = out_path
    summary["proxies_path"] = _abs_path(proxies_path)
    _atomic_write(_abs_path(meta_path), json.dumps(summary, ensure_ascii=False, indent=2))
    logger.info("[✅] 已写入: %s", out_path)
    logger.info("[✅] 已写入纯节点列表: %s", _abs_path(proxies_path))


# ======================== 重载 / 测试 ========================

def reload_clash(api_url: str, secret: str, config_path: str,
                 timeout: int = 15) -> Tuple[bool, str]:
    """让 Clash/Mihomo 重新加载配置文件（PUT /configs）。

    ⚠️ 这会真实替换运行中实例的配置，可能改变端口/策略组。
    """
    session = requests.Session()
    session.trust_env = False
    headers = {"Authorization": "Bearer %s" % secret} if secret else {}
    url = api_url.rstrip("/") + "/configs"
    try:
        resp = session.put(url, json={"path": _abs_path(config_path)},
                           headers=headers, timeout=timeout)
        if resp.status_code in (200, 204):
            return True, "配置已重载"
        if resp.status_code == 401:
            return False, "HTTP 401：API secret 错误或未配置"
        # 部分版本要求 ?force=true
        resp2 = session.put(url + "?force=true",
                            json={"path": _abs_path(config_path)},
                            headers=headers, timeout=timeout)
        if resp2.status_code in (200, 204):
            return True, "配置已重载（force）"
        return False, "重载失败: HTTP %s / %s" % (resp.status_code, resp2.status_code)
    except Exception as e:
        return False, "重载异常: %s" % e
    finally:
        session.close()


def test_nodes(api_url: str, secret: str, group_name: str,
               test_url: str = "https://www.gstatic.com/generate_204",
               workers: int = 8, state_file: str = "local/state/subscription_test.json",
               timeout_ms: int = 10000) -> Dict[str, Any]:
    """通过 Clash API 测试当前分组里的所有节点（串行切换无关，只测延迟）。"""
    from clash_manager import ClashManager

    manager = ClashManager({
        "api_url": api_url,
        "secret": secret,
        "group_name": group_name,
        "proxy_port": 7897,
        "post_switch_test_url": test_url,
        "health_check_url": test_url,
        "health_check_timeout_ms": timeout_ms,
        "health_check_workers": workers,
        "enable_node_switching": False,
        "disable_policy": True,
        "show_node_info": False,
        "verify_exit_ip": False,
        "state_file": state_file,
    })
    if not manager.refresh_nodes():
        return {"ok": False, "error": "读取节点失败（API 不可达 / secret 错误 / 策略组不存在）",
                "total": 0, "healthy": 0, "nodes": []}
    manager.refresh_node_health(force=True)

    nodes = []
    for n in manager.available_nodes:
        nodes.append({"name": n.get("name"), "type": n.get("type"),
                      "delay_ms": n.get("delay") or 0,
                      "healthy": bool(n.get("healthy"))})
    healthy = [n for n in nodes if n["healthy"]]
    nodes.sort(key=lambda x: (not x["healthy"], x["delay_ms"] or 10 ** 9))
    return {
        "ok": True,
        "total": len(nodes),
        "healthy": len(healthy),
        "unhealthy": len(nodes) - len(healthy),
        "nodes": nodes,
        "fastest": nodes[:10],
    }


# ======================== 同步主流程 ========================

def _default_paths(cfg: Dict[str, Any]) -> Dict[str, str]:
    return {
        "manifest": cfg.get("subscription_file") or "local/subscriptions.txt",
        "cache_dir": cfg.get("subscription_cache_dir") or "local/subscriptions/cache",
        "out": cfg.get("subscription_output") or "config/config.yaml",
        "template": cfg.get("subscription_template") or "config/config.example.yaml",
        "proxies_out": cfg.get("subscription_proxies_output")
                        or "local/subscriptions/merged-proxies.yaml",
        "meta": cfg.get("subscription_meta_output")
                 or "local/subscriptions/last_sync.json",
        "group": cfg.get("subscription_group") or "🚀节点选择",
        "ua": cfg.get("subscription_ua") or "clash-verge/v2.0.0",
        "proxy": cfg.get("subscription_proxy") or "",
        "base": cfg.get("subscription_base") or "auto",
        "mode": cfg.get("subscription_mode") or "base",
    }


def sync(cfg: Dict[str, Any], allow_partial: bool = False,
         write: bool = True, reload: bool = False,
         test: bool = False, test_url: str = "",
         workers: int = 8, base: str = "", mode: str = "",
         group: str = "", out: str = "", template: str = "",
         api_listen: str = "", mixed_port: str = "",
         secret: str = "", timeout: int = 0) -> Dict[str, Any]:
    """执行一次完整同步：拉取 → 解析 → 合并 → 写配置 →（可选）重载/测试。"""
    paths = _default_paths(cfg)
    if base:
        paths["base"] = base
    if mode:
        paths["mode"] = mode
    if group:
        paths["group"] = group
    if out:
        paths["out"] = out
    if template:
        paths["template"] = template

    # CLI 参数 > 环境变量：让 sub watch 也能持续生成同一套端口/secret
    api_listen = api_listen or cfg.get("subscription_api_listen") or ""
    mixed_port = mixed_port or cfg.get("subscription_mixed_port") or ""
    secret = secret or cfg.get("subscription_secret") or ""

    timeout = timeout or int(cfg.get("subscription_timeout") or 30)
    specs = load_sources(paths["manifest"])
    if not specs:
        return {"ok": False, "error": "来源列表为空，请先执行 sub add <url-or-file>"}

    parsed_sources: List[ParsedSource] = []
    failures: List[str] = []
    for spec in specs:
        text, from_cache, note, cache_path = fetch_source(
            spec, paths["cache_dir"], timeout=timeout,
            ua=paths["ua"], proxy=paths["proxy"])
        if text is None:
            parsed_sources.append(ParsedSource(spec=spec, ok=False, error=note))
            failures.append("%s: %s" % (spec.label, note))
            continue
        try:
            fmt, proxies, full_cfg, unsupported = parse_subscription(text)
            parsed_sources.append(ParsedSource(
                spec=spec, ok=True, fmt=fmt, proxies=proxies, config=full_cfg,
                from_cache=from_cache, cache_path=cache_path,
                unsupported_uris=unsupported))
            logger.info("[✅] %s: %s, %s 个节点%s%s", spec.label, fmt, len(proxies),
                        ("（缓存）" if from_cache else ""),
                        ("，%s 个 URI 不支持" % unsupported if unsupported else ""))
        except Exception as e:
            parsed_sources.append(ParsedSource(spec=spec, ok=False, error=str(e)))
            failures.append("%s: %s" % (spec.label, e))

    if failures and not allow_partial:
        return {"ok": False, "error": "部分来源失败（默认中止，避免丢失节点）: "
                                       + " | ".join(failures),
                "failures": failures}

    good = [p for p in parsed_sources if p.ok]
    if not good:
        return {"ok": False, "error": "没有成功解析的来源: " + " | ".join(failures),
                "failures": failures}

    merged, stats = merge_proxies(good)
    if not merged:
        return {"ok": False, "error": "合并后没有有效节点", "stats": stats}

    config, base_desc = build_config(
        merged, good, template_path=paths["template"],
        group_name=paths["group"], base=paths["base"], mode=paths["mode"],
        api_listen=api_listen, mixed_port=mixed_port, secret=secret)
    summary = {
        "ok": True,
        "base": base_desc,
        "group": paths["group"],
        "sources": stats["sources"],
        "total_unique": stats["total_unique"],
        "type_counts": stats["type_counts"],
        "duplicates": stats["duplicates"],
        "metadata": stats["metadata"],
        "invalid": stats["invalid"],
        "renamed": stats["renamed"],
        "failures": failures,
        "config": paths["out"],
        "proxies": paths["proxies_out"],
    }

    if write:
        write_outputs(config, merged, paths["out"], paths["proxies_out"],
                      paths["meta"], summary)
    logger.info("[📊] 合并完成: %s 个唯一节点 %s；重复 %s，元数据 %s，无效 %s",
                stats["total_unique"], stats["type_counts"],
                stats["duplicates"], stats["metadata"], stats["invalid"])

    if reload:
        ok, msg = reload_clash(cfg.get("api_url") or "http://127.0.0.1:9097",
                               secret or cfg.get("secret") or "", paths["out"])
        summary["reload"] = {"ok": ok, "message": msg}
        if ok:
            logger.info("[✅] %s", msg)
        else:
            logger.warning("[⚠️] %s", msg)

    if test:
        if not test_url:
            test_url = (cfg.get("health_check_url")
                        or cfg.get("post_switch_test_url")
                        or "https://www.gstatic.com/generate_204")
        result = test_nodes(
            cfg.get("api_url") or "http://127.0.0.1:9097",
            secret or cfg.get("secret") or "", paths["group"],
            test_url=test_url, workers=workers)
        summary["test"] = result
        if result.get("ok"):
            logger.info("[📊] 节点测试: %s/%s 健康", result["healthy"], result["total"])
        else:
            logger.warning("[⚠️] 节点测试失败: %s", result.get("error"))

    # reload / test 结果是在 write_outputs 之后才产生的，补写一次元数据
    if write:
        _atomic_write(_abs_path(paths["meta"]),
                      json.dumps(summary, ensure_ascii=False, indent=2))

    return summary


# ======================== CLI 入口 ========================

def _print_sources(manifest_path: str) -> int:
    specs = load_sources(manifest_path)
    if not specs:
        print("（来源列表为空）")
        return 0
    print(f"{'#':>3}  {'类型':<7} 来源")
    print("-" * 72)
    for i, spec in enumerate(specs, 1):
        print(f"{i:>3}  {spec.kind:<7} {spec.label}")
    print(f"\n来源列表: {_abs_path(manifest_path)}")
    return 0


def _print_sync_summary(result: Dict[str, Any]) -> None:
    if not result.get("ok"):
        print("[❌] %s" % result.get("error"))
        for f in result.get("failures") or []:
            print("      - %s" % f)
        return
    print("[✅] 合并完成")
    print("  唯一节点: %s" % result["total_unique"])
    print("  类型分布: %s" % result["type_counts"])
    print("  重复/元数据/无效: %s / %s / %s" % (
        result["duplicates"], result["metadata"], result["invalid"]))
    print("  基底配置: %s" % result["base"])
    print("  管理组: %s" % result["group"])
    print("  配置输出: %s" % result["config"])
    print("  纯节点列表: %s" % result["proxies"])
    if result.get("failures"):
        print("  ⚠️ 失败来源（已用缓存/手动文件或跳过）:")
        for f in result["failures"]:
            print("      - %s" % f)
    if "reload" in result:
        print("  重载: %s" % result["reload"]["message"])
    if "test" in result:
        t = result["test"]
        if t.get("ok"):
            print("  节点测试: %s/%s 健康" % (t["healthy"], t["total"]))
        else:
            print("  节点测试失败: %s" % t.get("error"))


def _sync_from_args(args, cfg: Dict[str, Any], write: bool,
                    reload: bool, test: bool) -> Dict[str, Any]:
    """从 CLI 参数调用 sync，并把异常转成结构化错误。"""
    try:
        return sync(
            cfg,
            allow_partial=args.allow_partial,
            write=write,
            reload=reload,
            test=test,
            test_url=args.test_url,
            workers=args.workers,
            base=args.base,
            mode=args.mode,
            group=args.group,
            out=args.out,
            template=args.template,
            api_listen=args.api_listen,
            mixed_port=args.mixed_port,
            secret=args.secret,
        )
    except Exception as e:
        logger.error("[‼] 同步异常: %s", e)
        return {"ok": False, "error": "同步异常: %s" % e}


def cmd_sub(args, cfg: Dict[str, Any]) -> int:
    """`clash_cli.py sub ...` 的实现。"""
    paths = _default_paths(cfg)
    sub = getattr(args, "sub_command", None)

    if sub == "list":
        return _print_sources(paths["manifest"])

    if sub == "add":
        add_source(paths["manifest"], args.source)
        return 0

    if sub == "remove":
        return 0 if remove_source(paths["manifest"], args.index) else 1

    if sub == "sync":
        result = _sync_from_args(args, cfg, write=not args.dry_run,
                                 reload=args.reload, test=args.test)
        _print_sync_summary(result)
        return 0 if result.get("ok") else 1

    if sub == "test":
        try:
            result = test_nodes(
                cfg.get("api_url") or "http://127.0.0.1:9097",
                args.secret or cfg.get("subscription_secret")
                or cfg.get("secret") or "",
                args.group or paths["group"],
                test_url=args.test_url or cfg.get("health_check_url")
                         or cfg.get("post_switch_test_url")
                         or "https://www.gstatic.com/generate_204",
                workers=args.workers)
        except Exception as e:
            print("[❌] 节点测试异常: %s" % e)
            return 1
        if not result.get("ok"):
            print("[❌] %s" % result.get("error"))
            return 1
        print("[✅] 节点测试: %s/%s 健康（共 %s）" % (
            result["healthy"], result["total"], result["total"]))
        for n in result.get("nodes") or []:
            mark = "✅" if n["healthy"] else "❌"
            print("  %s %-45s %s" % (mark, n["name"][:45], n["delay_ms"] or "-"))
        return 0

    if sub == "watch":
        interval = args.interval or int(cfg.get("subscription_interval") or 21600)
        print("[🔁] 订阅定期刷新已启动，间隔 %s 秒（Ctrl+C 停止）" % interval)
        try:
            while True:
                result = _sync_from_args(args, cfg, write=True,
                                         reload=not args.no_reload,
                                         test=not args.no_test)
                _print_sync_summary(result)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n已停止")
            return 0

    print("用法: clash_cli.py sub {list|add|remove|sync|test|watch} ...")
    return 1

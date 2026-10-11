# -*- coding: utf-8 -*-
"""clash_sources.py — 可插拔的代理来源（ProxySource）

把「出口从哪来」和「怎么切换出口」解耦。`clash_manager.py` 原先把两者焊死了：
节点必须来自某个 Clash 实例的策略组，且唯一改变出口的手段是 `PUT /proxies/{group}`。
于是静态代理列表、订阅 URL、多个 Clash 实例统统接不进来。

定位（使用场景）
----------------
三种来源，覆盖绝大多数场景：

    ClashSelectorSource   某个 Clash/Mihomo 实例的策略组      （＝原来的行为）
    StaticListSource      静态代理列表 / 订阅 URL / 文件       （完全不经过 Clash）
    MultiClashSource      聚合 N 个 Clash 实例

依赖
----
- requests （必选，与主库一致）

用法示例
--------
::

    from clash_sources import StaticListSource, ClashSelectorSource, MultiClashSource
    from clash_manager import ClashManager

    # ① 直接用静态代理列表（不需要装 Clash）
    manager = ClashManager({
        "post_switch_test_url": "https://你的目标站/",
        "source": StaticListSource.from_file("proxies.txt"),
    })

    # ② 聚合两个 Clash 实例
    manager = ClashManager({
        "post_switch_test_url": "https://你的目标站/",
        "source": MultiClashSource([
            ClashSelectorSource("http://127.0.0.1:9097", "secret-a", "🚀节点选择"),
            ClashSelectorSource("http://127.0.0.1:9091", "secret-b", "GLOBAL"),
        ]),
    })

静态列表支持的写法（每行一条，`#` 开头为注释）::

    http://user:pass@host:1080
    socks5://user:pass@host:1080
    host:1080:user:pass          # 常见的「IP:端口:账号:密码」格式
    host:1080                    # 无认证

⚠️ 静态来源的出口**不走 Clash** —— 每个节点自带 `proxy` 地址，
`get_proxy_config()` 会直接返回它，requests/aiohttp 照常使用。
"""

import ipaddress
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger("clash_sources")

# Clash 支持的节点类型（与主库保持一致）
ALLOWED_NODE_TYPES = ["Shadowsocks", "Vmess", "Trojan", "Socks5", "Vless",
                      "Http", "Snell", "Wireguard", "Hysteria", "Hysteria2",
                      "Tuic", "AnyTLS", "Mieru", "Ssh"]

_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")


# ---------------------------------------------------------------- 节点描述

@dataclass
class NodeSpec:
    """一个出口的完整描述。

    主库原先的 `available_nodes` 条目只有 `name/type/delay` —— **没有任何连接
    信息**，所以除了「切 Clash 策略组」之外无路可走。`NodeSpec` 补上了这一层。
    """
    name: str
    kind: str = "clash"                      # clash | static
    proxy: Optional[str] = None              # 该节点自己的代理地址（static 用）
    api_url: Optional[str] = None            # 所属 Clash 实例（多实例聚合用）
    group: Optional[str] = None              # 所属策略组
    type: str = "Unknown"
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "type": self.type, "kind": self.kind,
                "proxy": self.proxy, "api_url": self.api_url, "group": self.group,
                "meta": dict(self.meta)}


# ---------------------------------------------------------------- 来源基类

class ProxySource:
    """代理来源接口。

    实现三个方法即可接入主库：
        - `list_nodes()`   返回节点列表
        - `activate(spec)` 把流量切到该节点
        - `probe_delay(spec, url, timeout_ms)` 测该节点的延迟（可选，返回 None 表示不支持）
    """

    kind = "base"

    def list_nodes(self) -> List[NodeSpec]:
        raise NotImplementedError

    def activate(self, node: NodeSpec) -> bool:
        raise NotImplementedError

    def probe_delay(self, node: NodeSpec, url: str, timeout_ms: int = 5000) -> Optional[int]:
        """默认不支持延迟测试（返回 None 由主库跳过预检）。"""
        return None

    def describe(self) -> str:
        return self.kind


# ---------------------------------------------------------------- Clash 来源

class ClashSelectorSource(ProxySource):
    """节点来自某个 Clash/Mihomo 实例的策略组（**与主库原先的行为一致**）。

    :param api_url: Clash 外部控制地址，如 `http://127.0.0.1:9097`
    :param secret: API 密钥
    :param group_name: 策略组名；为空或 404 时会自动发现第一个 Selector 组
    """

    kind = "clash"

    def __init__(self, api_url: str, secret: str = "", group_name: str = "",
                 proxy_port: Optional[int] = None):
        self.api_url = api_url.rstrip("/")
        self.secret = secret
        self.group_name = group_name
        self.proxy_port = proxy_port
        self._session: Optional[requests.Session] = None
        self._group_before: Optional[str] = None

    # ---- HTTP 基础设施（直连 API，不走代理）----

    def _s(self) -> requests.Session:
        if self._session is None:
            s = requests.Session()
            s.trust_env = False
            if self.secret:
                s.headers["Authorization"] = "Bearer %s" % self.secret
            self._session = s
        return self._session

    def _api(self, method: str, path: str, **kw) -> requests.Response:
        """访问 Clash API 时必须直连 —— 否则会「用代理访问代理自己的 API」导致 502。"""
        return self._s().request(
            method, "%s%s" % (self.api_url, path),
            proxies={"http": None, "https": None}, **kw)

    def available(self) -> bool:
        try:
            self._api("GET", "/version", timeout=4)
            return True
        except Exception:
            return False

    def local_proxy(self) -> Optional[str]:
        if not self.proxy_port:
            return None
        return "http://127.0.0.1:%s" % self.proxy_port

    # ---- 节点发现 ----

    def _find_selector_groups(self) -> List[str]:
        try:
            resp = self._api("GET", "/proxies", timeout=8)
            if resp.status_code == 200:
                return [n for n, info in (resp.json().get("proxies") or {}).items()
                        if isinstance(info, dict) and info.get("type") == "Selector"]
        except Exception as e:
            logger.warning("[⚠️] 查找策略组失败: %s", e)
        return []

    def list_nodes(self) -> List[NodeSpec]:
        """读取策略组节点。策略组不存在时自动发现并**记住实际组名**。"""
        if not self.group_name:
            groups = self._find_selector_groups()
            if not groups:
                logger.error("[❌] 未指定 group_name 且未发现任何 Selector 策略组")
                return []
            self.group_name = groups[0]
            logger.info("[✅] 自动选用策略组: %s", self.group_name)

        try:
            resp = self._api("GET", "/proxies/%s" % self.group_name, timeout=8)
            if resp.status_code == 404:
                groups = self._find_selector_groups()
                if not groups:
                    logger.error("[❌] 策略组 '%s' 不存在，且未发现替代组", self.group_name)
                    return []
                logger.warning("[⚠️] 策略组 '%s' 不存在，改用 '%s'", self.group_name, groups[0])
                self.group_name = groups[0]
                resp = self._api("GET", "/proxies/%s" % self.group_name, timeout=8)
            if resp.status_code != 200:
                logger.warning("[⚠️] 获取节点列表失败: HTTP %s", resp.status_code)
                if resp.status_code == 401:
                    logger.info("[💡] 提示: 检查 API 密钥 (secret) 是否正确")
                return []
        except Exception as e:
            logger.error("[‼] 连接 Clash API 失败: %s", e)
            return []

        data = resp.json() or {}
        all_proxies: Dict[str, Any] = {}
        try:
            pr = self._api("GET", "/proxies", timeout=8)
            if pr.status_code == 200:
                all_proxies = pr.json().get("proxies") or {}
        except Exception as e:
            logger.warning("[⚠️] 获取全部代理信息失败: %s", e)

        specs: List[NodeSpec] = []
        for raw in (data.get("all") or []):
            if isinstance(raw, dict):
                name, ntype = raw.get("name"), raw.get("type", "Unknown")
            elif isinstance(raw, str):
                name = raw
                ntype = (all_proxies.get(raw) or {}).get("type", "Unknown")
            else:
                continue
            if not name:
                continue
            # 嵌套策略组不是真实节点
            if (all_proxies.get(name) or {}).get("type") == "Selector":
                continue
            specs.append(NodeSpec(name=name, kind="clash", type=ntype,
                                  api_url=self.api_url, group=self.group_name,
                                  proxy=self.local_proxy(),
                                  meta={"raw": raw if isinstance(raw, dict) else {}}))
        logger.info("[🔍] 从策略组 '%s' 读取到 %s 个候选", self.group_name, len(specs))
        return specs

    # ---- 切换 ----

    def activate(self, node: NodeSpec) -> bool:
        """PUT 策略组。返回 204 只代表「收到指令」，真正的生效判定由主库负责。"""
        try:
            base = (node.api_url or self.api_url).rstrip("/")
            group = node.group or self.group_name
            resp = self._s().request(
                "PUT", "%s/proxies/%s" % (base, quote(group)),
                json={"name": node.name}, timeout=10,
                proxies={"http": None, "https": None})
            if resp.status_code == 204:
                return True
            if resp.status_code == 400:
                logger.info("[💡] 节点 '%s' 可能不在策略组 '%s' 里", node.name, group)
            elif resp.status_code == 404:
                logger.info("[💡] 策略组 '%s' 不存在", group)
            return False
        except Exception as e:
            logger.error("[‼] 切换节点异常: %s", e)
            return False

    def get_group_now(self) -> Optional[str]:
        try:
            resp = self._api("GET", "/proxies/%s" % self.group_name, timeout=5)
            if resp.status_code == 200:
                return (resp.json() or {}).get("now")
        except Exception:
            pass
        return None

    def probe_delay(self, node: NodeSpec, url: str, timeout_ms: int = 5000) -> Optional[int]:
        """用 Clash 自带的 delay 测试（不改变当前出口）。"""
        try:
            base = (node.api_url or self.api_url).rstrip("/")
            resp = self._s().request(
                "GET", "%s/proxies/%s/delay" % (base, quote(node.name)),
                params={"url": url, "timeout": timeout_ms}, timeout=timeout_ms / 1000 + 5,
                proxies={"http": None, "https": None})
            if resp.status_code == 200:
                return resp.json().get("delay")
        except Exception:
            pass
        return None

    def describe(self) -> str:
        return "Clash 策略组 %s @ %s" % (self.group_name or "(自动)", self.api_url)


# ---------------------------------------------------------------- 静态列表

def parse_proxy_line(line: str) -> Optional[str]:
    """把一行代理文本解析成标准代理 URL；无法解析返回 None。

    支持::

        http://user:pass@host:1080
        socks5://host:1080
        host:1080:user:pass
        host:1080
        user:pass@host:1080
    """
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None

    # ① 已经带 scheme
    if "://" in line:
        parsed = urlparse(line)
        if parsed.scheme.lower() in _PROXY_SCHEMES and parsed.hostname:
            return line
        return None

    # ② host:port:user:pass（无 scheme 的冒号分隔）
    parts = line.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return "http://%s:%s@%s:%s" % (quote(user), quote(pwd), host, port)

    # ③ user:pass@host:port
    if "@" in line:
        cred, _, hostport = line.rpartition("@")
        if ":" in hostport:
            return "http://%s@%s" % (cred, hostport)

    # ④ host:port
    if len(parts) == 2 and parts[0] and parts[1].isdigit():
        return "http://%s" % line

    return None


def _looks_like_ip_or_host(s: str) -> bool:
    if not s:
        return False
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return bool(re.match(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", s))


class StaticListSource(ProxySource):
    """静态代理列表 —— 完全不经过 Clash。

    出口就是代理本身，`NodeSpec.proxy` 直接是它的地址；主库的
    `get_proxy_config()` 会返回它，requests/aiohttp/curl_cffi 照常使用。

    :param lines: 代理行（列表或含换行的字符串）
    :param source: 也可以直接给文件路径或订阅 URL
    :param scheme: 无 scheme 行默认补的协议（http / socks5）
    """

    kind = "static"

    def __init__(self, lines: Optional[Any] = None, source: Optional[str] = None,
                 scheme: str = "http", timeout: float = 10.0):
        self.scheme = scheme
        self.timeout = timeout
        self._current: Optional[NodeSpec] = None
        self._raw = ""
        if source:
            self._raw = self._read_source(source)
        if lines:
            self._raw = "\n".join(lines) if isinstance(lines, (list, tuple)) else str(lines)

    # ---- 读取 ----

    @classmethod
    def from_file(cls, path: str, **kw) -> "StaticListSource":
        return cls(source=path, **kw)

    @classmethod
    def from_url(cls, url: str, **kw) -> "StaticListSource":
        return cls(source=url, **kw)

    def _read_source(self, source: str) -> str:
        if source.startswith(("http://", "https://")):
            try:
                s = requests.Session()
                s.trust_env = False
                resp = s.get(source, timeout=self.timeout)
                s.close()
                if resp.status_code == 200:
                    return resp.text
                logger.error("[❌] 拉取订阅失败 HTTP %s: %s", resp.status_code, source)
            except Exception as e:
                logger.error("[‼] 拉取订阅失败: %s", e)
            return ""
        try:
            with open(source, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            logger.error("[‼] 读取代理列表失败(%s): %s", source, e)
            return ""

    # ---- 接口 ----

    def list_nodes(self) -> List[NodeSpec]:
        specs: List[NodeSpec] = []
        seen = set()
        for raw in self._raw.splitlines():
            proxy = parse_proxy_line(raw)
            if not proxy or proxy in seen:
                continue
            parsed = urlparse(proxy)
            host = parsed.hostname or ""
            if not _looks_like_ip_or_host(host):
                logger.debug("[⏭️] 跳过无法识别的主机: %s", raw.strip()[:40])
                continue
            seen.add(proxy)
            label = "%s:%s" % (host, parsed.port or "")
            name = "%s|%s" % (parsed.scheme, label)
            specs.append(NodeSpec(name=name, kind="static", type="Static",
                                  proxy=proxy, meta={"raw_line": raw.strip()}))
        logger.info("[🔍] 静态列表解析出 %s 个出口", len(specs))
        return specs

    def activate(self, node: NodeSpec) -> bool:
        """静态来源不需要「切换」—— 出口由每个请求自带的 proxy 决定，这里只记游标。"""
        self._current = node
        return True

    def probe_delay(self, node: NodeSpec, url: str, timeout_ms: int = 5000) -> Optional[int]:
        """经该代理打一次目标站，返回耗时（ms）。"""
        try:
            s = requests.Session()
            s.trust_env = False
            t0 = time.time()
            resp = s.get(url, proxies={"http": node.proxy, "https": node.proxy},
                         timeout=max(1.0, timeout_ms / 1000.0),
                         headers={"Connection": "close"})
            s.close()
            if resp.status_code:
                return int((time.time() - t0) * 1000)
        except Exception:
            pass
        return None

    def describe(self) -> str:
        return "静态代理列表（%s 条）" % len(self.list_nodes())


# ---------------------------------------------------------------- 多实例聚合

class MultiClashSource(ProxySource):
    """聚合多个 Clash 实例的节点为一个池。

    每个 `NodeSpec` 带上自己的 `api_url`，`activate()` 会路由到对应实例 ——
    这是单实例模型做不到的（原先「切换」只会 PUT 一个固定地址）。
    """

    kind = "multi-clash"

    def __init__(self, sources: List[ClashSelectorSource]):
        if not sources:
            raise ValueError("MultiClashSource 需要至少一个 ClashSelectorSource")
        self.sources = list(sources)

    def list_nodes(self) -> List[NodeSpec]:
        specs: List[NodeSpec] = []
        for src in self.sources:
            try:
                nodes = src.list_nodes()
            except Exception as e:
                logger.warning("[⚠️] 来源 %s 读取失败: %s", src.describe(), e)
                continue
            # 名字冲突时加实例前缀，否则两边的同名节点会互相覆盖
            names = {n.name for n in nodes}
            for n in nodes:
                n.meta["instance"] = src.api_url
                if sum(1 for s in self.sources
                       if any(x.name == n.name for x in specs)) > 0:
                    n.name = "%s@%s" % (n.name, src.api_url.split("//")[-1])
                specs.append(n)
            logger.info("[🔗] %s → %s 个节点（本实例去重后 %s 个不同名）",
                        src.describe(), len(nodes), len(names))
        return specs

    def activate(self, node: NodeSpec) -> bool:
        for src in self.sources:
            if node.api_url and src.api_url.rstrip("/") == node.api_url.rstrip("/"):
                return src.activate(node)
        logger.error("[❌] 找不到节点 %s 所属的 Clash 实例 (%s)", node.name, node.api_url)
        return False

    def probe_delay(self, node: NodeSpec, url: str, timeout_ms: int = 5000) -> Optional[int]:
        for src in self.sources:
            if node.api_url and src.api_url.rstrip("/") == node.api_url.rstrip("/"):
                return src.probe_delay(node, url, timeout_ms)
        return None

    def describe(self) -> str:
        return "聚合 %s 个 Clash 实例" % len(self.sources)


# ---------------------------------------------------------------- 工厂

def build_source(cfg: Dict[str, Any]) -> Optional[ProxySource]:
    """从配置字典构造来源（供 ClashManager 使用）。

    支持的写法::

        {"source": <ProxySource 实例>}                 # 直接给对象
        {"source": {"type": "static", "file": "p.txt"}}
        {"source": {"type": "static", "url": "https://..."}}
        {"source": {"type": "static", "lines": [...]}}
        {"source": {"type": "clash", "api_url": ..., "secret": ..., "group_name": ...}}
        {"sources": [ {...}, {...} ]}                  # 多个 → 自动聚合
    """
    if not cfg:
        return None
    if isinstance(cfg, ProxySource):
        return cfg

    if cfg.get("sources"):
        subs = [build_source(s) for s in cfg["sources"]]
        subs = [s for s in subs if s is not None]
        clash_subs = [s for s in subs if isinstance(s, ClashSelectorSource)]
        if len(clash_subs) == len(subs) and len(subs) > 1:
            return MultiClashSource(clash_subs)
        if len(subs) == 1:
            return subs[0]
        logger.warning("[⚠️] sources 只能聚合多个 Clash 来源；静态列表请单独使用")
        return subs[0] if subs else None

    kind = (cfg.get("type") or "clash").lower()
    if kind == "static":
        if cfg.get("file"):
            return StaticListSource.from_file(cfg["file"], scheme=cfg.get("scheme", "http"))
        if cfg.get("url"):
            return StaticListSource.from_url(cfg["url"], scheme=cfg.get("scheme", "http"))
        return StaticListSource(lines=cfg.get("lines") or [], scheme=cfg.get("scheme", "http"))
    if kind == "clash":
        return ClashSelectorSource(
            api_url=cfg.get("api_url", "http://127.0.0.1:9097"),
            secret=cfg.get("secret", ""),
            group_name=cfg.get("group_name", ""),
            proxy_port=cfg.get("proxy_port"))
    logger.warning("[⚠️] 未知的来源类型 '%s'，忽略", kind)
    return None

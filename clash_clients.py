# -*- coding: utf-8 -*-
"""clash_clients.py — 客户端适配层（requests / aiohttp / curl_cffi / 浏览器）

把「代理池已就绪」和「你的 HTTP 客户端」接起来。原先这部分只能从文档里
复制粘贴，现在是一行调用。

每个适配器统一内置三件事（都是真实项目反复踩出来的坑）
------------------------------------------------------
1. **自动带代理** —— 启动时取一次 `manager.get_proxy_config()`；
2. **切换节点后自动重建连接** —— ⚠️ 这是最大的坑：Clash/mihomo 是**按连接**
   选出口的，切换只影响之后新建的连接。复用的 keep-alive 连接会继续走旧节点，
   表现为「切了节点但出口 IP 没变」。适配器会检测到节点变化并丢弃连接池。
3. **自动上报判定** —— 按 `verdict_map` 把响应映射成 ok/blocked/dead 调
   `report_verdict()`，让轮换与冷却自动生效。

依赖
----
- requests （必选）
- aiohttp / curl_cffi / playwright （用到哪个装哪个，懒加载）

用法示例
--------
::

    from clash_clients import session, aiohttp_session, curl_cffi_session, browser_context

    # requests
    with session(manager) as s:
        r = s.get("https://目标站/api")

    # aiohttp（异步）
    async with aiohttp_session(manager) as s:
        async with s.get("https://目标站/api") as r: ...

    # curl_cffi（TLS 指纹伪装）
    with curl_cffi_session(manager, impersonate="chrome") as s:
        r = s.get("https://目标站/api")

    # Playwright（真浏览器）
    async with browser_context(manager) as ctx:
        page = await ctx.new_page()
        await page.goto("https://目标站/")

⚠️ 异步提醒
-----------
`ClashManager` 是**同步**的，且 `switch_node()` 单次可能阻塞 30-60 秒
（要做预检 + PUT + 三重验证）。所以异步适配器内部一律用 `asyncio.to_thread()`
把切换丢到线程池，**绝不能直接 await**，否则会卡死整个事件循环。
"""

import asyncio
import functools
import logging
import threading
from typing import Any, Dict, Optional, Sequence, Tuple

import requests

logger = logging.getLogger("clash_clients")


async def _to_thread(func, *args, **kw):
    """`asyncio.to_thread` 的 3.8 兼容版（后者是 3.9 才有的）。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kw))

# 默认判定: 2xx/3xx = ok；403/429/451 = blocked；异常 = dead；其余状态码不上报。
# ⚠️ 这只是「常见站点」的保守默认。判据因站而异（同一个 403 对 A 站是权限不足、
# 对 B 站才是封禁），务必按目标站用 verdict_map 覆盖。
DEFAULT_BLOCKED_STATUS: Tuple[int, ...] = (403, 429, 451)


def classify_status(status: int, blocked_status: Sequence[int] = DEFAULT_BLOCKED_STATUS,
                    ok_status: Optional[Sequence[int]] = None) -> Optional[str]:
    """把 HTTP 状态码映射成 verdict；不该上报时返回 None。"""
    if status in blocked_status:
        return "blocked"
    if ok_status is not None:
        return "ok" if status in ok_status else None
    return "ok" if 200 <= status < 400 else None


class _ProxyFollower:
    """「跟随代理池」的公共逻辑：检测节点变化 → 通知子类重建连接。

    抽出来是因为三种客户端要做的事完全一样，只是重建连接的方法名不同。
    """

    def __init__(self, manager, auto_report: bool = True,
                 blocked_status: Sequence[int] = DEFAULT_BLOCKED_STATUS,
                 ok_status: Optional[Sequence[int]] = None):
        self.manager = manager
        self.auto_report = auto_report
        self.blocked_status = tuple(blocked_status)
        self.ok_status = tuple(ok_status) if ok_status else None
        self._node: Optional[str] = None
        self._lock = threading.Lock()
        self.proxies: Dict[str, str] = dict(manager.get_proxy_config())
        self._node = self._current_node_name()

    def _current_node_name(self) -> Optional[str]:
        try:
            node = self.manager.get_current_node()
            return (node or {}).get("name")
        except Exception:
            return None

    def sync(self) -> bool:
        """节点变了就刷新代理地址并重建连接。返回是否发生了变化。"""
        with self._lock:
            name = self._current_node_name()
            if name == self._node:
                return False
            self._node = name
            try:
                self.proxies = dict(self.manager.get_proxy_config())
            except Exception as e:
                logger.warning("[⚠️] 刷新代理地址失败: %s", e)
                return False
            self._on_node_changed()
            logger.info("[🔁] 出口已切换 → %s，连接池已重建", name)
            return True

    def _on_node_changed(self) -> None:
        """子类实现：丢弃旧的 keep-alive 连接。"""

    def report(self, status: Optional[int], exc: Optional[BaseException] = None) -> None:
        """按判定上报给 ClashManager。"""
        if not self.auto_report:
            return
        try:
            if exc is not None:
                self.manager.report_verdict("dead")
                return
            verdict = classify_status(status, self.blocked_status, self.ok_status)
            if verdict:
                self.manager.report_verdict(verdict)
        except Exception as e:
            logger.debug("[⚠️] 上报判定失败: %s", e)


# ---------------------------------------------------------------- requests

def session(manager, auto_report: bool = True,
            blocked_status: Sequence[int] = DEFAULT_BLOCKED_STATUS,
            ok_status: Optional[Sequence[int]] = None, **session_kw) -> "requests.Session":
    """返回一个「跟着代理池走」的 `requests.Session`。

    每次请求前检查节点是否变化；变了就刷新代理并丢弃连接池（keep-alive 会复用
    旧隧道，不丢弃的话新节点不生效）。
    """
    class _ManagedSession(requests.Session, _ProxyFollower):
        def __init__(self):
            requests.Session.__init__(self)
            _ProxyFollower.__init__(self, manager, auto_report, blocked_status, ok_status)
            extra = dict(session_kw)
            self.headers.update(extra.pop("headers", {}))
            for k, v in extra.items():
                setattr(self, k, v)

        def _on_node_changed(self) -> None:
            # 关闭底层连接池 —— 这是「切换节点后仍走旧出口」的根治手段
            try:
                self.close()
            except Exception:
                pass

        def request(self, method: str, url: str, **kw):
            self.sync()
            kw.setdefault("proxies", self.proxies)
            try:
                resp = super().request(method, url, **kw)
            except Exception as e:
                self.report(None, exc=e)
                raise
            self.report(resp.status_code)
            return resp

    s = _ManagedSession()
    s.trust_env = False                 # 别让系统代理环境变量叠加进来
    return s


# ---------------------------------------------------------------- aiohttp

def aiohttp_session(manager, auto_report: bool = True,
                    blocked_status: Sequence[int] = DEFAULT_BLOCKED_STATUS,
                    ok_status: Optional[Sequence[int]] = None, **kw):
    """返回一个可 `async with` 的 aiohttp 会话包装。

    ⚠️ 内部用 `asyncio.to_thread()` 调用同步的 `ClashManager`，
    避免阻塞事件循环。切换相关的阻塞调用都在线程池里。
    """
    import aiohttp

    class _ManagedAiohttp:
        def __init__(self, **kw):
            self._kw = kw
            self._session = None
            self._follower = _ProxyFollower(manager, auto_report,
                                            blocked_status, ok_status)

        async def __aenter__(self):
            self._session = aiohttp.ClientSession(**self._kw)
            return self

        async def __aexit__(self, *exc):
            await self._session.close()
            return False

        @property
        def session(self):
            return self._session

        async def request(self, method: str, url: str, **kw):
            # 同步的 manager 操作丢到线程里，别卡事件循环
            await _to_thread(self._follower.sync)
            kw.setdefault("proxy", self._follower.proxies.get("https"))
            try:
                resp = await self._session.request(method, url, **kw)
            except Exception as e:
                self._follower.report(None, exc=e)
                raise
            self._follower.report(resp.status)
            return resp

        def get(self, url, **kw):
            return self.request("GET", url, **kw)

        def post(self, url, **kw):
            return self.request("POST", url, **kw)

        async def switch_node_async(self, **kw):
            """异步地请求切换节点（不会卡事件循环）。"""
            return await _to_thread(manager.switch_node, **kw)

    return _ManagedAiohttp(**kw)


# ---------------------------------------------------------------- curl_cffi

def curl_cffi_session(manager, impersonate: str = "chrome",
                      auto_report: bool = True,
                      blocked_status: Sequence[int] = DEFAULT_BLOCKED_STATUS,
                      ok_status: Optional[Sequence[int]] = None, **kw):
    """返回一个「跟着代理池走」的 curl_cffi Session（TLS 指纹伪装）。

    目标站做 TLS 指纹检测（requests/curl 被拒、浏览器能开）时用它。
    """
    from curl_cffi import requests as cffi_requests

    class _ManagedCffiSession(_ProxyFollower):
        def __init__(self):
            _ProxyFollower.__init__(self, manager, auto_report, blocked_status, ok_status)
            self._s = cffi_requests.Session(impersonate=impersonate, **kw)

        def _on_node_changed(self) -> None:
            try:
                self._s.close()
            except Exception:
                pass

        def request(self, method: str, url: str, **kw):
            self.sync()
            kw.setdefault("proxies", self.proxies)
            try:
                resp = self._s.request(method, url, **kw)
            except Exception as e:
                self.report(None, exc=e)
                raise
            self.report(resp.status_code)
            return resp

        def get(self, url, **kw):
            return self.request("GET", url, **kw)

        def post(self, url, **kw):
            return self.request("POST", url, **kw)

        def close(self):
            try:
                self._s.close()
            except Exception:
                pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    return _ManagedCffiSession()


# ---------------------------------------------------------------- 浏览器

def browser_context(manager, channel: str = "chrome", headless: bool = False, **kw):
    """返回一个可 `async with` 的 Playwright 浏览器上下文（代理已配好）。

    :param channel: `"chrome"` / `"msedge"` 驱动本机真实浏览器。
        ⚠️ 别用 Playwright 内置 Chromium —— 实测同一 IP 同一 UA 下，
        内置 Chromium 被目标站识别，真实 Chrome / Edge 正常。
    :param headless: 默认 **False**。实测同 IP 同 UA：headless 被风控拦，
        headed 正常。服务器上必须无头时请先评估目标站是否检测。

    用法::

        async with browser_context(manager) as ctx:
            page = await ctx.new_page()
            await page.goto(TARGET)
            ...
            await ctx.reload_page(page)      # 切节点后重载，制造新连接
    """
    class _BrowserCtx:
        def __init__(self):
            self._pw = None
            self._browser = None
            self._ctx = None
            self._follower = _ProxyFollower(manager, auto_report=False)

        async def __aenter__(self):
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            launch_kw: Dict[str, Any] = dict(
                headless=headless,
                proxy={"server": self._follower.proxies.get("https")},
                args=["--disable-blink-features=AutomationControlled"],
            )
            if channel:
                launch_kw["channel"] = channel
            launch_kw.update(kw.pop("launch_args", {}))
            try:
                self._browser = await self._pw.chromium.launch(**launch_kw)
            except Exception as e:
                logger.warning("[⚠️] channel=%s 启动失败(%s)，回退内置 Chromium", channel, e)
                launch_kw.pop("channel", None)
                self._browser = await self._pw.chromium.launch(**launch_kw)
            # ⚠️ 不覆盖 user_agent —— 原生 UA 与 Sec-CH-UA 头才自洽，
            # 伪造版本号反而是自动化特征（实测踩过）。
            self._ctx = await self._browser.new_context(**kw)
            return self

        async def __aexit__(self, *exc):
            try:
                if self._ctx:
                    await self._ctx.close()
                if self._browser:
                    await self._browser.close()
            finally:
                if self._pw:
                    await self._pw.stop()
            return False

        @property
        def context(self):
            return self._ctx

        async def new_page(self):
            return await self._ctx.new_page()

        async def switch_and_reload(self, page, **switch_kw):
            """切换节点并重载页面 —— 重载是为了制造新连接，否则仍走旧出口。"""
            changed = await _to_thread(manager.switch_node, **switch_kw)
            if changed:
                await _to_thread(self._follower.sync)
                try:
                    await page.reload(wait_until="load", timeout=30000)
                except Exception as e:
                    logger.warning("[⚠️] 切换后重载失败: %s", e)
            return changed

    return _BrowserCtx()

# -*- coding: utf-8 -*-
"""clash_probe.py — 目标站探针 / 逐节点出口 IP / 网络体检

`clash_manager.py` 负责「用哪个节点」，本模块负责「怎么判断一个节点到底行不行」。

定位（使用场景）
----------------
`ClashManager` 内置的连通性检测是「拿到任意 HTTP 响应即算可达」。这在多数场景够用，
但对付**风控站点**不够 —— 判据往往不是 HTTP 状态码，而是**业务返回码**：

    HTTP 200 但 {"code": 50000}  → 该 IP 已被限流（不可用）
    HTTP 403 但 {"code": 403}    → 只是请求体不合法（**节点完全正常**）

同一个 403 在不同站点含义相反，所以判据必须由调用方按站点配置，不能写死在库里。
`SiteProbe` 就是承载这套判据的对象。

依赖
----
- requests （必选，与主库一致）

用法示例
--------
::

    from clash_probe import SiteProbe, probe_exit_ip, netcheck

    # ① 按站点配置判据（这里的例子来自某真实风控站）
    probe = SiteProbe(
        url="https://api.example.com/v2/captcha/generate?appId=xxx",
        method="POST", body={},                       # 空体请求即可区分封禁
        body_code_field="code",
        body_blocked_codes=(50000,),                  # 业务码 50000 = 被封
        blocked_status=(403,),                        # HTTP 403 = 正常（空体非法）
    )
    print(probe.probe(proxy="http://127.0.0.1:7897"))
    # {'verdict': 'ok', 'status': 403, 'body_code': 403, 'elapsed_ms': 164, ...}

    # ② 只想知道某个代理的真实出口 IP
    print(probe_exit_ip("http://127.0.0.1:7897"))     # '140.235.11.68'

    # ③ 一屏体检整个池子
    print(netcheck(manager))
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Sequence

import requests

logger = logging.getLogger("clash_probe")

# 逐节点测出口 IP 必须串行 —— 切换是全局状态，并发切换会互相踩
_SWITCH_LOCK = threading.Lock()

# 出口 IP 查询服务（可换成国内可达的同类服务）
DEFAULT_EXIT_IP_URL = "https://api.ipify.org"

# verdict 三态（与 ClashManager.report_verdict 对齐）
OK = "ok"
BLOCKED = "blocked"     # 拿到了响应，但被目标站拒绝
DEAD = "dead"           # 超时 / 连接被重置 / DNS 失败 —— 链路层不可用


def _session() -> requests.Session:
    """新建一个不读系统代理环境变量的 Session。

    ⚠️ 必须每次新建: keep-alive 会把旧隧道复用回来，切换节点后复用连接
    测到的还是旧出口（这是本项目踩过最多次的坑）。
    """
    s = requests.Session()
    s.trust_env = False
    return s


def _proxies(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    """把单个代理地址转成 requests 的 proxies 参数（None 表示直连）。"""
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


class SiteProbe:
    """按「特定站点的接口」判定一个出口是否可用。

    :param url: 探测 URL。**应当填目标站** —— 用 gstatic 之类的通用地址只能
        证明"能上网"，证明不了"能到目标站"（实测有一批节点通 gstatic 却完全
        到不了目标站，被误判为健康节点）。
    :param method: `GET` 或 `POST`。有些站的判据只在 POST 时返回（例如需要
        带一个空 JSON body 才会走到业务校验分支）。
    :param body: POST 的 JSON body。
    :param headers: 额外请求头。
    :param timeout: 单次探测超时（秒）。
    :param ok_status: 视为「正常」的 HTTP 状态码；默认 2xx。
    :param blocked_status: 视为「被拒」的 HTTP 状态码。
    :param body_code_field: 从 JSON 响应体里提取业务码的字段名（如 `code`）。
        给了它才会做业务码判定。
    :param body_blocked_codes: 业务码命中则判 blocked。
    :param body_ok_codes: 业务码命中则判 ok（优先级高于 blocked）。
    :param extra_dead_status: 视为「链路不通」的状态码。默认 `(502, 503, 504)`
        —— 网关错误通常意味着代理链路本身出了问题，而不是目标站在正常应答。

    判定优先级：业务码（若配置）> HTTP 状态码 > 兜底规则。

    兜底规则（**没被任何配置命中时**）：判 `ok`（可达）。这是刻意的保守方向 ——
    把没见过的状态码判成 `dead` 会把好节点踢出池子。但它也意味着
    **默认不会因为 HTTP 404/500 而判某个节点不可用**；如果你的站点需要用
    状态码判封禁，把对应值写进 `blocked_status` 即可。
    """

    def __init__(
        self,
        url: str,
        method: str = "GET",
        body: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 8.0,
        ok_status: Optional[Sequence[int]] = None,
        blocked_status: Optional[Sequence[int]] = None,
        body_code_field: Optional[str] = None,
        body_blocked_codes: Optional[Sequence[Any]] = None,
        body_ok_codes: Optional[Sequence[Any]] = None,
        extra_dead_status: Sequence[int] = (502, 503, 504),
    ):
        self.url = url
        self.method = method.upper()
        self.body = body
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.ok_status = tuple(ok_status) if ok_status is not None else tuple(range(200, 400))
        self.blocked_status = tuple(blocked_status or ())
        self.body_code_field = body_code_field
        self.body_blocked_codes = tuple(body_blocked_codes or ())
        self.body_ok_codes = tuple(body_ok_codes or ())
        self.extra_dead_status = tuple(extra_dead_status)

    # ---------------------------------------------------------------- 判定

    def classify(self, status_code: Optional[int], text: Optional[str] = None) -> str:
        """把一次响应映射成 ok / blocked / dead。

        业务码优先于 HTTP 状态码 —— 风控站点的真实判据通常藏在 body 里。
        """
        if status_code is None:
            return DEAD

        code = self._extract_body_code(text)
        if code is not None:
            if code in self.body_ok_codes:
                return OK
            if code in self.body_blocked_codes:
                return BLOCKED

        if status_code in self.extra_dead_status:
            return DEAD
        if status_code in self.blocked_status:
            return BLOCKED
        if status_code in self.ok_status:
            return OK
        # 没被显式归类，但拿到了 HTTP 响应 → 链路是通的。
        # 这是保守的正确方向：把「没见过的状态码」判成 dead 会误杀好节点。
        return OK

    def _extract_body_code(self, text: Optional[str]) -> Optional[Any]:
        if not self.body_code_field or not text:
            return None
        try:
            data = json.loads(text)
        except Exception:
            return None
        if isinstance(data, dict):
            return data.get(self.body_code_field)
        return None

    # ---------------------------------------------------------------- 探测

    def probe(self, proxy: Optional[str] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        """经指定代理探测一次，返回结构化结果。

        :param proxy: 代理地址（如 `http://127.0.0.1:7897`）。None = 直连。
        :return: `{"verdict", "status", "body_code", "elapsed_ms", "error", "url"}`
        """
        t0 = time.time()
        result = {
            "url": self.url,
            "proxy": proxy,
            "verdict": DEAD,
            "status": None,
            "body_code": None,
            "elapsed_ms": None,
            "error": None,
        }
        session = _session()
        try:
            kwargs = {
                "proxies": _proxies(proxy),
                "timeout": timeout or self.timeout,
                "headers": {**self.headers, "Connection": "close"},
            }
            if self.method == "POST":
                kwargs["json"] = {} if self.body is None else self.body
            resp = session.request(self.method, self.url, **kwargs)
            result["status"] = resp.status_code
            result["body_code"] = self._extract_body_code(resp.text)
            result["verdict"] = self.classify(resp.status_code, resp.text)
        except Exception as e:
            result["error"] = "%s: %s" % (type(e).__name__, e)
        finally:
            session.close()
        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        return result


# ---------------------------------------------------------------- 出口 IP

def probe_exit_ip(
    proxy: Optional[str] = None,
    url: str = DEFAULT_EXIT_IP_URL,
    timeout: float = 8.0,
) -> Optional[str]:
    """查指定代理的真实出口 IP（None = 直连），失败返回 None。

    ⚠️ 每次调用都新建连接: keep-alive 会把旧隧道复用回来，复用连接查到的
    还是旧节点的 IP，校验就失去意义了。
    """
    session = _session()
    try:
        resp = session.get(
            url,
            proxies=_proxies(proxy),
            timeout=timeout,
            headers={"Connection": "close"},
        )
        if resp.status_code == 200:
            return resp.text.strip()[:64]
        logger.warning("[⚠️] 查询出口 IP 返回 %s: %s", resp.status_code, url)
    except Exception as e:
        logger.warning("[⚠️] 查询出口 IP 失败(%s): %s", url, e)
    finally:
        session.close()
    return None


# ---------------------------------------------------------------- 网络体检

def netcheck(
    manager,
    nodes: Optional[List[str]] = None,
    workers: int = 4,
    probe: Optional[SiteProbe] = None,
    check_exit_ip: bool = True,
) -> Dict[str, Any]:
    """对代理池做一次完整体检：逐节点延迟 / 出口 IP / 目标站判定。

    与 `ClashManager` 的关系是**鸭子类型**的 —— 只要求它提供
    `available_nodes` / `get_node_delay()` / `set_node_by_name()` /
    `get_exit_ip()` / `group_name`，不 import 主库，避免循环依赖。

    ⚠️ 会真实切换节点（逐节点测出口 IP 必须切过去）。**别在采集任务正在跑时调用**，
    除非用 `check_exit_ip=False` 跳过出口 IP 部分。

    :param nodes: 只体检这些节点；None = 全部。
    :param workers: 并发数。默认 4 —— 刻意压低，几十个节点同时打目标站会触发
        服务端整体限流（风控看的是聚合速率，不是单请求）。
    :param probe: 目标站探针；None 则只做延迟与出口 IP。
    :param check_exit_ip: 是否逐节点测真实出口 IP（慢但最有信息量）。
    :return: `{"total", "nodes": [...], "summary": {...}}`
    """
    all_nodes = [n.get("name") for n in getattr(manager, "available_nodes", []) if n.get("name")]
    targets = [n for n in (nodes or all_nodes) if n in set(all_nodes)]
    if not targets:
        logger.warning("[⚠️] 没有可体检的节点（available_nodes 为空？）")
        return {"total": 0, "nodes": [], "summary": {}}

    logger.info("[🩺] 开始网络体检: %s 个节点，并发 %s", len(targets), workers)

    results: List[Dict[str, Any]] = []

    def _one(name: str) -> Dict[str, Any]:
        row: Dict[str, Any] = {"name": name, "delay_ms": None, "exit_ip": None,
                               "verdict": None, "status": None, "error": None}
        # 延迟（走 Clash 自带的 delay 测试，不切节点）
        try:
            row["delay_ms"] = manager.get_node_delay(name)
        except Exception as e:
            row["error"] = "delay: %s" % e

        # 出口 IP + 目标站判定需要切过去（串行段，见下方锁）
        if check_exit_ip or probe is not None:
            with _SWITCH_LOCK:
                if not manager.set_node_by_name(name):
                    row["error"] = "切换失败"
                    return row
                time.sleep(1.5)          # 给 mihomo 一点时间重建连接
                if check_exit_ip:
                    row["exit_ip"] = manager.get_exit_ip()
                if probe is not None:
                    # 直接经本地代理端口探（此时 GLOBAL 已指向该节点）
                    pr = probe.probe(proxy=_local_proxy(manager))
                    row["verdict"] = pr["verdict"]
                    row["status"] = pr["status"]
                    if pr["error"]:
                        row["error"] = pr["error"]
        return row

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_one, n) for n in targets]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                logger.warning("[⚠️] 体检线程异常: %s", e)

    order = {n: i for i, n in enumerate(targets)}
    results.sort(key=lambda r: order.get(r["name"], 999))

    summary = {
        "ok": sum(1 for r in results if r["verdict"] == OK),
        "blocked": sum(1 for r in results if r["verdict"] == BLOCKED),
        "dead": sum(1 for r in results if r["verdict"] == DEAD),
        "unique_exit_ips": len({r["exit_ip"] for r in results if r["exit_ip"]}),
    }
    logger.info("[🩺] 体检完成: ok=%s blocked=%s dead=%s 独立出口IP=%s",
                summary["ok"], summary["blocked"], summary["dead"],
                summary["unique_exit_ips"])
    return {"total": len(results), "nodes": results, "summary": summary}


def _local_proxy(manager) -> Optional[str]:
    """从 manager 取本地代理地址（用于 SiteProbe 直接经代理探测）。"""
    try:
        cfg = manager.get_proxy_config()
        return cfg.get("https") or cfg.get("http")
    except Exception:
        port = getattr(manager, "proxy_port", None)
        return "http://127.0.0.1:%s" % port if port else None

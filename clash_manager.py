# -*- coding: utf-8 -*-
"""ClashManager — 基于 Clash/Mihomo RESTful API 的机场节点代理池管理器。

定位（使用场景）
----------------
海外数据采集时，企业级海外代理按流量计费、价格昂贵。而机场订阅动辄提供
十几个到几十个节点，日常看视频根本用不完。本模块把这部分「闲置节点」管
起来，用于少量数据的采集任务（学术文献、公开数据、国外新闻等）：

    - 节点读取：从策略组拉取全部节点，过滤订阅元数据/嵌套策略组
    - 节点测试：Clash API 延迟测试 + 经代理出口真实访问目标站双重验证
    - 节点轮换：下载计数 / 403 计数触发自动切换，四种负载均衡策略
    - 节点管理：已验证节点池、错误节点黑名单（按站点隔离）、误杀保护
    - 健康守护：后台线程定时巡检，节点失效自动切换到健康节点

依赖
----
- requests   （必选）
- redis      （可选，跨进程共享错误黑名单，未安装时降级为进程内内存）

环境变量
--------
- CLASH_REDIS_URL   错误黑名单 Redis 地址，默认 redis://127.0.0.1:6379/0

用法示例
--------
::

    from clash_manager import ClashManager

    manager = ClashManager({
        "api_url": "http://127.0.0.1:9099",           # Clash 外部控制地址
        "secret": "your-secret",                      # 与 config.yaml 的 secret 一致
        "post_switch_test_url": "https://example.com/",  # 连通性检测 URL(必填)
        "group_name": "🚀节点选择",                    # 策略组名；global 模式下填 "GLOBAL"
        "proxy_port": 7777,                           # 本地代理端口
        "switch_strategy": "least_403",               # 负载均衡策略
        "force_global_mode": True,                    # rule 模式下切换无效，建议开启
    })

    # 请求处如实上报三态（推荐）:
    manager.report_verdict("ok")        # 成功
    manager.report_verdict("blocked")   # 被目标站拒绝（403/429/业务风控码）
    manager.report_verdict("dead")      # 超时/连接重置 → 该节点立即冷却

    # 旧接口同样可用:
    manager.increment_403_error()        # 等价于 report_verdict("blocked")
    manager.increment_download_count()   # 成功计数（每节点满 500 次轮换）

    # 想知道"切换到底有没有生效":
    print(manager.get_exit_ip())         # 经代理新建连接查一次真实出口 IP
"""

import datetime
import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

try:
    import redis  # type: ignore
except ImportError:  # redis 为可选依赖
    redis = None

logger = logging.getLogger("clash_manager")

# **非节点**类型 —— 用黑名单而不是白名单。
#
# 为什么改：原先是「允许列表」，只列了 Shadowsocks/Vmess/Trojan/Socks5/Vless/
# Http/Snell/Wireguard。而 mihomo 实际会返回 Hysteria2、Tuic、AnyTLS、Mieru 等
# 新协议 —— 实测一个 34 节点的订阅里 **15 个 Hysteria2 被静默丢弃**（占池子
# 一半，且恰恰是主力线路）。白名单在协议不断新增的场景下只会持续漏节点。
_NON_NODE_TYPES = {
    "Selector", "URLTest", "Fallback", "LoadBalance", "Relay",
    "Direct", "Reject", "RejectDrop", "Pass", "PassRule", "Compatible",
}

# 订阅元数据/推广条目特征（出现在节点名中的非节点条目）
#
# ⚠️ 加词要克制: 节点名常含「流量倍率」这类字样，用词太宽（比如单用「流量」）
# 会把全部线路误杀。这里只放"正规节点名里几乎不可能出现"的词。
# 实测机场常见的推广条目会伪装成正常节点类型（如 Vless 的
# 「1.永久官网《xxx.io》（Always Online)」），只按类型过滤拦不住。
_INVALID_NODE_PATTERNS = (
    "最新网址", "剩余流量", "过期时间", "订阅", "到期",
    "官网", "网址", "发布", "://", "试用", "加群", "邀请", "防失联",
)

# ⚠️ 这里千万别用裸的 "http" 当过滤词 —— 静态代理来源的节点名形如
# `http|1.2.3.4:8080`，会被整个误杀（离线单测 tests/sources_test.py 覆盖了这点）。
# 要拦的是「推广条目里的网址」，用 "://" 才精确。

# 支持的负载均衡策略
SWITCH_STRATEGIES = ("round_robin", "random", "least_403", "least_used")


class _ErrorNodeStore:
    """节点错误黑名单存储（Redis set，按站点域名隔离）。

    - 优先使用 Redis，便于多进程共享；
    - Redis 未安装/不可用时自动降级为进程内 set，不阻塞主流程。
    """

    def __init__(self, key: str):
        self.key = key
        self._fallback: set = set()
        self._redis = None
        if redis is not None:
            try:
                self._redis = redis.Redis.from_url(
                    os.environ.get("CLASH_REDIS_URL", "redis://127.0.0.1:6379/0"),
                    decode_responses=True,
                )
                self._redis.ping()
            except Exception as e:
                logger.warning("[⚠️] Redis 连接失败(%s)，错误节点黑名单降级为进程内内存", e)
                self._redis = None

    def get_set_members(self) -> set:
        if self._redis is not None:
            try:
                return self._redis.smembers(self.key)
            except Exception:
                pass
        return set(self._fallback)

    def add_to_set(self, value) -> None:
        self._fallback.add(str(value))
        if self._redis is not None:
            try:
                self._redis.sadd(self.key, str(value))
            except Exception:
                pass

    def remove_from_set(self, value) -> None:
        self._fallback.discard(str(value))
        if self._redis is not None:
            try:
                self._redis.srem(self.key, str(value))
            except Exception:
                pass

    def clear_value(self) -> None:
        self._fallback.clear()
        if self._redis is not None:
            try:
                self._redis.delete(self.key)
            except Exception:
                pass


class _FileLock:
    """跨平台文件锁（Windows: msvcrt / POSIX: fcntl），用于保护状态文件的读-改-写。

    没有它时多个进程各自 load → 各自 save，后写的会**整体覆盖**前者的所有字段
    （实测：批量采集时按站点分进程跑，`.pool_state.json` 里另一批节点的信誉
    全部丢失）。用完释放，失败时静默降级（加不上锁也比崩掉好）。
    """

    def __init__(self, path: str, timeout: float = 5.0):
        self.path = f"{path}.lock"
        self.timeout = timeout
        self._fd = None

    def __enter__(self):
        try:
            self._fd = open(self.path, "a+")
            deadline = time.time() + self.timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        self._fd.seek(0)
                        msvcrt.locking(self._fd.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError:
                    if time.time() >= deadline:
                        logger.warning("[⚠️] 状态文件加锁超时(%ss)，继续但可能与其它进程竞争: %s",
                                       self.timeout, self.path)
                        return self
                    time.sleep(0.05)
        except Exception as e:
            logger.warning("[⚠️] 状态文件加锁失败(%s)，继续执行", e)
            return self

    def __exit__(self, *exc):
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fd.seek(0)
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        finally:
            try:
                self._fd.close()
            except Exception:
                pass
            self._fd = None


class ClashManager:
    """Clash/Mihomo 代理节点池管理器。

    核心设计（生产踩坑沉淀）:
    1. 三重验证切换 —— ① 切换前用 Clash API 测候选节点延迟（跳过明显不可用的，
       避免白等 set_node + sleep）；② 切换后经本地代理出口真实访问目标站
       （**拿到任何 HTTP 响应即算可达**，403 也算——风控站上 403 往往代表节点
       正常，把它当"不通"会误杀好节点）；③ 再查一次**出口 IP**，必须与切换前
       不同才认成功。
       ⚠️ 前两步都过、出口却纹丝不动的情况真实存在且完全静默：Clash 处于 rule
       模式时 PUT 策略组对流量是 no-op；keep-alive 又会复用旧隧道。只有实测
       出口 IP 能识破（verify_exit_ip，默认开）。
    2. 已验证节点池 —— 只在上次验证通过的节点间轮换；池为空时才全量扫描构建。
    3. 指数冷却而非永久拉黑 —— 节点失败进入 cooldown_base_sec × 2ⁿ 冷却
       （封顶 cooldown_max_sec），到期自动回到轮换池。节点失败大多会自愈
       （IP 限流实测约 1 小时恢复），永久拉黑等于白白损失会恢复的节点。
    4. 错误黑名单按站点隔离 —— Redis key 含 post_switch_test_url 的域名，
       A 站封的 IP 不影响 B 站使用。
    5. 误杀保护 —— 错误节点数与总节点数接近时自动清空黑名单，避免死锁。
    6. 订阅元数据过滤 —— 节点名含「流量」「到期」等字样或嵌套策略组均被剔除，
       不能用单独的「流量」二字过滤，节点名常含「流量倍率」会误杀全部线路。
    7. 健康检测限并发 —— health_check_workers 默认 8。几十个节点同时打同一个
       目标站，单看每个都很温和，但**聚合速率足以触发服务端整体限流**。
    8. 状态文件加锁 + 合并写 —— 多进程共享 state_file 时逐键取大值合并，
       不会出现"后写的把前者的节点信誉整体覆盖掉"。
    """

    def __init__(self, config: Dict):
        # ---- 基础配置 ----
        self.api_url: str = config.get("api_url", "http://127.0.0.1:9097")
        self.secret: str = config.get("secret", "1")  # API 密钥
        self.group_name: str = config.get("group_name", "自动选择")  # 策略组名
        self.proxy_port: int = config.get("proxy_port", 7897)  # 本地代理端口
        self.state_file: str = config.get("state_file", "clash_node_state.json")

        # ---- 切换策略 ----
        self.enable_node_switching: bool = config.get("enable_node_switching", True)
        self.switch_strategy: str = config.get("switch_strategy", "round_robin")
        if self.switch_strategy not in SWITCH_STRATEGIES:
            logger.warning("[⚠️] 未知切换策略 '%s'，回退为 round_robin（可选: %s）",
                           self.switch_strategy, "/".join(SWITCH_STRATEGIES))
            self.switch_strategy = "round_robin"
        self.max_downloads_per_node: int = config.get("max_downloads_per_node", 500)
        self.max_403_errors: int = config.get("max_403_errors", 3)
        self.only_use_verified_nodes: bool = config.get("only_use_verified_nodes", True)
        self.show_node_info: bool = config.get("show_node_info", True)
        # 额外的节点名过滤词（你的订阅有内置词表拦不住的推广条目时补在这里）
        self.extra_invalid_patterns: List[str] = list(config.get("extra_invalid_patterns", []))

        # ---- 连通性检测 ----
        if not config.get("post_switch_test_url", ""):
            raise ValueError("配置错误: post_switch_test_url 参数不能为空 必须提供一个用于切换后连通性检测的URL")
        self.post_switch_test_url: str = config.get("post_switch_test_url", "https://www.gstatic.com/generate_204")
        self.post_switch_timeout_sec: int = config.get("post_switch_timeout_sec", 5)
        self.post_switch_max_retries: int = config.get("post_switch_max_retries", 3)
        self.pre_switch_delay_check: bool = config.get("pre_switch_delay_check", True)  # 切换前 API 测延迟
        self.require_post_switch_connectivity: bool = config.get("require_post_switch_connectivity", True)  # 切换后验出口

        # ---- 出口 IP 校验（默认开启）----
        # 「策略组 now 变了」≠「流量真的换了出口」。两种静默失效现有验证都拦不住:
        #   ① rule 模式下 PUT 策略组对实际流量是 no-op（流量走规则链）
        #   ② keep-alive 复用旧连接，切换后仍走旧节点
        # 唯一可靠判据 = 经代理查一次出口 IP，与切换前比对。
        self.verify_exit_ip: bool = config.get("verify_exit_ip", True)
        self.exit_ip_url: str = config.get("exit_ip_url", "https://api.ipify.org")
        self.exit_ip_timeout_sec: int = config.get("exit_ip_timeout_sec", 8)
        self.allow_same_exit_ip: bool = config.get("allow_same_exit_ip", False)  # 少数线路多节点共用出口 IP
        self.exit_ip_log_file: str = config.get("exit_ip_log_file", "")  # 非空则把出口 IP 溯源写入该 TSV

        # ---- 模式控制 ----
        # Clash 处于 rule 模式时，切换任何**不在规则链上**的策略组都不影响实际出口。
        # force_global_mode=True 会在启动时切到 global 并记住原模式，restore_mode() 可还原。
        self.force_global_mode: bool = config.get("force_global_mode", False)
        self._mode_before: Optional[str] = None

        # ---- 节点冷却/退避（替代"永久拉黑"）----
        # IP 限流/节点抖动大多会自愈（实测限流约 1h 恢复），永久拉黑会白白损失
        # 会恢复的节点。改为每节点独立指数冷却，失败越多次冷得越久。
        self.cooldown_base_sec: int = config.get("cooldown_base_sec", 120)
        self.cooldown_max_sec: int = config.get("cooldown_max_sec", 7200)
        # 全池熔断: 所有节点都在冷却时的静默等待（秒）。0 = 关闭该行为。
        # 为什么需要: 目标站整体限流是由**聚合请求速率**维持的，池子耗尽后
        # 每隔几分钟试探一轮等于持续添柴。实测静默 1-2 小时后自行恢复。
        self.pool_exhausted_wait_sec: int = config.get("pool_exhausted_wait_sec", 3600)

        # ---- 节点优先度（可选扩展 clash_policy.py）----
        # 置顶/拉黑/地区偏好/延迟阈值/倍率。不装扩展模块时退化为空策略。
        self.policy_disabled: bool = config.get("disable_policy", False)
        self.policy = self._build_policy(config.get("policy"))

        # ---- 代理来源（可选扩展 clash_sources.py）----
        # 不给 = 走原来的 Clash 策略组路径，行为与改造前逐字节一致。
        self.custom_source = self._build_source(config)
        self._spec_by_name: Dict[str, Any] = {}   # 节点名 -> NodeSpec（静态来源用）

        # ---- 健康检测 ----
        self.health_check_workers: int = config.get("health_check_workers", 8)
        # 健康检测/延迟测试用的 URL。默认 = post_switch_test_url（目标站），
        # 强烈建议就用目标站: 用 gstatic 之类的通用地址只能证明"能上网"，
        # 证明不了"能到目标站"——实测有一批节点通 gstatic 但完全到不了目标站。
        self.health_check_url: str = config.get("health_check_url", "") or self.post_switch_test_url
        # 目标站若是重页面（带 JS 的首页等），5s 会误杀好节点 —— 实测有节点
        # 能正常取数却在 5s 的 delay 探测里超时。默认放宽到 10s。
        self.health_check_timeout_ms: int = config.get("health_check_timeout_ms", 10000)

        # ---- 运行时状态 ----
        self.current_node_index: int = 0
        self.download_count: int = 0
        self.node_download_counts: Dict[str, int] = {}  # 各节点累计下载次数（负载均衡依据）
        self.node_403_counts: Dict[str, int] = {}       # 各节点累计 403 次数（least_403 依据）
        self.node_fail_counts: Dict[str, int] = {}      # 各节点连续失败次数（冷却时长依据）
        self.node_cooldowns: Dict[str, float] = {}      # 节点名 -> 冷却截止时间戳
        self.last_exit_ip: Optional[str] = None         # 最近一次实测出口 IP（溯源/切换校验）
        self.available_nodes: List[Dict] = []
        self.verified_nodes: set = set()  # 连通性已验证的节点名
        self.error_403_count: int = 0
        self.healthy_node_idx: int = -1
        self._switch_condition = threading.Condition()
        self._is_switching = False

        # ---- 错误黑名单（按站点域名隔离: 网站1 封的 ip 不影响网站2）----
        web_loc = urlparse(self.post_switch_test_url).netloc.replace(".", "_")
        if not web_loc:
            raise ValueError(f"{self.post_switch_test_url} 提取 web_loc {web_loc} 异常")
        self.error_node_set = _ErrorNodeStore(f"clash_error_node_set_{web_loc}")
        logger.info("[📂] 错误节点黑名单 key: clash_error_node_set_%s", web_loc)

        # ---- 启动: 恢复状态 + 模式检查 + 拉取节点 ----
        self.load_state()
        if self.force_global_mode:
            self.ensure_global_mode()
        if self.enable_node_switching:
            if self.refresh_nodes():
                self.refresh_node_health()
            # rule 模式下切策略组对流量是 no-op —— 提前把话说清楚，
            # 免得用户以为在轮换节点、实际一直用同一个出口。
            mode = self.get_mode()
            if mode and mode != "global" and self.verify_exit_ip:
                logger.warning("[⚠️] Clash 当前为 %s 模式: 切换策略组**不改变实际出口**"
                               "（流量按规则链走）。要真正按节点轮换，请设 "
                               "force_global_mode=True 或手动切到 global 模式。"
                               "出口 IP 校验已开启，若切换无效会明确报错。", mode)

    def _build_source(self, config: Dict):
        """构造代理来源（需要可选的 clash_sources.py）。

        没配 `source`/`sources` 时返回 None —— 主库走原来的 Clash 策略组路径。
        """
        raw = config.get("source") or ({"sources": config["sources"]}
                                       if config.get("sources") else None)
        if not raw:
            return None
        try:
            from clash_sources import ProxySource, build_source
        except ImportError:
            logger.error("[❌] 配置了 source/sources 但找不到 clash_sources.py（该模块随仓库提供）")
            return None
        if isinstance(raw, ProxySource):
            logger.info("[🔌] 代理来源: %s", raw.describe())
            return raw
        merged = dict(raw)
        merged.setdefault("api_url", self.api_url)
        merged.setdefault("secret", self.secret)
        merged.setdefault("group_name", self.group_name)
        merged.setdefault("proxy_port", self.proxy_port)
        source = build_source(merged)
        if source is not None:
            logger.info("[🔌] 代理来源: %s", source.describe())
        return source

    def _spec_of(self, node_name: str, node: Optional[Dict] = None):
        """取节点对应的 NodeSpec（自定义来源才可能有；否则 None）。"""
        from_spec = (node or {}).get("spec")
        if from_spec is not None:
            return from_spec
        return self._spec_by_name.get(node_name)

    def _build_policy(self, cfg: Optional[Dict]):
        """构造优先度策略对象（需要可选的 clash_policy.py）。"""
        if self.policy_disabled:
            return None
        if not cfg:
            cfg = {}
        if isinstance(cfg, str):                     # policy 直接给文件名
            cfg = {"priority_file": cfg}
        cfg = dict(cfg)
        cfg.setdefault("priority_file", self.state_file + ".priority.json")
        try:
            from clash_policy import NodePolicy
        except ImportError:
            if cfg.get("pin_nodes") or cfg.get("block_nodes") or cfg.get("prefer_regions"):
                logger.warning("[⚠️] 配置了优先度规则但找不到 clash_policy.py，"
                               "这些规则不会生效（该模块随仓库提供）")
            return None
        policy = NodePolicy(cfg)
        logger.info("[🎯] 节点优先度: %s", policy.describe([]))
        return policy

    # ======================== 状态持久化 ========================

    def load_state(self) -> None:
        """从本地 JSON 恢复节点状态。"""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                    self.current_node_index = state.get('current_node_index', 0)
                    self.download_count = state.get('download_count', 0)
                    self.node_download_counts = state.get('node_download_counts', {})
                    self.node_403_counts = state.get('node_403_counts', {})
                    self.node_fail_counts = state.get('node_fail_counts', {})
                    self.node_cooldowns = state.get('node_cooldowns', {})
                    self.verified_nodes = set(state.get('verified_nodes', []))
                    self.last_exit_ip = state.get('last_exit_ip')
                    logger.info("[📂] 加载节点状态: 索引=%s, 下载计数=%s, 已验证节点=%s",
                                self.current_node_index, self.download_count, len(self.verified_nodes))
            else:
                logger.info("[📂] 未找到节点状态文件，使用默认值")
        except Exception as e:
            logger.warning("[⚠️] 加载节点状态失败: %s", e)

    def save_state(self) -> None:
        """保存当前状态（文件锁 + 与磁盘现状合并 + temp/rename 原子写入）。

        合并的必要性: 多个进程各持一份内存状态，若直接整体覆盖写，后写的会把
        另一批节点的信誉（ok/fail/冷却）全部抹掉。这里在锁内先把磁盘上的计数
        读回来逐键取大值合并，再落盘，让多进程各自贡献而不是互相覆盖。
        """
        try:
            with _FileLock(self.state_file):
                disk = {}
                if os.path.exists(self.state_file):
                    try:
                        with open(self.state_file, 'r', encoding='utf-8') as f:
                            disk = json.load(f) or {}
                    except Exception:
                        disk = {}

                def _merge_max(local: Dict, remote: Dict) -> Dict:
                    """计数类字段取大值（计数器单调递增，大者更新）。"""
                    out = dict(remote or {})
                    for k, v in (local or {}).items():
                        out[k] = max(out.get(k, 0), v)
                    return out

                state = {
                    'current_node_index': self.current_node_index,
                    'download_count': max(self.download_count, disk.get('download_count', 0)),
                    'node_download_counts': _merge_max(self.node_download_counts,
                                                       disk.get('node_download_counts', {})),
                    'node_403_counts': _merge_max(self.node_403_counts,
                                                  disk.get('node_403_counts', {})),
                    'node_fail_counts': _merge_max(self.node_fail_counts,
                                                   disk.get('node_fail_counts', {})),
                    # 冷却时间戳取"更晚的那个"（更晚 = 冷得更久，保守）
                    'node_cooldowns': {
                        k: max(v, (disk.get('node_cooldowns', {}) or {}).get(k, 0))
                        for k, v in {**(disk.get('node_cooldowns', {}) or {}),
                                     **self.node_cooldowns}.items()
                    },
                    'verified_nodes': list(set(self.verified_nodes)
                                           | set(disk.get('verified_nodes', []) or [])),
                    'last_exit_ip': self.last_exit_ip or disk.get('last_exit_ip'),
                    'timestamp': time.time()
                }
                tmp_file = f"{self.state_file}.tmp"
                with open(tmp_file, 'w', encoding='utf-8') as f:
                    json.dump(state, f, indent=2, ensure_ascii=False)
                os.replace(tmp_file, self.state_file)
            logger.debug("[💾] 节点状态已保存: 索引=%s, 下载计数=%s",
                         self.current_node_index, self.download_count)
        except Exception as e:
            logger.warning("[⚠️] 保存节点状态失败: %s", e)

    # ======================== 代理环境变量处理 ========================
    # 访问 Clash API 必须直连，避免全局代理环境变量导致 502

    def _disable_proxy_env(self) -> Dict[str, str]:
        """临时禁用代理环境变量，返回原始值以便恢复。"""
        proxy_vars = ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']
        original_values = {}
        for var in proxy_vars:
            if var in os.environ:
                original_values[var] = os.environ[var]
                del os.environ[var]
        return original_values

    def _restore_proxy_env(self, original_values: Dict[str, str]) -> None:
        """恢复代理环境变量。"""
        for var, value in original_values.items():
            os.environ[var] = value

    def _api_request(self, method: str, path: str, **kwargs) -> requests.Response:
        """经直连发起 Clash API 请求（自动附带 secret、禁用代理环境变量）。"""
        headers = kwargs.pop("headers", {})
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        original_proxies = self._disable_proxy_env()
        try:
            return requests.request(
                method,
                f"{self.api_url}{path}",
                headers=headers,
                timeout=kwargs.pop("timeout", 10),
                proxies={"http": None, "https": None},
                **kwargs,
            )
        finally:
            self._restore_proxy_env(original_proxies)

    def _find_available_groups(self) -> List[str]:
        """查找所有 Selector 类型策略组。"""
        try:
            response = self._api_request("GET", "/proxies")
            if response.status_code == 200:
                data = response.json()
                return [
                    name for name, info in data.get('proxies', {}).items()
                    if isinstance(info, dict) and info.get('type') == 'Selector'
                ]
        except Exception as e:
            logger.warning("[⚠️] 查找策略组失败: %s", e)
        return []

    # ======================== 节点发现 ========================

    def _fetch_nodes(self) -> tuple:
        """取候选节点，返回 `(all_nodes, all_proxies)`；失败返回 `(None, None)`。

        - 配了自定义来源（`clash_sources.ProxySource`）→ 由来源提供
        - 否则走原来的 Clash 策略组读取路径（行为与改造前逐字节一致）
        """
        if self.custom_source is not None:
            try:
                specs = self.custom_source.list_nodes()
            except Exception as e:
                logger.error("[‼] 来源读取失败(%s): %s", self.custom_source.describe(), e)
                return None, None
            logger.info("[🔍] 来源 %s 提供 %s 个候选",
                        self.custom_source.describe(), len(specs))
            # 记下「名字 → NodeSpec」，后面切换/取代理地址都要用
            self._spec_by_name = {s.name: s for s in specs}
            return [s.to_dict() for s in specs], {}

        try:
            response = self._api_request("GET", f"/proxies/{self.group_name}")

            # 策略组不存在（404）时自动查找可用策略组
            if response.status_code == 404:
                logger.warning("[⚠️] 策略组 '%s' 不存在，查找可用的策略组...", self.group_name)
                available_groups = self._find_available_groups()
                if available_groups:
                    logger.info("[🔍] 找到 %s 个可用策略组: %s",
                                len(available_groups), ", ".join(available_groups[:10]))
                    self.group_name = available_groups[0]
                    logger.info("[✅] 自动切换到策略组: %s", self.group_name)
                    response = self._api_request("GET", f"/proxies/{self.group_name}")
                else:
                    logger.error("[❌] 未找到任何可用的策略组")
                    return None, None

            if response.status_code != 200:
                logger.warning("[⚠️] 获取节点列表失败: %s", response.status_code)
                if response.status_code == 401:
                    logger.info("[💡] 提示: 可能需要设置正确的API密钥 (secret)")
                elif response.status_code == 404:
                    logger.info("[💡] 提示: 策略组 '%s' 不存在，请检查Clash配置")
                return None, None

            data = response.json()
            logger.info("[🔍] 从策略组 '%s' 获取节点信息", self.group_name)

            # 获取全部代理信息，用于识别嵌套策略组
            all_proxies = {}
            try:
                proxies_response = self._api_request("GET", "/proxies")
                if proxies_response.status_code == 200:
                    all_proxies = proxies_response.json().get("proxies", {})
            except Exception as e:
                logger.warning("[⚠️] 获取所有代理信息失败: %s", e)

            return data.get("all", []), all_proxies
        except Exception as e:
            logger.error("[‼] 连接Clash API失败: %s", e)
            return None, None

    def refresh_nodes(self) -> bool:
        """拉取可用节点列表（过滤订阅元数据/嵌套策略组/错误黑名单）。"""
        try:
            error_nodes = self.error_node_set.get_set_members()
            all_nodes, all_proxies = self._fetch_nodes()
            if all_nodes is None:
                return False

            # 误杀保护: 错误节点数与总节点数接近时清空黑名单，避免死锁
            if all_nodes and abs(len(error_nodes) - len(all_nodes)) < 5:
                logger.warning("[⚠️] 策略组节点数(%s)与错误节点数(%s)接近，可能误杀过多节点，清空错误节点缓存",
                               len(all_nodes), len(error_nodes))
                self.error_node_set.clear_value()
                error_nodes = set()

            # 兼容两种结构: [{"name":..,"type":..}] 或 ["节点1", ...]
            self.available_nodes = []
            if all_nodes:
                for raw_node in all_nodes:
                    if isinstance(raw_node, dict):
                        node_name, node_type = raw_node.get("name"), raw_node.get("type", "Unknown")
                    elif isinstance(raw_node, str):
                        node_name, node_type = raw_node, all_proxies.get(raw_node, {}).get("type", "Unknown")
                    else:
                        continue

                    if not node_name:
                        continue
                    # 过滤订阅元数据 / 推广条目（最新网址、剩余流量、到期、官网…）
                    if any(p in str(node_name)
                           for p in _INVALID_NODE_PATTERNS + tuple(self.extra_invalid_patterns)):
                        continue
                    # 过滤嵌套策略组
                    if all_proxies.get(node_name, {}).get("type") == "Selector":
                        continue
                    # 过滤**非节点**类型（黑名单制，见 _NON_NODE_TYPES 注释）
                    if node_type in _NON_NODE_TYPES:
                        continue
                    # 过滤错误黑名单节点
                    if node_name in error_nodes:
                        continue

                    entry = {
                        "name": node_name,
                        "type": node_type,
                        "delay": 0,
                        "healthy": True,
                        "last_check": time.time(),
                    }
                    # 地区标记（优先度规则与 netcheck 都用到）
                    if self.policy is not None:
                        try:
                            from clash_policy import region_of
                            entry["region"] = region_of(node_name)
                        except ImportError:
                            pass
                    # 优先度剔除（拉黑 / 排除地区；延迟阈值留到测完延迟再判）
                    if self.policy is not None:
                        reason = self.policy.reject_reason(entry)
                        if reason and reason.startswith(("已拉黑", "排除地区")):
                            logger.info("[⏭️] 优先度剔除 %s: %s", node_name, reason)
                            continue

                    self.available_nodes.append(entry)

            self._validate_node_index()

            if self.show_node_info:
                for i, node in enumerate(self.available_nodes):
                    logger.info("    %s. %s (%s) - 延迟: 未检测", i + 1, node['name'], node['type'])

            return True

        except Exception as e:
            logger.error("[‼] 连接Clash API失败: %s", e)
            return False

    def _validate_node_index(self) -> None:
        """验证并修正当前节点索引。"""
        if self.available_nodes and self.current_node_index >= len(self.available_nodes):
            logger.warning("[⚠️] 当前节点索引 %s 超出范围，重置为0", self.current_node_index)
            self.current_node_index = 0

    # ======================== 节点信息 ========================

    def get_current_node(self) -> Optional[Dict]:
        """获取当前节点信息。"""
        if not self.available_nodes:
            return None
        self._validate_node_index()
        return self.available_nodes[self.current_node_index]

    def get_proxy_config(self) -> Dict:
        """获取当前出口的代理配置（传给 requests 的 proxies 参数）。

        静态来源的节点自带 `proxy` 地址（不经过 Clash），直接返回它；
        Clash 来源则统一走本地代理端口（由 mihomo 按当前选中的节点转发）。
        """
        node = self.get_current_node()
        spec = self._spec_of((node or {}).get("name", ""), node) if node else None
        if spec is not None and getattr(spec, "proxy", None):
            return {"http": spec.proxy, "https": spec.proxy}
        return {
            "http": f"http://127.0.0.1:{self.proxy_port}",
            "https": f"http://127.0.0.1:{self.proxy_port}",
        }

    # ======================== 运行模式（rule / global）========================

    def get_mode(self) -> Optional[str]:
        """读取 Clash 当前模式（rule / global / direct）。"""
        try:
            resp = self._api_request("GET", "/configs", timeout=5)
            if resp.status_code == 200:
                return (resp.json() or {}).get("mode")
        except Exception as e:
            logger.warning("[⚠️] 读取 Clash 模式失败: %s", e)
        return None

    def ensure_global_mode(self) -> bool:
        """切到 global 模式并记住原模式（供 restore_mode 还原）。

        为什么需要: **rule 模式下切换策略组不改变实际出口** —— 流量按 rules 链
        行走，PUT /proxies/<组> 只是改了组的 now，对流量是 no-op。想真正按节点
        轮换出口，必须处于 global 模式（此时 GLOBAL 组即实际出口）。
        """
        mode = self.get_mode()
        if mode is None:
            return False
        if mode == "global":
            logger.info("[🌐] Clash 已是 global 模式")
            return True
        self._mode_before = mode
        try:
            resp = self._api_request("PATCH", "/configs", json={"mode": "global"}, timeout=5)
            if resp.status_code in (200, 204):
                logger.info("[🌐] Clash 模式 %s -> global（原模式已记录，可 restore_mode() 还原）", mode)
                return True
        except Exception as e:
            logger.warning("[⚠️] 切换 global 模式失败: %s", e)
        return False

    def restore_mode(self) -> None:
        """还原 ensure_global_mode 之前的模式。"""
        if not self._mode_before or self._mode_before == "global":
            return
        try:
            self._api_request("PATCH", "/configs", json={"mode": self._mode_before}, timeout=5)
            logger.info("[🌐] Clash 模式已还原为 %s", self._mode_before)
            self._mode_before = None
        except Exception as e:
            logger.warning("[⚠️] 还原 Clash 模式失败: %s", e)

    # ======================== 出口 IP 校验 ========================

    def get_exit_ip(self, proxy: Optional[str] = None) -> Optional[str]:
        """经代理查询出口 IP（新连接 + 忽略系统代理环境变量）。

        :param proxy: 指定代理地址；None = 当前节点的出口。

        ⚠️ 必须每次新建连接: keep-alive 会把旧隧道复用回来，复用连接查到的
        还是旧节点的 IP，校验就失去意义。
        """
        try:
            session = requests.Session()
            session.trust_env = False          # 否则系统代理环境变量会叠加/覆盖
            try:
                resp = session.get(
                    self.exit_ip_url,
                    proxies={"http": proxy, "https": proxy} if proxy else self.get_proxy_config(),
                    timeout=self.exit_ip_timeout_sec,
                    headers={"Connection": "close"},
                )
                if resp.status_code == 200:
                    self.last_exit_ip = resp.text.strip()[:64]
                    return self.last_exit_ip
                logger.warning("[⚠️] 查询出口 IP 返回 %s: %s", resp.status_code, self.exit_ip_url)
            finally:
                session.close()
        except Exception as e:
            logger.warning("[⚠️] 查询出口 IP 失败(%s): %s", self.exit_ip_url, e)
        return None

    def get_exit_ip_of(self, node_name: str, restore: bool = True) -> Optional[str]:
        """查**指定节点**的真实出口 IP（逐节点溯源 / 体检用）。

        与 `get_exit_ip()` 的区别: 后者只能测「当前选中的那个出口」。
        这里会切过去测完再切回来（`restore=True`）。

        ⚠️ 会真实切换节点 —— 别在采集任务并发跑的时候调用。
        """
        if node_name not in {n.get("name") for n in self.available_nodes}:
            logger.warning("[⚠️] 节点不在可用列表里: %s", node_name)
            return None
        previous = (self.get_current_node() or {}).get("name")
        if not self.set_node_by_name(node_name):
            return None
        time.sleep(1.2)                        # 给 mihomo 时间重建连接
        ip = self.get_exit_ip()
        if restore and previous and previous != node_name:
            self.set_node_by_name(previous)
            time.sleep(0.8)
        return ip

    def _record_exit_ip(self, node_name: str, exit_ip: Optional[str]) -> None:
        """出口 IP 溯源: 记内存 + 可选落盘 TSV（判断"切换是否真生效"的硬证据）。"""
        if not exit_ip:
            return
        self.last_exit_ip = exit_ip
        if not self.exit_ip_log_file:
            return
        try:
            with open(self.exit_ip_log_file, "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now().isoformat()}\t{node_name}\t{exit_ip}\n")
        except Exception as e:
            logger.warning("[⚠️] 写出口 IP 溯源文件失败: %s", e)

    # ======================== 节点温度（冷却/退避）========================

    def is_cooled(self, node_name: str) -> bool:
        """节点是否处于冷却期。"""
        return self.node_cooldowns.get(node_name, 0) > time.time()

    def cooldown_remaining(self, node_name: str) -> int:
        """剩余冷却秒数（0 表示未冷却）。"""
        return max(0, int(self.node_cooldowns.get(node_name, 0) - time.time()))

    def mark_node_failed(self, node_name: str, reason: str = "") -> int:
        """标记节点失败: 连续失败次数 +1，进入指数冷却。返回本次冷却秒数。

        冷却而非永久拉黑: 节点抖动/IP 限流大多会自愈（实测限流约 1 小时），
        永久拉黑等于把会恢复的节点白白损失掉。
        """
        if not node_name:
            return 0
        self.node_fail_counts[node_name] = self.node_fail_counts.get(node_name, 0) + 1
        n = self.node_fail_counts[node_name]
        cool = min(self.cooldown_base_sec * (2 ** (n - 1)), self.cooldown_max_sec)
        self.node_cooldowns[node_name] = time.time() + cool
        self.verified_nodes.discard(node_name)
        logger.warning("[🧊] 节点 %s 失败(%s) → 冷却 %ss（连续失败 %s 次）",
                       node_name, reason or "未注明", cool, n)
        self.save_state()
        return cool

    def mark_node_ok(self, node_name: str) -> None:
        """标记节点成功: 清除冷却与连续失败计数，并衰减历史 403 计数。

        403 计数必须有"成功即衰减"，否则 least_403 策略跑久了会退化成
        「永远选那几个历史清零的节点」，哪怕它们现在早就坏了。
        """
        if not node_name:
            return
        self.node_fail_counts[node_name] = 0
        self.node_cooldowns[node_name] = 0.0
        if self.node_403_counts.get(node_name, 0) > 0:
            self.node_403_counts[node_name] = max(0, self.node_403_counts[node_name] - 1)

    def get_healthy_nodes(self) -> List[Dict]:
        """获取所有健康节点（自动排除冷却中的节点）。"""
        now = time.time()
        return [n for n in self.available_nodes
                if n.get("healthy")
                and n.get("name") != "DIRECT"
                and self.node_cooldowns.get(n.get("name", ""), 0) <= now]

    def get_unhealthy_nodes(self) -> List[Dict]:
        """获取所有不健康节点。"""
        return [n for n in self.available_nodes if not n.get("healthy")]

    def get_status(self) -> Dict:
        """获取状态信息。"""
        current_node = self.get_current_node()
        return {
            "current_node": current_node["name"] if current_node else None,
            "strategy": self.switch_strategy,
            "download_count": self.download_count,
            "node_download_counts": self.node_download_counts.copy(),
            "node_403_counts": self.node_403_counts.copy(),
            "available_nodes_count": len(self.available_nodes),
            "enable_node_switching": self.enable_node_switching,
            "error_403_count": self.error_403_count,
            "mode": self.get_mode(),
            "exit_ip": self.last_exit_ip,
            "healthy_nodes_count": len(self.get_healthy_nodes()),
            "cooling_nodes": {n: self.cooldown_remaining(n)
                              for n in self.node_cooldowns if self.is_cooled(n)},
        }

    # ======================== 健康检测与延迟测试 ========================

    def get_node_delay(self, node_name: str) -> Optional[int]:
        """通过 Clash API 测试指定节点延迟(ms)，失败返回 None。

        探测 URL 用 health_check_url（默认 = post_switch_test_url = 目标站）。
        ⚠️ 不要用 gstatic/generate_204 这类通用地址: 它只能证明"能上网"，
        证明不了"能到目标站"——实测有一批节点通 gstatic 却完全到不了目标站，
        会被误判为健康节点，白白浪费采集轮次。
        """
        # 自定义来源可能自带延迟测试（如静态代理直接打目标站）
        spec = self._spec_of(node_name)
        if self.custom_source is not None and spec is not None:
            try:
                delay = self.custom_source.probe_delay(spec, self.health_check_url,
                                                       self.health_check_timeout_ms)
                if delay is not None:
                    return delay
            except Exception as e:
                logger.debug("[⚠️] 来源延迟测试失败(%s): %s", node_name, e)
        try:
            import urllib.parse
            encoded_name = urllib.parse.quote(node_name)
            params = {"url": self.health_check_url, "timeout": self.health_check_timeout_ms}
            response = self._api_request("GET", f"/proxies/{encoded_name}/delay", params=params, timeout=10)
            if response.status_code == 200:
                return response.json().get("delay")
        except Exception:
            pass
        return None

    def _check_single_node_health(self, node: Dict) -> bool:
        """检测单个节点健康状态（5分钟内复用缓存结果）。"""
        try:
            if node.get("last_check", 0) > time.time() - 300:  # 5分钟缓存
                return node.get("healthy", True)

            delay = self.get_node_delay(node["name"])
            node["last_check"] = time.time()
            if delay is not None and delay > 0:
                node["delay"] = delay
                node["healthy"] = True
                logger.info("[✅] 节点 %s 健康，延迟: %sms", node['name'], delay)
                return True
            node["delay"] = 0
            node["healthy"] = False
            logger.info("[❌] 节点 %s 不健康，延迟: %s", node['name'], delay)
            return False
        except Exception as e:
            logger.warning("[⚠️] 检测节点 %s 健康状态失败: %s", node['name'], e)
            node["healthy"] = False
            node["delay"] = 0
            node["last_check"] = time.time()
            return False

    def _check_node_health(self) -> None:
        """并发检测所有节点健康状态。"""
        if not self.available_nodes:
            logger.warning("[⚠️] 没有可用节点进行健康检测")
            return

        # 并发度必须设上限: 78 个节点就是 78 个线程同时打同一个目标站，
        # 会在目标侧形成突发流量 —— 实测这正是触发服务端整体限流的典型原因
        # （"单个请求都很温和，但聚合速率把风控叫醒了"）。
        workers = max(1, min(self.health_check_workers, len(self.available_nodes)))
        if workers < len(self.available_nodes):
            logger.info("[📊] 健康检测并发度限制为 %s（共 %s 个节点）",
                        workers, len(self.available_nodes))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(self._check_single_node_health, node) for node in self.available_nodes]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logger.warning("[⚠️] 健康检测线程异常: %s", e)

        healthy_count = sum(1 for n in self.available_nodes if n.get("healthy"))
        logger.info("[📊] 健康节点: %s/%s", healthy_count, len(self.available_nodes))

    def refresh_node_health(self) -> bool:
        """手动刷新全部节点健康状态。"""
        logger.info("[🔄] 手动刷新节点健康状态...")
        try:
            self._check_node_health()
            logger.info("[✅] 节点健康状态刷新完成")
            return True
        except Exception as e:
            logger.error("[❌] 刷新节点健康状态失败: %s", e)
            return False

    # ======================== 连通性检测 ========================

    def test_connection(self) -> bool:
        """测试 Clash API 连通性。"""
        try:
            response = self._api_request("GET", "/version", timeout=5)
            if response.status_code == 200:
                version = response.json().get("version", "Unknown")
                logger.info("[✅] Clash连接正常，版本: %s", version)
                return True
            logger.warning("[⚠️] Clash连接异常: %s", response.status_code)
            return False
        except Exception as e:
            logger.error("[‼] 无法连接到Clash: %s", e)
            return False

    def probe_endpoint(self, url: Optional[str] = None) -> tuple:
        """经本地代理出口访问目标站，返回 (verdict, status_code)。

        verdict 三态（这个区分很关键）:
            "ok"      2xx —— 正常
            "blocked" 403/429 等 —— **节点是通的**，只是被目标站拒绝。
                      实测风控站点上 403 恰恰常代表"节点正常且可达"
                      （例如请求体不合法），把它当成"节点不通"会误杀好节点。
            "dead"    超时/连接错误 —— 才是真正的不通

        注意 trust_env=False: 否则系统代理环境变量会与 proxies= 叠加。
        """
        try:
            session = requests.Session()
            session.trust_env = False
            try:
                resp = session.get(url or self.post_switch_test_url,
                                   proxies=self.get_proxy_config(),
                                   timeout=self.post_switch_timeout_sec,
                                   headers={"Connection": "close"})
                code = resp.status_code
                return ("ok" if 200 <= code < 400 else "blocked"), code
            finally:
                session.close()
        except Exception:
            return "dead", None

    def _post_switch_connectivity_ok(self) -> bool:
        """切换后连通性是否通过（保留原方法名；语义已修正）。

        修正点: 原来只认 200/204，403 被判为"不通"，会把被目标站拒绝但链路
        完全正常的好节点从已验证池里踢掉。现在只要**拿到了 HTTP 响应**就算
        可达，只有超时/连接错误才算不通。
        """
        verdict, _ = self.probe_endpoint()
        return verdict != "dead"

    def netcheck(self, nodes: Optional[List[str]] = None, workers: int = 4,
                 probe=None, check_exit_ip: bool = True) -> Dict:
        """网络体检: 逐节点延迟 / 真实出口 IP / 目标站判定。

        实现在可选的 `clash_probe.py` 里（本项目根目录，随仓库提供）。
        ⚠️ `check_exit_ip=True` 会真实切换节点，**别和采集任务并发跑**。
        """
        try:
            from clash_probe import netcheck as _netcheck
        except ImportError:
            logger.error("[❌] 未找到 clash_probe.py（体检功能在可选的扩展模块里）")
            return {"total": 0, "nodes": [], "summary": {}}
        return _netcheck(self, nodes=nodes, workers=workers,
                         probe=probe, check_exit_ip=check_exit_ip)

    # ======================== 统计与触发切换 ========================

    def increment_download_count(self) -> bool:
        """下载成功时调用：累计计数，达到阈值自动轮换节点。"""
        self.download_count += 1
        current_node = self.get_current_node()
        if current_node:
            node_name = current_node["name"]
            self.node_download_counts[node_name] = self.node_download_counts.get(node_name, 0) + 1
            self.mark_node_ok(node_name)   # 清除冷却 + 衰减该节点 403 计数

        self.reset_403_error()  # 下载成功重置403计数
        self.save_state()

        if self.download_count >= self.max_downloads_per_node:
            return self.switch_node()
        return False

    def report_success(self, node_name: Optional[str] = None) -> None:
        """上报一次成功：清除该节点冷却/连续失败计数，衰减其 403 计数。"""
        node = node_name or (self.get_current_node() or {}).get("name")
        self.mark_node_ok(node)
        self.reset_403_error()

    def report_verdict(self, verdict: str, node_name: Optional[str] = None) -> bool:
        """统一上报出口判定（推荐的集成方式）。

        :param verdict: "ok" / "blocked" / "dead"
            ok       —— 成功取到数据
            blocked  —— 拿到响应但被目标站拒绝（403/429/业务风控码…）
            dead     —— 超时、连接被重置、DNS 失败等链路层不可用
        :param node_name: 不传则取当前节点
        :return: 是否触发了节点切换

        为什么要有这个接口: "403 是否等于节点被封"取决于目标站。同一个 403，
        对 A 站是权限不足（与节点无关），对 B 站才是 IP 被封。把判据交给调用方，
        比在库里硬编码"403 = 封禁"可靠。increment_403_error() 保留为兼容别名。
        """
        verdict = (verdict or "").lower()
        node = node_name or (self.get_current_node() or {}).get("name")

        if verdict in ("ok", "success", "200"):
            self.report_success(node)
            return False

        if verdict in ("dead", "timeout", "unreachable", "conn_error"):
            # 链路层不通 → 立即冷却 + 换节点（没什么可等的）
            self.mark_node_failed(node, "链路不通")
            self.error_403_count += 1
            if self.error_403_count >= self.max_403_errors:
                self.error_403_count = 0
                return self.switch_node()
            return False

        if verdict in ("blocked", "403", "banned", "429"):
            if node:
                self.node_403_counts[node] = self.node_403_counts.get(node, 0) + 1
                # 单次 403 不冷却（可能是缺 header 之类的偶发），
                # 该节点累计到阈值才判定它对本站不可用并冷却。
                if self.node_403_counts[node] >= self.max_403_errors:
                    self.mark_node_failed(node, f"累计 {self.node_403_counts[node]} 次被拒")
            self.error_403_count += 1
            logger.warning("[⚠️] 被拒计数: %s/%s", self.error_403_count, self.max_403_errors)
            if self.error_403_count >= self.max_403_errors:
                logger.info("[🔄] 达到最大被拒次数，切换节点")
                self.error_403_count = 0
                return self.switch_node()
            return False

        logger.warning("[⚠️] 未知 verdict '%s'，忽略", verdict)
        return False

    def increment_403_error(self) -> bool:
        """目标站返回 403 时调用（保留原名，等价于 report_verdict("blocked")）。

        ⚠️ 只在"403 确实意味着该节点对本站不可用"时使用。若目标站的 403 是
        权限/参数问题（与出口 IP 无关），请改用 report_verdict() 自定义判据。
        """
        return self.report_verdict("blocked")

    def reset_403_error(self) -> None:
        """下载成功时重置403计数。"""
        if self.error_403_count > 0:
            logger.info("[✅] 下载成功，重置403错误计数: %s -> 0", self.error_403_count)
            self.error_403_count = 0

    # ======================== 节点切换 ========================

    def switch_node(self, max_retries: Optional[int] = None) -> bool:
        """切换节点（线程安全，并发调用会排队等待）。"""
        if not self.enable_node_switching or not self.available_nodes:
            logger.warning("[⚠️] 无法切换节点: 节点切换未启用或无可用节点")
            return False

        with self._switch_condition:
            while self._is_switching:
                self._switch_condition.wait()
            self._is_switching = True

        try:
            return self._perform_switch(max_retries)
        finally:
            with self._switch_condition:
                self._is_switching = False
                self._switch_condition.notify_all()

    def wait_for_switch_complete(self) -> None:
        """阻塞等待正在进行的切换完成（多线程下载场景用）。"""
        if not self.enable_node_switching:
            return
        with self._switch_condition:
            while self._is_switching:
                self._switch_condition.wait()

    def _get_verified_candidates(self) -> List[str]:
        """返回已验证节点在 available_nodes 中的 name 列表（去重保序）。"""
        available_names = {n["name"] for n in self.available_nodes}
        return [name for name in self.verified_nodes if name in available_names]

    def _order_by_strategy(self, names: List[str]) -> List[str]:
        """按负载均衡策略对候选节点排序。

        - round_robin: 保持顺序（调用方负责从当前节点之后轮转）
        - random:      随机打乱
        - least_403:   403 次数最少的优先
        - least_used:  下载次数最少的优先
        """
        if self.switch_strategy == "random":
            random.shuffle(names)
        elif self.switch_strategy == "least_403":
            names.sort(key=lambda n: self.node_403_counts.get(n, 0))
        elif self.switch_strategy == "least_used":
            names.sort(key=lambda n: self.node_download_counts.get(n, 0))
        return names

    def _apply_policy(self, names: List[str]) -> List[str]:
        """按优先度重排候选: 置顶在最前，其余按 (地区偏好, 倍率, 延迟) 排。

        没有配置 policy（未装扩展模块）时原样返回，行为与改造前一致。
        """
        if self.policy is None or not names:
            return names
        by_name = {n["name"]: n for n in self.available_nodes}
        pinned, rest = self.policy.apply(names, by_name)
        if pinned:
            logger.info("[🎯] 优先度置顶 %s 个: %s", len(pinned), ", ".join(pinned[:3]))
        return pinned + rest

    def _perform_switch(self, max_retries: Optional[int] = None) -> bool:
        """执行节点切换：候选排序 → 预检延迟 → PUT 切换 → 双重验证。"""
        if not self.available_nodes:
            logger.error("[❌] 无可用节点")
            return False

        if max_retries is None:
            max_retries = min(self.post_switch_max_retries, len(self.available_nodes))
        if max_retries <= 0:
            logger.error("[❌] 已尝试所有节点，切换失败")
            return False

        old_node = self.get_current_node()
        remaining = max_retries

        # 切换前先量一次出口 IP 作为基准（没有基准就没法判断"是否真的换了出口"）
        old_exit_ip = self.get_exit_ip() if self.verify_exit_ip else None
        if self.verify_exit_ip:
            logger.info("[🌐] 切换前出口 IP: %s", old_exit_ip or "查询失败")

        # 优先从已验证节点池中选
        verified_candidates = self._get_verified_candidates()
        if self.only_use_verified_nodes and verified_candidates:
            old_name = old_node["name"] if old_node else ""
            if self.switch_strategy == "round_robin" and old_name in verified_candidates:
                # 轮询: 从当前节点之后开始轮转
                idx = verified_candidates.index(old_name)
                verified_candidates = verified_candidates[idx + 1:] + verified_candidates[:idx]
            candidate_names = self._order_by_strategy(list(verified_candidates))
            mode = "verified_only"
        else:
            candidate_names = self._order_by_strategy([n["name"] for n in self.available_nodes])
            mode = "full_scan"

        if self.only_use_verified_nodes and not verified_candidates:
            logger.info("[🔍] 没有已验证节点，先扫描所有节点构建验证池...")
            mode = "build_pool"

        # ── 优先度: 置顶节点提到最前，且**绕过 round_robin 预旋转** ──
        # 预旋转发生在上面的 _order_by_strategy 之前，不在这里重排的话，
        # 被钉住的节点会被静默转走 —— 那正是"置顶看起来没生效"的原因。
        candidate_names = self._apply_policy(candidate_names)

        # 先用候选列表，再补全扫描
        all_names = [n["name"] for n in self.available_nodes]
        tried_names = set()

        for phase in (["candidates", "rest"] if mode != "build_pool" else ["build"]):
            if phase == "candidates":
                names = candidate_names
            elif phase == "rest":
                names = [n for n in all_names if n not in tried_names]
            else:  # build
                names = all_names

            for node_name in names:
                if node_name in tried_names:
                    continue
                tried_names.add(node_name)

                node = next((n for n in self.available_nodes if n["name"] == node_name), None)
                if not node:
                    continue
                if old_node and node_name == old_node["name"] and len(self.available_nodes) > 1:
                    continue

                logger.info("[🔍] 选择节点[%s]: %s", mode, node_name)
                logger.info("[🔄] 切换节点: %s -> %s", old_node['name'] if old_node else 'N/A', node_name)
                logger.info("[📊] 当前下载计数: %s, 已验证池: %s", self.download_count, len(self.verified_nodes))

                # 切换前预检: 候选节点 API 延迟必须可用
                if self.pre_switch_delay_check:
                    pre_delay = self.get_node_delay(node_name)
                    if pre_delay is None or pre_delay <= 0:
                        logger.info("[⏭️] 候选节点 Clash 延迟不可用，跳过: %s", node_name)
                        continue
                    logger.info("[✅] 候选节点延迟 OK: %s (%sms)", node_name, pre_delay)

                if self.set_node_by_name(node_name):
                    logger.info("[⏳] 等待节点切换完成...")
                    time.sleep(3)

                    # 验证1: API 确认策略组 now 指向目标节点
                    verify_success = False
                    for verify_attempt in range(3):
                        if self.verify_node_switch(node_name):
                            verify_success = True
                            break
                        if verify_attempt < 2:
                            logger.info("[⏳] 验证失败，等待后重试 (%s/3)...", verify_attempt + 2)
                            time.sleep(2)

                    # 验证2: 经代理出口真实访问外网
                    if verify_success and self.require_post_switch_connectivity:
                        conn_ok = False
                        for conn_attempt in range(self.post_switch_max_retries):
                            if self._post_switch_connectivity_ok():
                                conn_ok = True
                                break
                            if conn_attempt < self.post_switch_max_retries - 1:
                                logger.info("[⏳] 代理出口连通性检测失败，重试 (%s/%s)...",
                                            conn_attempt + 2, self.post_switch_max_retries)
                                time.sleep(0.8)
                        if not conn_ok:
                            logger.warning("[⚠️] 节点 %s 已选中但经代理无法访问外网，尝试下一候选", node_name)
                            self.verified_nodes.discard(node_name)
                            verify_success = False

                    # 验证3: 出口 IP 是否真的变了（唯一能识破"静默 no-op"的判据）
                    # 前两步都通过、出口却纹丝不动的情况真实存在:
                    #   ① rule 模式下 PUT 策略组对流量是 no-op
                    #   ② keep-alive 复用旧隧道
                    if verify_success and self.verify_exit_ip:
                        new_ip = self.get_exit_ip()
                        if new_ip is None:
                            logger.warning("[⚠️] 出口 IP 查询失败，跳过出口校验: %s", node_name)
                        elif old_exit_ip and new_ip == old_exit_ip and not self.allow_same_exit_ip:
                            logger.error("[❌] 出口 IP 未变化(%s) —— 切换实际未生效，尝试下一候选", new_ip)
                            logger.error("    常见原因: ① Clash 处于 rule 模式(流量走规则链, "
                                         "切策略组无效; 用 force_global_mode=True 或手动切 global)")
                            logger.error("    ② keep-alive 复用旧连接(需重新建连) "
                                         "③ 若确有多节点共用出口 IP, 设 allow_same_exit_ip=True")
                            self.verified_nodes.discard(node_name)
                            verify_success = False
                        else:
                            self._record_exit_ip(node_name, new_ip)
                            logger.info("[🌐] 出口 IP: %s -> %s", old_exit_ip or "?", new_ip)

                    if verify_success:
                        logger.info("[✅] 节点切换成功: %s", node_name)
                        self.verified_nodes.add(node_name)
                        try:
                            self.current_node_index = self.available_nodes.index(node)
                        except ValueError:
                            self.current_node_index = 0
                        self.download_count = 0
                        self.error_403_count = 0
                        self.save_state()
                        return True
                    remaining -= 1
                    logger.warning("[⚠️] 节点切换未通过，剩余重试: %s", remaining)
                else:
                    remaining -= 1
                    logger.error("[❌] set_node_by_name 失败，剩余重试: %s", remaining)

                if remaining <= 0:
                    break

            if remaining <= 0:
                break

        logger.error("[❌] 在 %s 次尝试后仍未切换成功", max_retries)
        return False

    def set_node_by_name(self, node_name: str) -> bool:
        """通过 Clash API 将策略组切换到指定节点。

        GLOBAL 是**合法且往往是唯一有效**的目标: global 模式下 GLOBAL 组即实际
        出口，其他组的切换对流量毫无影响。只有当 group_name 为空时才自动兜底。

        配了自定义来源时委托给它（静态来源的「切换」只是改游标，多实例聚合会
        路由到节点所属的那个实例）。
        """
        if self.custom_source is not None:
            spec = self._spec_of(node_name)
            if spec is None:
                logger.warning("[⚠️] 来源里没有节点 '%s'", node_name)
                return False
            return self.custom_source.activate(spec)
        try:
            # 仅当未配置策略组名时才自动查找兜底
            if not self.group_name:
                available_groups = self._find_available_groups()
                if available_groups:
                    self.group_name = available_groups[0]
                else:
                    logger.error("[❌] 无法找到可用的策略组")
                    return False

            response = self._api_request(
                "PUT", f"/proxies/{self.group_name}", json={"name": node_name}, timeout=10
            )

            if response.status_code == 204:
                return True

            logger.error("[❌] API调用失败: HTTP %s", response.status_code)
            if response.status_code == 400:
                logger.info("[💡] 提示: 节点名称可能不存在于策略组 '%s' 中", self.group_name)
            elif response.status_code == 404:
                logger.info("[💡] 提示: 策略组 '%s' 不存在，尝试查找可用策略组...", self.group_name)
                available_groups = self._find_available_groups()
                if available_groups:
                    self.group_name = available_groups[0]
                    return self.set_node_by_name(node_name)  # 递归重试一次
                logger.error("[❌] 未找到任何可用的策略组")
            return False

        except Exception as e:
            logger.error("[‼] 切换节点异常: %s", e)
            return False

    def verify_node_switch(self, expected_node_name: str) -> bool:
        """验证策略组当前选中节点是否为期望节点（最多3次）。"""
        max_verify_attempts = 3

        for attempt in range(max_verify_attempts):
            try:
                response = self._api_request("GET", f"/proxies/{self.group_name}", timeout=5)
                if response.status_code == 200:
                    current_node = (response.json() or {}).get("now", "")
                    if current_node == expected_node_name:
                        return True
                    if attempt < max_verify_attempts - 1:
                        time.sleep(1)
                        continue
                    logger.error("[❌] 节点切换验证失败: 期望 %s, 实际 %s", expected_node_name, current_node)
                    return False
                else:
                    logger.warning("[⚠️] 无法获取当前节点信息: HTTP %s", response.status_code)
                    if response.status_code == 404:
                        logger.info("[💡] 提示: 策略组 '%s' 不存在", self.group_name)
                    if attempt < max_verify_attempts - 1:
                        logger.info("[🔄] 重试验证 (%s/%s)...", attempt + 2, max_verify_attempts)
                        time.sleep(1)
                        continue
                    return False
            except Exception as e:
                if attempt < max_verify_attempts - 1:
                    logger.warning("[⚠️] 节点切换验证异常: %s，重试中...", e)
                    time.sleep(1)
                    continue
                logger.error("[❌] 节点切换验证异常: %s", e)
                return False

        return False

    def switch_to_healthy_node(self, record_now: bool = False) -> bool:
        """切换到健康节点（优先从已验证池中选；record_now=True 时把当前节点记入黑名单）。"""
        self.healthy_node_idx += 1
        while True:
            now_node = self.get_current_node()
            if now_node and isinstance(now_node, dict):
                if record_now:
                    self.error_node_set.add_to_set(now_node.get("name", ""))
                if not self._post_switch_connectivity_ok():
                    self.verified_nodes.discard(now_node.get("name", ""))
                    if now_node in self.available_nodes:
                        logger.info("[🔄] 当前节点不可用，移除再切换")
                        self.available_nodes.remove(now_node)
                    else:
                        self.refresh_nodes()
                        self.refresh_node_health()

            healthy_nodes = self.get_healthy_nodes()
            if len(healthy_nodes) < 5:
                logger.info("[🔍] 健康节点不足，刷新节点列表和健康状态...")
                self.refresh_nodes()
                self.refresh_node_health()
                healthy_nodes = self.get_healthy_nodes()

            # 优先从已验证池中选健康节点
            if self.only_use_verified_nodes and self.verified_nodes:
                verified_healthy = [n for n in healthy_nodes if n["name"] in self.verified_nodes]
                candidates = verified_healthy if verified_healthy else healthy_nodes
            else:
                candidates = healthy_nodes

            # ⚠️ 这是第二条并行的选择路径 —— 它不经过 _order_by_strategy，
            # 所以优先度（置顶/拉黑/地区/延迟）必须在这里单独套一遍，
            # 否则健康守护触发切换时，"钉住的节点"会失效。
            if self.policy is not None and candidates:
                by_name = {n["name"]: n for n in candidates}
                kept = [n for n in candidates if not self.policy.reject_reason(n)]
                pinned, rest = self.policy.split_pinned([n["name"] for n in kept])
                ordered = pinned + [n["name"] for n in
                                    sorted(rest, key=lambda x: self.policy.sort_key(x, by_name.get(x)))]
                candidates = [by_name[n] for n in ordered if n in by_name]
                if not candidates:                    # 规则把池子清空了 → 放宽
                    logger.warning("[⚠️] 优先度规则剔除了全部健康节点，本次忽略规则")
                    candidates = healthy_nodes

            if candidates:
                index = self.healthy_node_idx % len(candidates)
                healthy_node_name = candidates[index]["name"]
                logger.info("[🔄] 切换到健康节点: %s", healthy_node_name)
                self.set_node_by_name(healthy_node_name)
                time.sleep(2)
                if self._post_switch_connectivity_ok():
                    self.verified_nodes.add(healthy_node_name)
                    self.save_state()
                    return True
                self.verified_nodes.discard(healthy_node_name)
                self.healthy_node_idx += 1
                continue

            logger.warning("[⚠️] 没有可用的健康节点")
            # 全池熔断: 所有节点都在冷却 → 静默等待，别继续试探。
            # 实测若目标站处于整体限流（由聚合请求速率触发），继续试探只会
            # 延长它；静默 1-2 小时后自行恢复。
            if self.pool_exhausted_wait_sec > 0 and self.node_cooldowns:
                soonest = min(self.node_cooldowns.values())
                wait = min(max(soonest - time.time(), 0) or 0, self.pool_exhausted_wait_sec)
                if wait > 0:
                    logger.warning("[💤] 全部出口耗尽 → 静默 %.0f 分钟再试（pool_exhausted_wait_sec）",
                                   wait / 60)
                    time.sleep(wait)
            return False

    def start_auto_health_check(self, interval_sec: int = 30) -> None:
        """启动后台健康检查守护线程，出口不可用时自动切换节点（阻塞主线程）。"""

        def _check_loop():
            while True:
                time.sleep(interval_sec)
                if not self._post_switch_connectivity_ok():
                    logger.warning("[⚠️] 自动健康检查失败，切换节点")
                    self.switch_to_healthy_node()
                else:
                    logger.info("[✅] %s 测试通过", datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

        thread = threading.Thread(target=_check_loop, daemon=True)
        thread.start()
        logger.info("[🔁] 已启动自动健康检查，间隔 %s 秒", interval_sec)

        # 永久阻塞主线程
        threading.Event().wait()


def main():
    """最小使用示例：连接本机 Clash，列出节点并测试连通性。"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = {
        "api_url": os.environ.get("CLASH_API_URL", "http://127.0.0.1:9099"),
        "secret": os.environ.get("CLASH_SECRET", "your-secret-here"),
        "post_switch_test_url": os.environ.get("CLASH_TEST_URL", "https://www.gstatic.com/generate_204"),
        "group_name": os.environ.get("CLASH_GROUP_NAME", "🚀节点选择"),
        "proxy_port": int(os.environ.get("CLASH_PROXY_PORT", "7777")),
        "enable_node_switching": True,
        "max_downloads_per_node": 500,
        "switch_strategy": "round_robin",
        "show_node_info": False,
    }
    manager = ClashManager(config)
    manager.test_connection()


if __name__ == "__main__":
    main()

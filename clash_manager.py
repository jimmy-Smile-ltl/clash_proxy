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
        "post_switch_test_url": "https://example.com/",  # 切换后连通性检测 URL(必填)
        "group_name": "🚀节点选择",                    # 策略组名
        "proxy_port": 7777,                           # 本地代理端口
        "switch_strategy": "least_403",               # 负载均衡策略
    })

    # 请求目标站返回 403 时调用（累计 3 次自动切节点）:
    manager.increment_403_error()

    # 每次下载成功后调用（每节点满 500 次轮换）:
    manager.increment_download_count()
"""

import datetime
import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests

try:
    import redis  # type: ignore
except ImportError:  # redis 为可选依赖
    redis = None

logger = logging.getLogger("clash_manager")

# 允许作为候选节点的代理类型
_ALLOWED_NODE_TYPES = ["Shadowsocks", "Vmess", "Trojan", "Socks5", "Vless", "Http", "Snell", "Wireguard"]

# 订阅元数据特征（出现在节点名中的非节点条目）
_INVALID_NODE_PATTERNS = ("最新网址", "剩余流量", "过期时间", "订阅", "到期")

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


class ClashManager:
    """Clash/Mihomo 代理节点池管理器。

    核心设计（生产踩坑沉淀）:
    1. 双重验证切换 —— 切换前用 Clash API 测候选节点延迟（跳过明显不可用的，
       避免白等 set_node + sleep）；切换后必须经本地代理出口真实访问目标站
       （状态码 200/204）才确认成功。仅 API 返回 204 不代表节点真能上网。
    2. 已验证节点池 —— 只在上次验证通过的节点间轮换；池为空时才全量扫描构建。
    3. 错误黑名单按站点隔离 —— Redis key 含 post_switch_test_url 的域名，
       A 站封的 IP 不影响 B 站使用。
    4. 误杀保护 —— 错误节点数与总节点数接近时自动清空黑名单，避免死锁。
    5. 订阅元数据过滤 —— 节点名含「流量」「到期」等字样或嵌套策略组均被剔除，
       不能用单独的「流量」二字过滤，节点名常含「流量倍率」会误杀全部线路。
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

        # ---- 连通性检测 ----
        if not config.get("post_switch_test_url", ""):
            raise ValueError("配置错误: post_switch_test_url 参数不能为空 必须提供一个用于切换后连通性检测的URL")
        self.post_switch_test_url: str = config.get("post_switch_test_url", "https://www.gstatic.com/generate_204")
        self.post_switch_timeout_sec: int = config.get("post_switch_timeout_sec", 5)
        self.post_switch_max_retries: int = config.get("post_switch_max_retries", 3)
        self.pre_switch_delay_check: bool = config.get("pre_switch_delay_check", True)  # 切换前 API 测延迟
        self.require_post_switch_connectivity: bool = config.get("require_post_switch_connectivity", True)  # 切换后验出口

        # ---- 运行时状态 ----
        self.current_node_index: int = 0
        self.download_count: int = 0
        self.node_download_counts: Dict[str, int] = {}  # 各节点累计下载次数（负载均衡依据）
        self.node_403_counts: Dict[str, int] = {}       # 各节点累计 403 次数（least_403 依据）
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

        # ---- 启动: 恢复状态 + 拉取节点 ----
        self.load_state()
        if self.enable_node_switching:
            if self.refresh_nodes():
                self.refresh_node_health()

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
                    self.verified_nodes = set(state.get('verified_nodes', []))
                    logger.info("[📂] 加载节点状态: 索引=%s, 下载计数=%s, 已验证节点=%s",
                                self.current_node_index, self.download_count, len(self.verified_nodes))
            else:
                logger.info("[📂] 未找到节点状态文件，使用默认值")
        except Exception as e:
            logger.warning("[⚠️] 加载节点状态失败: %s", e)

    def save_state(self) -> None:
        """保存当前状态（临时文件 + rename 原子写入，防止写一半损坏）。"""
        try:
            state = {
                'current_node_index': self.current_node_index,
                'download_count': self.download_count,
                'node_download_counts': self.node_download_counts,
                'node_403_counts': self.node_403_counts,
                'verified_nodes': list(self.verified_nodes),
                'timestamp': time.time()
            }
            tmp_file = f"{self.state_file}.tmp"
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            os.replace(tmp_file, self.state_file)
            logger.info("[💾] 节点状态已保存: 索引=%s, 下载计数=%s", self.current_node_index, self.download_count)
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

    def refresh_nodes(self) -> bool:
        """从策略组拉取可用节点列表（过滤订阅元数据/嵌套策略组/错误黑名单）。"""
        try:
            error_nodes = self.error_node_set.get_set_members()

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
                    return False

            if response.status_code != 200:
                logger.warning("[⚠️] 获取节点列表失败: %s", response.status_code)
                if response.status_code == 401:
                    logger.info("[💡] 提示: 可能需要设置正确的API密钥 (secret)")
                elif response.status_code == 404:
                    logger.info("[💡] 提示: 策略组 '%s' 不存在，请检查Clash配置")
                return False

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

            all_nodes = data.get("all", [])

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
                    # 过滤订阅元数据（最新网址/剩余流量/到期等）
                    if any(p in str(node_name) for p in _INVALID_NODE_PATTERNS):
                        continue
                    # 过滤嵌套策略组
                    if all_proxies.get(node_name, {}).get("type") == "Selector":
                        continue
                    # 过滤不支持的节点类型（Unknown 放行）
                    if node_type not in _ALLOWED_NODE_TYPES and node_type != "Unknown":
                        continue
                    # 过滤错误黑名单节点
                    if node_name in error_nodes:
                        continue

                    self.available_nodes.append({
                        "name": node_name,
                        "type": node_type,
                        "delay": 0,
                        "healthy": True,
                        "last_check": time.time(),
                    })

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
        """获取当前代理配置（传给 requests 的 proxies 参数）。"""
        return {
            "http": f"http://127.0.0.1:{self.proxy_port}",
            "https": f"http://127.0.0.1:{self.proxy_port}",
        }

    def get_healthy_nodes(self) -> List[Dict]:
        """获取所有健康节点。"""
        return [n for n in self.available_nodes if n.get("healthy") and n.get("name") != "DIRECT"]

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
        }

    # ======================== 健康检测与延迟测试 ========================

    def get_node_delay(self, node_name: str) -> Optional[int]:
        """通过 Clash API 测试指定节点延迟(ms)，失败返回 None。"""
        try:
            import urllib.parse
            encoded_name = urllib.parse.quote(node_name)
            params = {"url": self.post_switch_test_url, "timeout": 5000}
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

        with ThreadPoolExecutor(max_workers=len(self.available_nodes)) as executor:
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

    def _post_switch_connectivity_ok(self) -> bool:
        """经本地代理出口真实访问目标站，返回是否可用（200/204 视为通过）。"""
        try:
            resp = requests.get(
                self.post_switch_test_url,
                proxies=self.get_proxy_config(),
                timeout=self.post_switch_timeout_sec,
            )
            return resp.status_code in (200, 204)
        except Exception:
            return False

    # ======================== 统计与触发切换 ========================

    def increment_download_count(self) -> bool:
        """下载成功时调用：累计计数，达到阈值自动轮换节点。"""
        self.download_count += 1
        current_node = self.get_current_node()
        if current_node:
            node_name = current_node["name"]
            self.node_download_counts[node_name] = self.node_download_counts.get(node_name, 0) + 1

        self.reset_403_error()  # 下载成功重置403计数
        self.save_state()

        if self.download_count >= self.max_downloads_per_node:
            return self.switch_node()
        return False

    def increment_403_error(self) -> bool:
        """目标站返回 403 时调用：累计计数（全局+按节点），达到阈值自动切换。"""
        self.error_403_count += 1
        current_node = self.get_current_node()
        if current_node:
            node_name = current_node["name"]
            self.node_403_counts[node_name] = self.node_403_counts.get(node_name, 0) + 1
        logger.warning("[⚠️] 403错误计数: %s/%s", self.error_403_count, self.max_403_errors)

        if self.error_403_count >= self.max_403_errors:
            logger.info("[🔄] 达到最大403错误数，切换节点")
            self.error_403_count = 0
            return self.switch_node()
        return False

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
        """通过 Clash API 将策略组切换到指定节点。"""
        try:
            # group_name 不能是 GLOBAL，自动查找可用策略组兜底
            if not self.group_name or self.group_name.upper() == "GLOBAL":
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

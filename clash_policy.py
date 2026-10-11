# -*- coding: utf-8 -*-
"""clash_policy.py — 节点优先度管理（置顶 / 拉黑 / 地区偏好 / 延迟阈值 / 倍率）

`clash_manager.py` 的四种 `switch_strategy` 决定「怎么在候选里排」，本模块决定
「哪些节点根本不该进候选，哪些必须排在最前」。

定位（使用场景）
----------------
负载均衡策略解决的是「摊流量」，但真实采集里你往往有更强的偏好：

    - 这条线路刚被目标站封过 → **拉黑**，别再选中它浪费时间
    - 找到两个能用的节点 → **钉住**，别让轮换把它们转走
    - 订阅里 30 个节点，只有日本和新加坡的线路能用 → **只留这两个地区**
    - 某几个节点延迟 2000ms+ → **设个阈值**直接不要
    - 有的节点是 0.1 倍率、有的是 3 倍率 → **优先用便宜的**

依赖
----
无（纯标准库）。

用法示例
--------
::

    from clash_policy import NodePolicy

    policy = NodePolicy({
        "pin_nodes": ["🇸🇬 新加坡-示例01"],
        "block_nodes": ["🇭🇰 香港01"],
        "prefer_regions": ["新加坡", "日本"],
        "exclude_regions": ["香港"],
        "max_latency_ms": 800,
        "prefer_low_multiplier": True,
    })

    names = policy.filter_names(all_names, nodes_by_name)   # 剔除
    pinned, rest = policy.split_pinned(names)               # 置顶
    # rest 交给 ClashManager 的 switch_strategy 排序，最后拼回 pinned + rest

⚠️ 设计要点（都是踩过的坑）
--------------------------
1. **置顶必须绕过 round-robin 预旋转** —— `ClashManager._perform_switch()` 在排序前
   会把候选列表转一圈，置顶节点会被静默转走。
2. **必须同时覆盖 `switch_to_healthy_node()`** —— 那是第二条并行选择路径，
   完全不经过 `_order_by_strategy`。
3. **持久化不能混进 `state_file`** —— 主库的 `save_state()` 用单调 `max()` 合并
   （对计数器正确），用在置顶标志上会导致**跨进程永远取消不掉置顶**。
   所以置顶/拉黑单独存一个文件，最后写入者生效。
"""

import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("clash_policy")

DEFAULT_PRIORITY_FILE = ".clash_priority.json"

# 地区名 → 规范名。节点名里出现任意一个即归到该地区。
# 刻意收录同义词与常见英文缩写 —— 机场节点名的写法极不统一。
_REGION_KEYWORDS = (
    ("香港", ("香港", "hongkong", "hong kong", "hk", "hkg")),
    ("台湾", ("台湾", "台灣", "taiwan", "tw", "tpe")),
    ("日本", ("日本", "japan", "jp", "tokyo", "osaka", "东京", "大阪")),
    ("新加坡", ("新加坡", "狮城", "singapore", "sg", "sin")),
    ("韩国", ("韩国", "韓國", "korea", "kr", "seoul", "首尔")),
    ("美国", ("美国", "美國", "united states", "usa", "us", "los angeles",
              "san jose", "seattle", "洛杉矶", "圣何塞", "西雅图", "硅谷")),
    ("英国", ("英国", "英國", "united kingdom", "uk", "gb", "london", "伦敦")),
    ("德国", ("德国", "德國", "germany", "de", "frankfurt", "法兰克福")),
    ("法国", ("法国", "法國", "france", "fr", "paris", "巴黎")),
    ("荷兰", ("荷兰", "荷蘭", "netherlands", "nl", "amsterdam", "阿姆斯特丹")),
    ("俄罗斯", ("俄罗斯", "俄羅斯", "russia", "ru", "moscow", "vladivostok")),
    ("澳大利亚", ("澳大利亚", "澳洲", "australia", "au", "sydney", "悉尼")),
    ("加拿大", ("加拿大", "canada", "ca", "toronto", "多伦多")),
    ("马来西亚", ("马来西亚", "馬來西亞", "malaysia", "my", "kuala")),
    ("土耳其", ("土耳其", "turkey", "tr", "istanbul")),
    ("印度", ("印度", "india", "in", "mumbai")),
    ("越南", ("越南", "vietnam", "vn")),
    ("泰国", ("泰国", "泰國", "thailand", "th")),
    ("菲律宾", ("菲律宾", "philippines", "ph")),
    ("巴西", ("巴西", "brazil", "br", "sao paulo")),
    ("阿根廷", ("阿根廷", "argentina", "ar")),
    ("南非", ("南非", "south africa", "za")),
    ("乌克兰", ("乌克兰", "ukraine", "ua")),
    ("波兰", ("波兰", "poland", "pl")),
    ("瑞典", ("瑞典", "sweden", "se")),
    ("瑞士", ("瑞士", "switzerland", "ch", "zurich")),
    ("西班牙", ("西班牙", "spain", "es", "madrid")),
    ("意大利", ("意大利", "italy", "it", "milan")),
    ("爱尔兰", ("爱尔兰", "ireland", "ie", "dublin")),
    ("芬兰", ("芬兰", "finland", "fi")),
    ("挪威", ("挪威", "norway", "no", "oslo")),
    ("丹麦", ("丹麦", "denmark", "dk")),
    ("奥地利", ("奥地利", "austria", "at")),
    ("比利时", ("比利时", "belgium", "be")),
    ("捷克", ("捷克", "czech", "cz")),
    ("罗马尼亚", ("罗马尼亚", "romania", "ro")),
    ("立陶宛", ("立陶宛", "lithuania", "lt")),
    ("爱沙尼亚", ("爱沙尼亚", "estonia", "ee")),
    ("拉脱维亚", ("拉脱维亚", "latvia", "lv")),
)

# 国旗 emoji → 地区（节点名通常以国旗开头，是最可靠的信号）
_FLAG_REGIONS = {
    "🇭🇰": "香港", "🇹🇼": "台湾", "🇯🇵": "日本", "🇸🇬": "新加坡", "🇰🇷": "韩国",
    "🇺🇸": "美国", "🇺🇲": "美国", "🇬🇧": "英国", "🇩🇪": "德国", "🇫🇷": "法国",
    "🇳🇱": "荷兰", "🇷🇺": "俄罗斯", "🇦🇺": "澳大利亚", "🇨🇦": "加拿大",
    "🇲🇾": "马来西亚", "🇹🇷": "土耳其", "🇮🇳": "印度", "🇻🇳": "越南",
    "🇹🇭": "泰国", "🇵🇭": "菲律宾", "🇧🇷": "巴西", "🇦🇷": "阿根廷",
    "🇿🇦": "南非", "🇺🇦": "乌克兰", "🇵🇱": "波兰", "🇸🇪": "瑞典",
    "🇨🇭": "瑞士", "🇪🇸": "西班牙", "🇮🇹": "意大利", "🇮🇪": "爱尔兰",
    "🇫🇮": "芬兰", "🇳🇴": "挪威", "🇩🇰": "丹麦", "🇦🇹": "奥地利",
    "🇧🇪": "比利时", "🇨🇿": "捷克", "🇷🇴": "罗马尼亚", "🇱🇹": "立陶宛",
    "🇪🇪": "爱沙尼亚", "🇱🇻": "拉脱维亚",
}

# 倍率: "0.1倍" / "x0.1" / "0.5x" / "0.01 倍"
_MULTIPLIER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:倍|[xX](?![a-zA-Z]))")


def region_of(node_name: str) -> str:
    """从节点名解析地区，解析不出返回空串。

    优先级: 国旗 emoji > 中文/英文关键词。
    国旗最可靠 —— 关键词表里 `in`(印度) 这类两字母缩写太容易误命中。
    """
    if not node_name:
        return ""
    for flag, region in _FLAG_REGIONS.items():
        if flag in node_name:
            return region
    low = node_name.lower()
    for region, keywords in _REGION_KEYWORDS:
        for kw in keywords:
            if len(kw) <= 2:                     # 两字母缩写要求按词边界匹配
                if re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(kw), low):
                    return region
            elif kw in low:
                return region
    return ""


def multiplier_of(node_name: str) -> float:
    """从节点名解析流量倍率（如「0.1倍」→ 0.1），解析不出返回 1.0。"""
    if not node_name:
        return 1.0
    m = _MULTIPLIER_RE.search(node_name)
    if not m:
        return 1.0
    try:
        return float(m.group(1))
    except ValueError:
        return 1.0


class NodePolicy:
    """节点优先度策略。

    :param pin_nodes: 置顶节点名（永不参与轮换，除非被拉黑或不可用）
    :param block_nodes: 拉黑节点名（直接不进候选）
    :param prefer_regions: 偏好地区，命中的排在其他同分节点前
    :param exclude_regions: 排除地区
    :param max_latency_ms: 延迟上限，超过的不进候选（延迟为 0/None 视为未知，放行）
    :param prefer_low_multiplier: 按流量倍率从低到高排序
    :param priority_file: 置顶/拉黑的持久化文件
    :param auto_save: 增删置顶/拉黑时自动落盘
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        self.priority_file: str = cfg.get("priority_file", DEFAULT_PRIORITY_FILE)
        self.prefer_regions: List[str] = list(cfg.get("prefer_regions", []) or [])
        self.exclude_regions: List[str] = list(cfg.get("exclude_regions", []) or [])
        self.max_latency_ms: Optional[int] = cfg.get("max_latency_ms") or None
        self.prefer_low_multiplier: bool = bool(cfg.get("prefer_low_multiplier", False))
        self.auto_save: bool = bool(cfg.get("auto_save", True))

        self.pin_nodes: List[str] = list(cfg.get("pin_nodes", []) or [])
        self.block_nodes: List[str] = list(cfg.get("block_nodes", []) or [])
        self._lock = threading.Lock()

        # 配置文件里的置顶/拉黑优先于持久化文件（配置是显式意图）
        if not self.pin_nodes and not self.block_nodes:
            self.load()

    # ---------------------------------------------------------------- 持久化

    def load(self) -> None:
        """从持久化文件恢复置顶/拉黑。

        ⚠️ 这里刻意**不做合并** —— 置顶/拉黑是「最后意图生效」，不是计数器。
        主库的 state_file 用 max() 合并，标志位塞进去会导致取消不掉。
        """
        try:
            if not self.priority_file or not os.path.exists(self.priority_file):
                return
            with open(self.priority_file, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            self.pin_nodes = list(data.get("pin", []) or [])
            self.block_nodes = list(data.get("block", []) or [])
            logger.info("[📂] 已加载优先度: 置顶 %s 个, 拉黑 %s 个",
                        len(self.pin_nodes), len(self.block_nodes))
        except Exception as e:
            logger.warning("[⚠️] 加载优先度文件失败: %s", e)

    def save(self) -> None:
        """落盘（最后写入者生效，不合并）。"""
        if not self.priority_file:
            return
        try:
            with self._lock:
                data = {"pin": self.pin_nodes, "block": self.block_nodes}
                tmp = "%s.tmp" % self.priority_file
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.priority_file)
        except Exception as e:
            logger.warning("[⚠️] 保存优先度文件失败: %s", e)

    # ---------------------------------------------------------------- 增删

    def pin(self, name: str) -> None:
        if name and name not in self.pin_nodes:
            self.pin_nodes.append(name)
            self.block_nodes = [n for n in self.block_nodes if n != name]
            logger.info("[📌] 已置顶: %s", name)
            if self.auto_save:
                self.save()

    def unpin(self, name: str) -> None:
        if name in self.pin_nodes:
            self.pin_nodes.remove(name)
            logger.info("[📍] 已取消置顶: %s", name)
            if self.auto_save:
                self.save()

    def block(self, name: str) -> None:
        if name and name not in self.block_nodes:
            self.block_nodes.append(name)
            self.pin_nodes = [n for n in self.pin_nodes if n != name]
            logger.info("[🚫] 已拉黑: %s", name)
            if self.auto_save:
                self.save()

    def unblock(self, name: str) -> None:
        if name in self.block_nodes:
            self.block_nodes.remove(name)
            logger.info("[♻️] 已解除拉黑: %s", name)
            if self.auto_save:
                self.save()

    # ---------------------------------------------------------------- 判定

    def reject_reason(self, node: Dict) -> Optional[str]:
        """节点是否该被剔除；返回中文原因，None = 保留。"""
        name = node.get("name", "")
        if name in self.block_nodes:
            return "已拉黑"
        region = node.get("region") or region_of(name)
        if region and region in self.exclude_regions:
            return "排除地区(%s)" % region
        if self.max_latency_ms:
            delay = node.get("delay") or 0
            if delay and delay > self.max_latency_ms:
                return "延迟 %sms 超过 %sms" % (delay, self.max_latency_ms)
        return None

    def filter_names(self, names: Sequence[str],
                     nodes_by_name: Optional[Dict[str, Dict]] = None) -> List[str]:
        """剔除拉黑 / 排除地区 / 超延迟的节点（保持原顺序）。"""
        nodes_by_name = nodes_by_name or {}
        out: List[str] = []
        for n in names:
            node = nodes_by_name.get(n, {"name": n})
            reason = self.reject_reason(node)
            if reason:
                logger.debug("[⏭️] 优先度剔除 %s: %s", n, reason)
                continue
            out.append(n)
        return out

    def split_pinned(self, names: Sequence[str]) -> Tuple[List[str], List[str]]:
        """拆成 (置顶, 其余)。置顶保持 policy 里的顺序。

        ⚠️ 调用方必须让置顶**绕过 round-robin 预旋转**，否则会被静默转走。
        """
        pinned = [n for n in self.pin_nodes if n in set(names)]
        rest = [n for n in names if n not in set(pinned)]
        return pinned, rest

    def sort_key(self, name: str, node: Optional[Dict] = None) -> Tuple:
        """同层候选的排序键（越小越优先）。

        地区偏好 > 倍率 > 延迟。没开启 `prefer_low_multiplier` 时倍率恒为 1.0，
        即退化为「按延迟排」。
        """
        node = node or {}
        region = node.get("region") or region_of(name)
        prefer = 0 if (self.prefer_regions and region in self.prefer_regions) else 1
        mult = multiplier_of(name) if self.prefer_low_multiplier else 1.0
        delay = node.get("delay") or 10 ** 6          # 未知延迟排最后
        return (prefer, mult, delay)

    def apply(self, names: Sequence[str],
              nodes_by_name: Optional[Dict[str, Dict]] = None
              ) -> Tuple[List[str], List[str]]:
        """一步到位: 过滤 → 拆置顶。返回 (pinned, rest)。"""
        nodes_by_name = nodes_by_name or {}
        kept = self.filter_names(names, nodes_by_name)
        pinned, rest = self.split_pinned(kept)
        rest = sorted(rest, key=lambda n: self.sort_key(n, nodes_by_name.get(n)))
        return pinned, rest

    def describe(self, names: Sequence[str]) -> str:
        """给 CLI 用的一行摘要。"""
        parts = []
        if self.pin_nodes:
            parts.append("置顶 %s" % len([n for n in self.pin_nodes if n in set(names)]))
        if self.block_nodes:
            parts.append("拉黑 %s" % len([n for n in self.block_nodes if n in set(names)]))
        if self.prefer_regions:
            parts.append("偏好 " + "/".join(self.prefer_regions))
        if self.exclude_regions:
            parts.append("排除 " + "/".join(self.exclude_regions))
        if self.max_latency_ms:
            parts.append("延迟≤%sms" % self.max_latency_ms)
        if self.prefer_low_multiplier:
            parts.append("低倍率优先")
        return " · ".join(parts) if parts else "（未设置任何优先度规则）"

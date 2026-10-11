# -*- coding: utf-8 -*-
"""sources_test.py — 离线单测（不联网、不需要 Clash、可直接进 CI）

覆盖 2026-10 新增的全部扩展能力：
    clash_sources.py   静态列表解析 / 来源抽象 / 多实例聚合
    clash_policy.py    地区解析 / 倍率解析 / 置顶 / 拉黑 / 排序 / 独立持久化
    clash_probe.py     目标站判据（业务码优先于 HTTP 状态码）
    clash_clients.py   状态码判定 / 节点变化检测 / 自动上报
    clash_cli.py       --probe 参数解析

用法:
    python tests/sources_test.py
"""

import os
import pathlib
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from clash_policy import NodePolicy, multiplier_of, region_of      # noqa: E402
from clash_probe import BLOCKED, DEAD, OK, SiteProbe               # noqa: E402
from clash_sources import (ClashSelectorSource, MultiClashSource,  # noqa: E402
                           StaticListSource, parse_proxy_line)

_ok = []


def check(name, cond, detail=""):
    _ok.append(bool(cond))
    print(f"{'✅' if cond else '❌'} {name} {detail}", flush=True)


# ---------------------------------------------------------------- 行解析

def test_parse_proxy_line():
    print("\n=== 代理行解析 ===")
    cases = [
        ("http://u:p@1.2.3.4:8080", "http://u:p@1.2.3.4:8080", "带 scheme 原样保留"),
        ("socks5://5.6.7.8:1080", "socks5://5.6.7.8:1080", "socks5 保留"),
        ("9.9.9.9:3128:user:pass", "http://user:pass@9.9.9.9:3128", "IP:端口:账号:密码"),
        ("1.1.1.1:1080", "http://1.1.1.1:1080", "IP:端口"),
        ("user:pw@2.2.2.2:8888", "http://user:pw@2.2.2.2:8888", "账号@主机:端口"),
        ("# 这是注释", None, "注释行忽略"),
        ("随便写点什么", None, "无法识别返回 None"),
        ("", None, "空行忽略"),
    ]
    for raw, want, why in cases:
        got = parse_proxy_line(raw)
        check(f"{why}: {raw[:26]!r}", got == want, f"-> {got!r}")


# ---------------------------------------------------------------- 地区 / 倍率

def test_region_and_multiplier():
    print("\n=== 地区 / 倍率解析（用真实节点名）===")
    regions = [
        ("🇸🇬 新加坡-示例01", "新加坡"),
        ("🇯🇵 日本-示例01", "日本"),
        ("🇺🇲 美国-示例01", "美国"),          # 🇺🇲 是美属外岛旗，也要认
        ("🇦🇺 Sydney-2x-示例", "澳大利亚"),   # 只有英文城市词，靠关键词命中
        ("🇭🇰 香港-示例01", "香港"),
        ("SomeNode-Unknown", ""),
    ]
    for name, want in regions:
        got = region_of(name)
        check(f"地区 {name[:30]}", got == want, f"-> {got!r}")

    mults = [
        ("🇦🇺 Sydney-2x-示例", 2.0),
        ("🇯🇵 Tokyo-01-0.1倍", 0.1),
        ("普通节点", 1.0),                        # 解析不出默认 1.0
    ]
    for name, want in mults:
        got = multiplier_of(name)
        check(f"倍率 {name[:30]}", got == want, f"-> {got}")


# ---------------------------------------------------------------- 优先度

def test_policy():
    print("\n=== 节点优先度 ===")
    names = ["🇭🇰 香港01", "🇯🇵 日本01", "🇸🇬 新加坡01", "🇺🇸 美国-示例02-0.1倍"]
    by = {n: {"name": n, "delay": 0} for n in names}

    p = NodePolicy({"exclude_regions": ["香港"], "auto_save": False})
    kept = p.filter_names(names, by)
    check("排除地区生效", "🇭🇰 香港01" not in kept, f"-> 剩 {len(kept)} 个")
    check("其他地区保留", len(kept) == 3)

    p = NodePolicy({"block_nodes": ["🇺🇸 美国-示例02-0.1倍"], "auto_save": False})
    check("拉黑生效", "🇺🇸 美国-示例02-0.1倍" not in p.filter_names(names, by))

    p = NodePolicy({"pin_nodes": ["🇺🇸 美国-示例02-0.1倍"], "auto_save": False})
    pinned, rest = p.split_pinned(names)
    check("置顶被拆出来", pinned == ["🇺🇸 美国-示例02-0.1倍"], f"-> pinned={pinned}")
    check("置顶不在 rest 里", "🇺🇸 美国-示例02-0.1倍" not in rest)

    # 延迟阈值: 已知延迟超限的剔除，未知延迟(0)放行
    p = NodePolicy({"max_latency_ms": 500, "auto_save": False})
    by2 = {"慢节点": {"name": "慢节点", "delay": 900},
           "快节点": {"name": "快节点", "delay": 120},
           "未知节点": {"name": "未知节点", "delay": 0}}
    kept = p.filter_names(list(by2), by2)
    check("超延迟剔除", "慢节点" not in kept)
    check("未知延迟放行", "未知节点" in kept and "快节点" in kept)

    # 偏好地区 + 低倍率排序
    p = NodePolicy({"prefer_regions": ["新加坡"], "prefer_low_multiplier": True,
                    "auto_save": False})
    order = sorted(names, key=lambda n: p.sort_key(n, {"delay": 100}))
    check("偏好地区排最前", order[0] == "🇸🇬 新加坡01", f"-> {order}")

    # 持久化: 独立文件，最后写入者生效（不能像 state_file 那样 max() 合并）
    tmp = os.path.join(tempfile.gettempdir(), "cm_policy_test.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    p1 = NodePolicy({"priority_file": tmp})
    p1.pin("节点A")
    p2 = NodePolicy({"priority_file": tmp})       # 另一个"进程"
    check("置顶已持久化", p2.pin_nodes == ["节点A"], f"-> {p2.pin_nodes}")
    p2.unpin("节点A")                             # 取消必须能生效
    p3 = NodePolicy({"priority_file": tmp})
    check("取消置顶能生效（max() 合并做不到这点）", p3.pin_nodes == [], f"-> {p3.pin_nodes}")
    os.remove(tmp)


# ---------------------------------------------------------------- 站点判据

def test_site_probe():
    print("\n=== 目标站判据 ===")
    # 场景A: 某风控站 —— 空体 403 = 节点正常；业务码 50000 = IP 被封
    a = SiteProbe("https://api.example.com/gen", method="POST", body={},
                  body_code_field="code", body_blocked_codes=(50000,))
    for status, body, want, why in [
        (403, '{"code": 403, "msg": "数据异常"}', OK, "空体403=节点正常"),
        (200, '{"code": 50000}', BLOCKED, "业务码50000=被封"),
        (200, '{"code": 0, "data": {}}', OK, "正常返回"),
        (502, "", DEAD, "网关错误=链路不通"),
    ]:
        got = a.classify(status, body)
        check(f"A {why}", got == want, f"-> {got}")

    # 场景B: 普通站 —— HTTP 403/429 就是被封
    b = SiteProbe("https://plain.example.com/", blocked_status=(403, 429))
    for status, want in [(200, OK), (403, BLOCKED), (429, BLOCKED), (404, OK)]:
        got = b.classify(status, "")
        check(f"B HTTP {status}", got == want, f"-> {got}")


# ---------------------------------------------------------------- 来源

def test_static_source():
    print("\n=== 静态来源 ===")
    src = StaticListSource(lines=[
        "# 我的代理池",
        "http://u:p@1.2.3.4:8080",
        "socks5://5.6.7.8:1080",
        "9.9.9.9:3128:user:pass",
        "1.1.1.1:1080",
        "http://u:p@1.2.3.4:8080",        # 重复行应被去重
        "垃圾行",
    ])
    nodes = src.list_nodes()
    check("解析出 4 个出口（去重 + 跳过垃圾）", len(nodes) == 4, f"-> {len(nodes)}")
    check("每个节点自带 proxy 地址",
          all(n.proxy for n in nodes), f"-> {nodes[0].proxy if nodes else None}")
    check("kind 标记为 static", all(n.kind == "static" for n in nodes))
    check("activate 恒成功（静态源无切换概念）", src.activate(nodes[0]))

    # 多 Clash 实例聚合: 同名节点要加实例后缀，否则互相覆盖
    multi = MultiClashSource([
        ClashSelectorSource("http://127.0.0.1:9097", "a", "G1"),
        ClashSelectorSource("http://127.0.0.1:9091", "b", "G2"),
    ])
    check("多实例来源可构造", multi.describe().startswith("聚合 2 个"))


def test_manager_with_static_source():
    print("\n=== ClashManager + 静态来源（端到端，不联网）===")
    from clash_manager import ClashManager
    tmp = os.path.join(tempfile.gettempdir(), "cm_static_test.json")
    for f in (tmp, tmp + ".lock", tmp + ".priority.json"):
        if os.path.exists(f):
            os.remove(f)
    m = ClashManager({
        "post_switch_test_url": "https://example.com/",
        "enable_node_switching": False,
        "state_file": tmp,
        "policy": {"priority_file": tmp + ".priority.json"},
        "source": {"type": "static", "lines": [
            "http://u:p@1.2.3.4:8080", "socks5://5.6.7.8:1080"]},
    })
    m.refresh_nodes()
    check("静态来源节点进入池子", len(m.available_nodes) == 2, f"-> {len(m.available_nodes)}")
    first = m.available_nodes[0]["name"]
    m.set_node_by_name(first)
    cfg = m.get_proxy_config()
    check("get_proxy_config 返回节点自己的地址（不经 Clash）",
          cfg["https"] == "http://u:p@1.2.3.4:8080", f"-> {cfg['https']}")
    for f in (tmp, tmp + ".lock", tmp + ".priority.json"):
        if os.path.exists(f):
            os.remove(f)


def test_clients():
    print("\n=== 客户端适配层 ===")
    from clash_clients import (_ProxyFollower, classify_status,   # noqa: E402
                               session as managed_session)

    for st, want in [(200, "ok"), (204, "ok"), (301, "ok"),
                     (403, "blocked"), (429, "blocked"), (404, None)]:
        got = classify_status(st)
        check(f"状态码 {st}", got == want, f"-> {got}")

    class _FakeMgr:
        def __init__(self):
            self.node = "A"
            self.proxy = "http://127.0.0.1:1"
            self.reports = []

        def get_current_node(self):
            return {"name": self.node}

        def get_proxy_config(self):
            return {"http": self.proxy, "https": self.proxy}

        def report_verdict(self, v):
            self.reports.append(v)

    mgr = _FakeMgr()
    f = _ProxyFollower(mgr)
    check("初始代理已带上", f.proxies["https"] == "http://127.0.0.1:1")
    check("节点没变 → 不重建", f.sync() is False)
    mgr.node, mgr.proxy = "B", "http://127.0.0.1:2"
    check("节点变了 → 重建并换代理",
          f.sync() is True and f.proxies["https"] == "http://127.0.0.1:2")

    mgr2 = _FakeMgr()
    s = managed_session(mgr2)
    check("requests 适配器 trust_env=False（不受系统代理干扰）", s.trust_env is False)
    check("requests 适配器已带代理", s.proxies["https"] == "http://127.0.0.1:1")
    s.report(200)
    s.report(403)
    s.report(None, exc=TimeoutError())
    check("自动上报 ok/blocked/dead", mgr2.reports == ["ok", "blocked", "dead"],
          f"-> {mgr2.reports}")
    s.close()


def test_probe_spec():
    """`clash_cli.py --probe` 的参数解析。

    这里曾经有个真 bug: 用 partition(":") 切方法前缀，结果 `https://x/` 的第一个
    冒号在 scheme 里，`https` 被当成方法名、URL 变成 `//x/`（MissingSchema）。
    """
    print("\n=== --probe 参数解析 ===")
    from clash_cli import parse_probe_spec          # noqa: E402

    kw = parse_probe_spec("https://example.com/")
    check("纯 URL → GET，scheme 不被当方法名",
          kw["method"] == "GET" and kw["url"] == "https://example.com/",
          f"-> {kw}")

    kw = parse_probe_spec("POST:https://api.x.com/gen")
    check("POST 前缀被识别", kw["method"] == "POST" and kw["url"] == "https://api.x.com/gen")

    kw = parse_probe_spec("POST:https://api.x.com/gen|code=50000")
    check("业务码判据被解析",
          kw.get("body_code_field") == "code" and kw.get("body_blocked_codes") == ["50000"],
          f"-> {kw}")


def main():
    test_parse_proxy_line()
    test_region_and_multiplier()
    test_policy()
    test_site_probe()
    test_static_source()
    test_manager_with_static_source()
    test_clients()
    test_probe_spec()
    passed = sum(_ok)
    print(f"\n===== {passed}/{len(_ok)} 项通过 =====")
    sys.exit(0 if passed == len(_ok) else 1)


if __name__ == "__main__":
    main()

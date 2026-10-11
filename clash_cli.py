#!/usr/bin/env python3
"""clash-cli — Clash 代理池节点管理命令行工具（基于 ClashManager）。

用法:
  python clash_cli.py list                 列出所有节点及健康状态
  python clash_cli.py switch               切换到下一节点（按当前策略）
  python clash_cli.py switch --random      随机切换节点
  python clash_cli.py switch --strategy least_403   按最少403策略切换
  python clash_cli.py status               查看当前状态
  python clash_cli.py stats                查看各节点负载统计（下载/403次数）
  python clash_cli.py health               对所有节点测延迟
  python clash_cli.py test                 测试 API 与代理出口连通性
  python clash_cli.py monitor [--interval 30]   后台健康检查守护

环境变量（或写入项目根目录 .env 文件）:
  CLASH_SECRET        Clash API 密钥（与 config.yaml 的 secret 一致）
  CLASH_API_URL       默认 http://127.0.0.1:9099
  CLASH_PROXY_PORT    默认 7777
  CLASH_TEST_URL      切换后连通性检测 URL，默认 https://www.gstatic.com/generate_204
  CLASH_GROUP_NAME    策略组名，默认 🚀节点选择
"""

import argparse
import logging
import os
import sys
from typing import List

from clash_manager import ClashManager, SWITCH_STRATEGIES

try:                                   # 可选的扩展模块（体检 / 站点探针）
    from clash_probe import SiteProbe
except ImportError:                    # 只装核心也能用，netcheck 会提示
    SiteProbe = None


def load_dotenv(path: str = ".env") -> None:
    """极简 .env 加载（无第三方依赖，仅解析 KEY=VALUE 行）。"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔型环境变量（1/true/yes/on 视为真）。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


def _env_list(name: str) -> List[str]:
    """读取逗号分隔的列表型环境变量（自动去空白与空项）。"""
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def get_config():
    cfg = {
        "api_url": os.environ.get("CLASH_API_URL", "http://127.0.0.1:9099"),
        "secret": os.environ.get("CLASH_SECRET", "your-secret-here"),
        "post_switch_test_url": os.environ.get("CLASH_TEST_URL", "https://www.gstatic.com/generate_204"),
        "group_name": os.environ.get("CLASH_GROUP_NAME", "🚀节点选择"),
        "proxy_port": int(os.environ.get("CLASH_PROXY_PORT", "7777")),
        "enable_node_switching": True,
        "max_downloads_per_node": 500,
        "switch_strategy": os.environ.get("CLASH_SWITCH_STRATEGY", "round_robin"),
        "show_node_info": False,
        "pre_switch_delay_check": True,
        "require_post_switch_connectivity": True,
        "only_use_verified_nodes": True,
        # 出口校验: 切换前后比对真实出口 IP（识破 rule 模式 / keep-alive 造成的静默 no-op）
        "verify_exit_ip": _env_bool("CLASH_VERIFY_EXIT_IP", True),
        "exit_ip_url": os.environ.get("CLASH_EXIT_IP_URL", "https://api.ipify.org"),
        "allow_same_exit_ip": _env_bool("CLASH_ALLOW_SAME_EXIT_IP", False),
        "exit_ip_log_file": os.environ.get("CLASH_EXIT_IP_LOG", ""),
        "force_global_mode": _env_bool("CLASH_FORCE_GLOBAL_MODE", False),
        # 全池熔断: 所有出口都冷却时的静默等待（别调小，见 README）
        "pool_exhausted_wait_sec": int(os.environ.get("CLASH_POOL_EXHAUSTED_WAIT", "3600")),
        # 节点优先度（可选扩展 clash_policy.py）
        "policy": {
            "pin_nodes": _env_list("CLASH_PIN_NODES"),
            "block_nodes": _env_list("CLASH_BLOCK_NODES"),
            "prefer_regions": _env_list("CLASH_PREFER_REGIONS"),
            "exclude_regions": _env_list("CLASH_EXCLUDE_REGIONS"),
            "prefer_low_multiplier": _env_bool("CLASH_PREFER_LOW_MULTIPLIER", False),
            "priority_file": os.environ.get("CLASH_PRIORITY_FILE", ".clash_priority.json"),
        },
    }
    # 设了 CLASH_MAX_LATENCY_MS 才启用延迟阈值（0/空 = 不限制）
    raw_lat = os.environ.get("CLASH_MAX_LATENCY_MS", "").strip()
    if raw_lat.isdigit() and int(raw_lat) > 0:
        cfg["policy"]["max_latency_ms"] = int(raw_lat)
    # 设了 CLASH_STATIC_PROXIES 就改用静态代理列表（不经过 Clash）
    if os.environ.get("CLASH_STATIC_PROXIES"):
        cfg["source"] = {"type": "static", "file": os.environ["CLASH_STATIC_PROXIES"]}
    return cfg


def cmd_list(manager: ClashManager):
    manager.refresh_nodes()
    manager.refresh_node_health()
    current = manager.get_current_node()
    current_name = current["name"] if current else "N/A"

    print(f"策略组: {manager.group_name}")
    print(f"当前节点: {current_name}")
    print(f"切换策略: {manager.switch_strategy}")
    print(f"节点总数: {len(manager.available_nodes)}")
    print("-" * 60)

    healthy = manager.get_healthy_nodes()
    unhealthy = manager.get_unhealthy_nodes()
    print(f"健康节点: {len(healthy)} / 异常节点: {len(unhealthy)}")
    print()

    for node in manager.available_nodes:
        name = node.get("name", "?")
        is_current = "👉" if name == current_name else "  "
        health = "✅" if node.get("healthy") else "❌"
        delay = node.get("delay", "N/A")
        print(f"  {is_current} {health} {name}  (延迟: {delay})")


def cmd_switch(manager: ClashManager, strategy: str = None):
    if strategy:
        manager.switch_strategy = strategy
    print(f"策略: {manager.switch_strategy}")
    ok = manager.switch_node()
    if ok:
        current = manager.get_current_node()
        print(f"当前节点: {current['name'] if current else '?'}")
    else:
        print("[❌] 切换失败")
        sys.exit(1)


def cmd_status(manager: ClashManager):
    status = manager.get_status()
    mode = status.get("mode")
    print(f"API:      {manager.api_url}")
    print(f"策略组:   {manager.group_name}")
    print(f"运行模式: {mode or 'N/A'}"
          f"{'  ⚠️ rule 模式下切换策略组不改变实际出口' if mode and mode != 'global' else ''}")
    print(f"当前节点: {status['current_node'] or 'N/A'}")
    print(f"节点索引: {manager.current_node_index}")
    print(f"出口 IP:  {status.get('exit_ip') or '未检测'}")
    print(f"下载计数: {status['download_count']}")
    print(f"403 错误: {status['error_403_count']}/{manager.max_403_errors}")
    print(f"可用节点: {status['available_nodes_count']}"
          f"（健康 {status.get('healthy_nodes_count', '-')}）")
    cooling = status.get("cooling_nodes") or {}
    if cooling:
        print(f"冷却中:   {len(cooling)} 个")
        for name, sec in sorted(cooling.items(), key=lambda kv: -kv[1])[:8]:
            print(f"          {name}  剩 {sec}s")


def cmd_stats(manager: ClashManager):
    """展示各节点负载统计，直观反映负载均衡效果。"""
    status = manager.get_status()
    print(f"当前节点: {status['current_node'] or 'N/A'}  策略: {status['strategy']}")
    print("-" * 72)
    print(f"{'节点':<44} {'下载':>6} {'403':>6} {'冷却':>6}")
    rows = sorted(
        set(status["node_download_counts"]) | set(status["node_403_counts"])
        | set(status.get("cooling_nodes") or {}),
        key=lambda n: (-status["node_download_counts"].get(n, 0), -status["node_403_counts"].get(n, 0)),
    )
    cooling = status.get("cooling_nodes") or {}
    for name in rows:
        cool = cooling.get(name, 0)
        print(f"{name:<44} {status['node_download_counts'].get(name, 0):>6} "
              f"{status['node_403_counts'].get(name, 0):>6} "
              f"{(str(cool) + 's') if cool else '-':>6}")


def cmd_health(manager: ClashManager):
    manager.refresh_nodes()
    manager.refresh_node_health()
    print(f"测延迟中... (策略组: {manager.group_name})")
    print("-" * 50)
    for node in manager.available_nodes:
        name = node.get("name", "?")
        delay = node.get("delay", "N/A")
        healthy = "✅" if node.get("healthy") else "❌"
        print(f"  {healthy} {name}: {delay}ms")


def cmd_test(manager: ClashManager):
    print(f"测试 API 连通性 ({manager.api_url})...")
    if manager.test_connection():
        print("[✅] Clash API 可达")
    else:
        print("[❌] Clash API 不可达")
        sys.exit(1)

    print(f"测试代理出口 ({manager.proxy_port})...")
    if manager._post_switch_connectivity_ok():
        print(f"[✅] 代理出口可用 → {manager.post_switch_test_url}")
    else:
        print(f"[❌] 代理出口不可用 → {manager.post_switch_test_url}")
        sys.exit(1)


def cmd_monitor(manager: ClashManager, interval: int = 30):
    print(f"启动健康检查守护 (间隔 {interval}s)...")
    try:
        manager.start_auto_health_check(interval)
    except KeyboardInterrupt:
        print("\n已停止")


def parse_probe_spec(spec: str) -> dict:
    """把 `--probe` 参数解析成 SiteProbe 的构造参数。

    格式::

        https://api.x.com/gen                  → GET
        POST:https://api.x.com/gen             → POST（自动带空 JSON body）
        POST:https://api.x.com/gen|code=50000  → POST，且业务码 50000 判为被封

    ⚠️ 别用 `partition(":")` 切方法前缀 —— `https://x/` 的第一个冒号在 scheme 里，
    会把 `https` 当成方法名、URL 变成 `//x/`。只认已知的 HTTP 方法。
    """
    spec, _, blocked = (spec or "").partition("|")
    method, url = "GET", spec
    for m in ("POST", "GET", "PUT", "HEAD"):
        if spec.upper().startswith(m + ":"):
            method, url = m, spec[len(m) + 1:]
            break
    if not url.strip():
        raise ValueError("探针 URL 为空")
    kw = {"url": url.strip(), "method": method}
    if method == "POST":
        kw["body"] = {}
    if blocked:
        kw["body_code_field"] = "code"
        kw["body_blocked_codes"] = [blocked.split("=")[-1]]
    return kw


def cmd_priority(manager: ClashManager, pin=None, unpin=None, block=None,
                 unblock=None, show: bool = True):
    """查看 / 修改节点优先度（置顶、拉黑）。"""
    policy = manager.policy
    if policy is None:
        print("[❌] 未启用优先度模块（需要 clash_policy.py；或用 disable_policy 关掉了）")
        sys.exit(1)

    changed = False
    for n in (pin or []):
        policy.pin(n); changed = True
    for n in (unpin or []):
        policy.unpin(n); changed = True
    for n in (block or []):
        policy.block(n); changed = True
    for n in (unblock or []):
        policy.unblock(n); changed = True
    if changed:
        policy.save()
        print()

    if not show and not changed:
        return
    names = [n["name"] for n in manager.available_nodes]
    known = set(names)
    print(f"优先度: {policy.describe(names)}")
    print(f"持久化文件: {policy.priority_file}")
    print("-" * 70)
    if policy.pin_nodes:
        print("📌 置顶:")
        for n in policy.pin_nodes:
            print(f"   {'✅' if n in known else '⚠️ 不在当前池中'}  {n}")
    if policy.block_nodes:
        print("🚫 拉黑:")
        for n in policy.block_nodes:
            print(f"   {'✅' if n in known else '⚠️ 不在当前池中'}  {n}")
    if not policy.pin_nodes and not policy.block_nodes:
        print("（未设置置顶/拉黑）")
    print("\n提示: 节点名可用 `list` 子命令查看，支持前缀匹配的完整名")


def cmd_netcheck(manager: ClashManager, nodes=None, workers: int = 4,
                 no_exit_ip: bool = False, probe_spec: str = None):
    """网络体检: 逐节点延迟 / 真实出口 IP / 目标站判定。

    probe_spec 形如 `POST:https://api.example.com/gen?appId=x|code=50000`
    —— 竖线前是方法和 URL，竖线后是「业务码=判为被封的值」。
    """
    probe = None
    if probe_spec:
        try:
            kw = parse_probe_spec(probe_spec)
            probe = SiteProbe(**kw)
        except Exception as e:
            print(f"[⚠️] --probe 解析失败({e})，仅做延迟与出口 IP 体检")

    result = manager.netcheck(nodes=nodes, workers=workers,
                              probe=probe, check_exit_ip=not no_exit_ip)
    if not result["total"]:
        print("[❌] 没有可体检的节点")
        sys.exit(1)

    print(f"\n{'节点':<40} {'延迟':>8} {'出口 IP':>18} {'判定':>9}")
    print("-" * 80)
    for r in result["nodes"]:
        delay = f"{r['delay_ms']}ms" if r["delay_ms"] else "-"
        ip = r["exit_ip"] or "-"
        verdict = r["verdict"] or "-"
        mark = {"ok": "✅", "blocked": "🚫", "dead": "❌"}.get(r["verdict"], " ")
        name = r["name"][:38]
        print(f"{mark}{name:<38} {delay:>8} {ip:>18} {verdict:>9}")
        if r["error"]:
            print(f"    └ {r['error'][:70]}")

    s = result["summary"]
    print("-" * 80)
    print(f"合计 {result['total']} 个: ok={s['ok']} blocked={s['blocked']} "
          f"dead={s['dead']} | 独立出口 IP {s['unique_exit_ips']} 个")
    if s["unique_exit_ips"] and s["unique_exit_ips"] < result["total"] * 0.5:
        print("[💡] 独立出口 IP 明显少于节点数 —— 不少节点其实共用同一个出口，"
              "轮换它们没有意义")


def main():
    parser = argparse.ArgumentParser(description="Clash 代理池节点管理")
    sub = parser.add_subparsers(dest="command", help="子命令")

    sub.add_parser("list", help="列出所有节点")
    sp = sub.add_parser("switch", help="切换节点")
    sp.add_argument("--random", action="store_true", help="随机切换")
    sp.add_argument("--strategy", choices=SWITCH_STRATEGIES, help="本次切换使用的负载均衡策略")
    sub.add_parser("status", help="查看当前状态")
    sub.add_parser("stats", help="查看各节点负载统计")
    sub.add_parser("health", help="对所有节点测延迟")
    sub.add_parser("test", help="测试代理连通性")
    mp = sub.add_parser("monitor", help="后台健康检查")
    mp.add_argument("--interval", type=int, default=30, help="检查间隔秒数")
    np_ = sub.add_parser("netcheck", help="网络体检: 逐节点延迟/出口 IP/目标站判定")
    np_.add_argument("--node", action="append", help="只体检这些节点（可重复）")
    np_.add_argument("--workers", type=int, default=4, help="并发数（默认 4，别调太高）")
    np_.add_argument("--no-exit-ip", action="store_true",
                     help="跳过逐节点出口 IP 检测（快，且不会切节点）")
    np_.add_argument("--probe", metavar="SPEC",
                     help="目标站探针，如 'POST:https://api.x.com/gen|code=50000'")
    pp = sub.add_parser("priority", help="查看/修改节点优先度(置顶、拉黑)")
    pp.add_argument("--pin", action="append", help="置顶某节点（可重复）")
    pp.add_argument("--unpin", action="append", help="取消置顶（可重复）")
    pp.add_argument("--block", action="append", help="拉黑某节点（可重复）")
    pp.add_argument("--unblock", action="append", help="解除拉黑（可重复）")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    manager = ClashManager(get_config())

    if args.command == "list":
        cmd_list(manager)
    elif args.command == "switch":
        strategy = "random" if args.random else args.strategy
        cmd_switch(manager, strategy=strategy)
    elif args.command == "status":
        cmd_status(manager)
    elif args.command == "stats":
        cmd_stats(manager)
    elif args.command == "health":
        cmd_health(manager)
    elif args.command == "test":
        cmd_test(manager)
    elif args.command == "monitor":
        cmd_monitor(manager, interval=args.interval)
    elif args.command == "netcheck":
        cmd_netcheck(manager, nodes=args.node, workers=args.workers,
                     no_exit_ip=args.no_exit_ip, probe_spec=args.probe)
    elif args.command == "priority":
        cmd_priority(manager, pin=args.pin, unpin=args.unpin,
                     block=args.block, unblock=args.unblock)


if __name__ == "__main__":
    main()

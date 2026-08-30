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

from clash_manager import ClashManager, SWITCH_STRATEGIES


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


def get_config():
    return {
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
    }


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
    print(f"API:      {manager.api_url}")
    print(f"策略组:   {manager.group_name}")
    print(f"当前节点: {status['current_node'] or 'N/A'}")
    print(f"节点索引: {manager.current_node_index}")
    print(f"下载计数: {status['download_count']}")
    print(f"403 错误: {status['error_403_count']}/{manager.max_403_errors}")
    print(f"可用节点: {status['available_nodes_count']}")


def cmd_stats(manager: ClashManager):
    """展示各节点负载统计，直观反映负载均衡效果。"""
    status = manager.get_status()
    print(f"当前节点: {status['current_node'] or 'N/A'}  策略: {status['strategy']}")
    print("-" * 60)
    print(f"{'节点':<40} {'下载':>6} {'403':>6}")
    rows = sorted(
        set(status["node_download_counts"]) | set(status["node_403_counts"]),
        key=lambda n: (-status["node_download_counts"].get(n, 0), -status["node_403_counts"].get(n, 0)),
    )
    for name in rows:
        print(f"{name:<40} {status['node_download_counts'].get(name, 0):>6} {status['node_403_counts'].get(name, 0):>6}")


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


if __name__ == "__main__":
    main()

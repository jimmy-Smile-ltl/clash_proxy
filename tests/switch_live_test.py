# -*- coding: utf-8 -*-
"""switch_live_test.py — 真实切换测试（需要第二个 mihomo 实例，避免干扰在跑的采集）

验证 P0 三项:
  ① 切换后出口 IP 真的变了（verify_exit_ip 生效，且返回实测 IP）
  ② group_name="GLOBAL" 能被正常使用（旧版会被强制改到别的组）
  ③ rule 模式下切换会被识破并拒绝（旧版会静默"成功"）

用法（连你自己的**第二个** Clash 实例，别打在正在采集的那个上）:
    set CLASH_API_URL=http://127.0.0.1:9091
    set CLASH_SECRET=your-secret
    set CLASH_PROXY_PORT=7891
    set CLASH_TEST_URL=https://你的目标站/
    python tests/switch_live_test.py
"""
import os
import pathlib
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import logging                                          # noqa: E402
logging.basicConfig(level=logging.INFO, format="%(message)s")
import requests                                         # noqa: E402
from clash_manager import ClashManager                  # noqa: E402

API = os.environ.get("CLASH_API_URL", "http://127.0.0.1:9091")
SECRET = os.environ.get("CLASH_SECRET", "your-secret")
H = {"Authorization": f"Bearer {SECRET}"}
STATE = os.path.join(tempfile.gettempdir(), "cm_switch_live.json")
for f in (STATE, STATE + ".lock"):
    if os.path.exists(f):
        os.remove(f)

ok = []


def check(name, cond, detail=""):
    ok.append(bool(cond))
    print(f"{'✅' if cond else '❌'} {name} {detail}", flush=True)


def set_mode(mode):
    s = requests.Session()
    s.trust_env = False
    s.patch(f"{API}/configs", json={"mode": mode}, headers=H, timeout=6)


cfg = {
    "api_url": API,
    "secret": SECRET,
    "proxy_port": int(os.environ.get("CLASH_PROXY_PORT", "7891")),
    "post_switch_test_url": os.environ.get("CLASH_TEST_URL", "https://example.com/"),
    "group_name": "GLOBAL",          # ★ 旧版会拒绝 GLOBAL 并改用它发现的第一个组
    "state_file": STATE,
    "post_switch_max_retries": 4,
    "exit_ip_log_file": os.path.join(tempfile.gettempdir(), "cm_switch_ips.tsv"),
}

set_mode("global")
m = ClashManager(cfg)
check("group_name='GLOBAL' 被接受", m.group_name == "GLOBAL", f"-> {m.group_name}")
check("节点列表非空", len(m.available_nodes) > 3, f"-> {len(m.available_nodes)} 个")

ip0 = m.get_exit_ip()
check("基线出口 IP", bool(ip0), f"-> {ip0}")

# ── ① 正常切换: 出口 IP 应当变化 ──
print("\n--- ① global 模式下切换（应成功且出口变化）---")
switched = m.switch_node()
ip1 = m.get_exit_ip()
check("切换返回成功", switched)
check("出口 IP 已变化", ip1 and ip1 != ip0, f"-> {ip0} ⇒ {ip1}")
check("last_exit_ip 已记录", m.last_exit_ip == ip1, f"-> {m.last_exit_ip}")
check("出口 IP 溯源文件已写入",
      os.path.exists(cfg["exit_ip_log_file"]) and os.path.getsize(cfg["exit_ip_log_file"]) > 0)

# ── ② 冷却: 失败节点不应出现在健康节点里 ──
print("\n--- ② 冷却机制 ---")
victim = m.get_current_node()["name"]
m.mark_node_failed(victim, "测试")
healthy_names = [n["name"] for n in m.get_healthy_nodes()]
check("失败节点被排除出健康列表", victim not in healthy_names, f"-> {victim}")
check("冷却剩余时间可读", m.cooldown_remaining(victim) > 0, f"-> {m.cooldown_remaining(victim)}s")

# ── ③ rule 模式下切换应被识破（核心回归）──
print("\n--- ③ rule 模式下切换（应被出口 IP 校验拒绝）---")
set_mode("rule")
m2 = ClashManager({**cfg, "state_file": STATE + ".2"})
before = m2.get_exit_ip()
res = m2.switch_node(max_retries=2)
after = m2.get_exit_ip()
check("rule 模式下切换被拒绝", res is False, f"-> switch_node()={res}")
check("出口确实没变（证明拒绝是对的）", before == after, f"-> {before} == {after}")

set_mode("global")
print(f"\n===== {sum(ok)}/{len(ok)} 项通过 =====")
sys.exit(0 if all(ok) else 1)

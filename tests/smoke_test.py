# -*- coding: utf-8 -*-
"""smoke_test.py — ClashManager 冒烟测试（只读，不切换节点）

逐项验证出口 IP 校验 / 三态探测 / 冷却退避 / verdict 上报 / 状态合并写 / 文件锁 / mode。
**不会切换节点**，可以安全地在采集任务运行期间执行。

用法（连你自己的 Clash）:
    set CLASH_API_URL=http://127.0.0.1:9097
    set CLASH_SECRET=your-secret
    set CLASH_PROXY_PORT=7897
    set CLASH_TEST_URL=https://你的目标站/
    python tests/smoke_test.py
"""
import json
import os
import pathlib
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from clash_manager import ClashManager, _FileLock   # noqa: E402

STATE = os.path.join(tempfile.gettempdir(), "cm_smoke_state.json")
for f in (STATE, STATE + ".lock"):
    if os.path.exists(f):
        os.remove(f)

cfg = {
    "api_url": os.environ.get("CLASH_API_URL", "http://127.0.0.1:9097"),
    "secret": os.environ.get("CLASH_SECRET", "1"),
    "proxy_port": int(os.environ.get("CLASH_PROXY_PORT", "7897")),
    "post_switch_test_url": os.environ.get("CLASH_TEST_URL", "https://www.gstatic.com/generate_204"),
    "group_name": "GLOBAL",
    "enable_node_switching": False,      # ★ 不碰节点，不干扰正在跑的采集
    "state_file": STATE,
    "exit_ip_log_file": os.path.join(tempfile.gettempdir(), "cm_smoke_ips.tsv"),
}
m = ClashManager(cfg)
ok = []


def check(name, cond, detail=""):
    ok.append(bool(cond))
    print(f"{'✅' if cond else '❌'} {name} {detail}", flush=True)


# 1. 模式读取
mode = m.get_mode()
check("get_mode()", mode == "global", f"-> {mode}")

# 2. 出口 IP（经代理，新连接）
ip = m.get_exit_ip()
check("get_exit_ip()", ip and "." in ip, f"-> {ip}")

# 3. 三态探测: 首页应到 200
verdict, code = m.probe_endpoint()
check("probe_endpoint(首页)", verdict == "ok" and code == 200, f"-> ({verdict}, {code})")

# 4. 三态探测: 不存在的域名应判 dead（链路不通，不是 blocked）
v2, c2 = m.probe_endpoint("https://this-host-does-not-exist-xyz.invalid/")
check("probe_endpoint(坏地址)=dead", v2 == "dead", f"-> ({v2}, {c2})")

# 5. 冷却退避: 120s -> 240s -> 480s
n = "测试节点A"
c1 = m.mark_node_failed(n, "试1")
c2 = m.mark_node_failed(n, "试2")
c3 = m.mark_node_failed(n, "试3")
check("指数冷却 120/240/480", (c1, c2, c3) == (120, 240, 480), f"-> {c1}/{c2}/{c3}")
check("is_cooled()", m.is_cooled(n))
check("cooldown_remaining()", 0 < m.cooldown_remaining(n) <= 480, f"-> {m.cooldown_remaining(n)}s")

# 6. 成功即修复
m.mark_node_ok(n)
check("mark_node_ok 清除冷却", not m.is_cooled(n) and m.node_fail_counts[n] == 0)

# 7. 403 计数衰减
m.node_403_counts[n] = 5
m.mark_node_ok(n)
check("403 计数成功即衰减", m.node_403_counts[n] == 4, f"-> {m.node_403_counts[n]}")

# 8. verdict 上报
m.report_verdict("ok", n)
check("report_verdict('ok')", m.error_403_count == 0)

# 9. 状态合并写: 模拟另一进程写入的节点，不能被本次 save 冲掉
os.makedirs(os.path.dirname(STATE), exist_ok=True)
with open(STATE, "w", encoding="utf-8") as f:
    json.dump({"node_download_counts": {"别的进程的节点": 99},
               "node_403_counts": {}, "verified_nodes": ["别的进程的节点"],
               "download_count": 500}, f, ensure_ascii=False)
m.node_download_counts["本进程节点"] = 7
m.save_state()
saved = json.load(open(STATE, encoding="utf-8"))
check("save_state 合并(不覆盖他进程)",
      saved["node_download_counts"].get("别的进程的节点") == 99
      and saved["node_download_counts"].get("本进程节点") == 7,
      f"-> {saved['node_download_counts']}")
check("verified_nodes 取并集", "别的进程的节点" in saved["verified_nodes"])

# 10. 文件锁可重入获取/释放
try:
    with _FileLock(STATE, timeout=2):
        pass
    locked_ok = True
except Exception as e:
    locked_ok = False
    print("   lock err:", e)
check("_FileLock 获取/释放", locked_ok)

# 11. get_status 暴露新字段
st = m.get_status()
check("get_status 含 mode/exit_ip",
      "mode" in st and "cooling_nodes" in st,
      f"-> mode={st.get('mode')} ip={st.get('exit_ip')}")

print(f"\n===== {sum(ok)}/{len(ok)} 项通过 =====")
sys.exit(0 if all(ok) else 1)

# -*- coding: utf-8 -*-
"""subscriptions_test.py — 订阅合并的离线单测（不联网、不需要 Clash）。

覆盖:
    - 来源类型识别 / 来源列表 / URL 打码
    - Clash YAML / JSON / base64 URI / 纯 URI 列表解析
    - 元数据过滤 / 端口校验 / 跨源去重 / 同名改名
    - 模板合并 / 基底配置合并 / 策略组引用清理
    - URL 失败时的手动文件回退
    - 输出文件写入

用法:
    python tests/subscriptions_test.py
"""

import base64
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clash_subscriptions import (  # noqa: E402
    ParsedSource, SourceSpec, _cache_paths, _source_kind, build_config,
    fetch_source, load_sources, mask_source, merge_proxies, parse_subscription,
    write_outputs,
)

_ok = []


def check(name, cond, detail=""):
    _ok.append(bool(cond))
    print(f"{'✅' if cond else '❌'} {name} {detail}", flush=True)


def _spec(raw="", kind="file", label="test", path=None, url=None):
    return SourceSpec(raw=raw, kind=kind, label=label, path=path, url=url)


# ---------------------------------------------------------------- 基础

def test_mask_and_kind():
    print("\n=== 来源识别 / 打码 ===")
    check("URL 识别", _source_kind("https://example.com/sub") == "url")
    check("未知识别", _source_kind("not-exist-file") == "unknown")

    tmp = tempfile.mkdtemp()
    try:
        f = os.path.join(tmp, "a.yaml")
        open(f, "w", encoding="utf-8").write("proxies: []\n")
        check("文件识别", _source_kind(f) == "file")
        check("目录识别", _source_kind(tmp) == "dir")
    finally:
        shutil.rmtree(tmp)

    masked = mask_source("https://example.com/api/v1/sub?token=SECRET123")
    check("URL 打码隐藏 token", "SECRET123" not in masked and "example.com" in masked,
          f"-> {masked}")


def test_manifest():
    print("\n=== 来源列表 ===")
    tmp = tempfile.mkdtemp()
    try:
        subdir = os.path.join(tmp, "extra")
        os.makedirs(subdir)
        open(os.path.join(subdir, "b.yaml"), "w", encoding="utf-8").write("proxies: []\n")
        f = os.path.join(tmp, "a.yaml")
        open(f, "w", encoding="utf-8").write("proxies: []\n")
        manifest = os.path.join(tmp, "subscriptions.txt")
        with open(manifest, "w", encoding="utf-8") as fh:
            fh.write("# 注释\nhttps://example.com/sub?token=SECRET\n" + f + "\n" + subdir + "\n")
        specs = load_sources(manifest)
        check("URL + 文件 + 目录展开", len(specs) == 3, f"-> {len(specs)}")
        check("URL label 已打码", "SECRET" not in specs[0].label, f"-> {specs[0].label}")
        check("目录中的文件被展开", any(s.path and s.path.endswith("b.yaml") for s in specs))
    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------- 解析

def test_parse_formats():
    print("\n=== 订阅格式解析 ===")
    yaml_text = """
proxies:
  - {name: 香港01, type: ss, server: 1.1.1.1, port: 443, cipher: aes-256-gcm, password: pass}
  - {name: 剩余流量：1TB, type: ss, server: 2.2.2.2, port: 443, cipher: aes-256-gcm, password: pass}
  - {name: 距离下次重置剩余：1天, type: ss, server: 3.3.3.3, port: 443, cipher: aes-256-gcm, password: pass}
  - {name: 坏端口, type: ss, server: 4.4.4.4, port: 99999, cipher: aes-256-gcm, password: pass}
  - {name: 直连, type: direct, server: 5.5.5.5, port: 1}
proxy-groups:
  - {name: 🚀节点选择, type: select, proxies: [香港01, DIRECT]}
rules:
  - MATCH,🚀节点选择
"""
    fmt, proxies, cfg, unsupported = parse_subscription(yaml_text)
    check("Clash YAML 识别", fmt == "clash-yaml" and cfg is not None, f"-> {fmt}")
    check("proxies 数量", len(proxies) == 5, f"-> {len(proxies)}")

    json_text = json.dumps({"proxies": [{"name": "A", "type": "ss",
                                         "server": "1.1.1.1", "port": 1}]})
    fmt, proxies, _, _ = parse_subscription(json_text)
    check("JSON 识别", fmt == "clash-json" and len(proxies) == 1, f"-> {fmt}")

    uri_text = "\n".join([
        "http://user:pass@1.2.3.4:8080#HTTP节点",
        "socks5://5.6.7.8:1080#SOCKS节点",
        "ss://YWVzLTI1Ni1nY206cGFzcw==@203.0.113.9:8388#SS节点",
        "trojan://example-password@203.0.113.9:443?sni=example.com#Trojan节点",
        "vless://uuid@203.0.113.9:443#不支持的节点",
    ])
    fmt, proxies, _, unsupported = parse_subscription(uri_text)
    check("URI 列表识别", fmt == "uri-list" and len(proxies) == 4, f"-> {fmt} {len(proxies)}")
    check("不支持的 URI 计数", unsupported == 1, f"-> {unsupported}")
    check("SS URI 转换", any(p.get("type") == "ss" and p.get("cipher") for p in proxies))
    check("Trojan URI 转换", any(p.get("type") == "trojan" for p in proxies))

    b64 = base64.b64encode(uri_text.encode()).decode()
    fmt, proxies, _, _ = parse_subscription(b64)
    check("base64 订阅识别", fmt == "base64-uri-list" and len(proxies) == 4, f"-> {fmt}")

    try:
        parse_subscription("<html><body>login</body></html>")
        check("HTML 被拒绝", False)
    except ValueError:
        check("HTML 被拒绝", True)


def test_merge_filter():
    print("\n=== 过滤 / 去重 / 改名 ===")
    yaml_text = """
proxies:
  - {name: 香港01, type: ss, server: 1.1.1.1, port: 443, cipher: aes-256-gcm, password: pass}
  - {name: 剩余流量：1TB, type: ss, server: 2.2.2.2, port: 443, cipher: aes-256-gcm, password: pass}
  - {name: 距离下次重置剩余：1天, type: ss, server: 3.3.3.3, port: 443, cipher: aes-256-gcm, password: pass}
  - {name: 坏端口, type: ss, server: 4.4.4.4, port: 99999, cipher: aes-256-gcm, password: pass}
  - {name: 直连, type: direct, server: 5.5.5.5, port: 1}
"""
    fmt, proxies, cfg, _ = parse_subscription(yaml_text)
    merged, stats = merge_proxies([ParsedSource(_spec(), True, fmt, proxies, cfg)])
    check("只保留 1 个有效节点", len(merged) == 1, f"-> {len(merged)}")
    check("过滤 2 个元数据", stats["metadata"] == 2, f"-> {stats['metadata']}")
    check("过滤 2 个无效节点", stats["invalid"] == 2, f"-> {stats['invalid']}")

    # 跨源重复 + 同名不同配置
    p1 = {"name": "X", "type": "ss", "server": "1.1.1.1", "port": 443,
          "cipher": "aes-256-gcm", "password": "a"}
    p1_dup = {"name": "X2", "type": "ss", "server": "1.1.1.1", "port": 443,
              "cipher": "aes-256-gcm", "password": "a"}
    p1_other = {"name": "X", "type": "ss", "server": "2.2.2.2", "port": 443,
                "cipher": "aes-256-gcm", "password": "b"}
    merged, stats = merge_proxies([
        ParsedSource(_spec("a"), True, "yaml", [p1], None),
        ParsedSource(_spec("b"), True, "yaml", [p1_dup, p1_other], None),
    ])
    check("跨源重复被去掉", stats["duplicates"] == 1, f"-> {stats['duplicates']}")
    check("同名不同配置被改名", stats["renamed"] == 1, f"-> {stats['renamed']}")
    names = [p["name"] for p in merged]
    check("改名后名称唯一", len(names) == len(set(names)) == 2, f"-> {names}")


# ---------------------------------------------------------------- 配置合并

def test_build_template():
    print("\n=== 模板合并 ===")
    merged = [
        {"name": "A", "type": "ss", "server": "1.1.1.1", "port": 1},
        {"name": "B", "type": "ss", "server": "2.2.2.2", "port": 2},
    ]
    parsed = [ParsedSource(_spec(), True, "yaml", merged, None)]
    cfg, desc = build_config(merged, parsed,
                             str(ROOT / "config" / "config.example.yaml"),
                             base="template")
    check("模板基底", "模板" in desc, f"-> {desc}")
    names = [p["name"] for p in cfg["proxies"]]
    check("proxies 被替换", names == ["A", "B"], f"-> {names}")
    managed = [g for g in cfg["proxy-groups"] if g["name"] == "🚀节点选择"][0]
    check("管理组包含全部新节点", "A" in managed["proxies"] and "B" in managed["proxies"])
    check("模板示例节点已移除", "示例节点-香港01" not in managed["proxies"])
    check("规则保留", any("MATCH" in str(r) for r in cfg.get("rules") or []))


def test_build_base():
    print("\n=== 基底配置合并 ===")
    base = {
        "mode": "rule",
        "proxies": [{"name": "old", "type": "ss", "server": "9.9.9.9", "port": 443}],
        "proxy-groups": [
            {"name": "🚀节点选择", "type": "select", "proxies": ["old", "DIRECT"]},
            {"name": "地区组", "type": "select", "proxies": ["old"]},
        ],
        "rules": ["GEOIP,CN,DIRECT", "MATCH,🚀节点选择"],
    }
    merged = [
        {"name": "A", "type": "ss", "server": "1.1.1.1", "port": 1},
        {"name": "B", "type": "ss", "server": "2.2.2.2", "port": 2},
    ]
    parsed = [ParsedSource(_spec(), True, "yaml", merged, base)]
    cfg, desc = build_config(merged, parsed, str(ROOT / "config" / "config.example.yaml"),
                             base="1")
    check("使用来源 #1 作基底", "来源 #1" in desc, f"-> {desc}")
    check("旧 proxies 被替换", [p["name"] for p in cfg["proxies"]] == ["A", "B"])
    managed = [g for g in cfg["proxy-groups"] if g["name"] == "🚀节点选择"][0]
    check("管理组 = 新节点 + 原特殊项", managed["proxies"] == ["A", "B", "DIRECT"],
          f"-> {managed['proxies']}")
    region = [g for g in cfg["proxy-groups"] if g["name"] == "地区组"][0]
    check("旧节点失效的组被新节点填充", region["proxies"] == ["A", "B"],
          f"-> {region['proxies']}")
    check("规则保留", cfg["rules"] == ["GEOIP,CN,DIRECT", "MATCH,🚀节点选择"])
    check("mode 保留", cfg["mode"] == "rule")


# ---------------------------------------------------------------- 拉取回退 / 输出

def test_manual_fallback():
    print("\n=== URL 失败 → 手动文件回退 ===")
    tmp = tempfile.mkdtemp()
    try:
        cache_dir = os.path.join(tmp, "cache")
        os.makedirs(cache_dir)
        url = "http://127.0.0.1:1/sub?token=SECRET"
        _, _, manual = _cache_paths(cache_dir, url)
        os.makedirs(os.path.dirname(manual), exist_ok=True)
        with open(manual, "w", encoding="utf-8") as f:
            f.write("proxies:\n  - {name: A, type: ss, server: 1.1.1.1, port: 1}\n")
        spec = SourceSpec(raw=url, kind="url", label="test", url=url)
        text, from_cache, note, path = fetch_source(spec, cache_dir, timeout=1)
        check("回退到手动文件", bool(text) and from_cache, f"-> {from_cache} {note[:40]}")
        check("回退路径存在", os.path.isfile(path or ""))
    finally:
        shutil.rmtree(tmp)


def test_min_interval_skip():
    print("\n=== 订阅最小拉取间隔（防机场滥用）===")
    import clash_subscriptions as cs

    tmp = tempfile.mkdtemp()
    try:
        cache_dir = os.path.join(tmp, "cache")
        os.makedirs(cache_dir)
        url = "http://127.0.0.1:1/sub?token=example"
        raw, meta, _ = cs._cache_paths(cache_dir, url)
        with open(raw, "w", encoding="utf-8") as f:
            f.write("proxies:\n  - {name: A, type: ss, server: 1.1.1.1, port: 1}\n")
        with open(meta, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time()}, f)

        spec = SourceSpec(raw=url, kind="url", label="test", url=url)
        calls = []

        def fake_get(u, timeout, ua, proxy=None):
            calls.append(u)
            return ("proxies:\n"
                    "  - {name: B, type: ss, server: 2.2.2.2, port: 2}\n"
                    "  - {name: C, type: ss, server: 3.3.3.3, port: 3}\n"), "text/plain"

        old = cs._http_get
        cs._http_get = fake_get
        try:
            text, from_cache, note, _ = cs.fetch_source(
                spec, cache_dir, min_interval=3600)
            check("间隔内直接使用缓存，不发请求",
                  bool(text) and from_cache and not calls, f"-> calls={len(calls)}")
            check("提示包含「跳过」", "跳过" in note, f"-> {note[:36]}")

            text2, from_cache2, _, _ = cs.fetch_source(
                spec, cache_dir, min_interval=3600, force=True)
            check("--force 会强制重新拉取",
                  (not from_cache2) and len(calls) == 1, f"-> calls={len(calls)}")
        finally:
            cs._http_get = old
    finally:
        shutil.rmtree(tmp)


def test_update_gate():
    print("\n=== 订阅更新门控（24h / 12h）===")
    import clash_subscriptions as cs

    now = 1_800_000_000.0
    gate = {"last_attempt_at": now - 25 * 3600,
            "failure_since": now - 13 * 3600}
    ok, why = cs._gate_allows_fetch(gate, now)
    check("24h+12h 全失败才允许", ok and not why, f"-> {ok} {why}")

    gate = {"last_attempt_at": now - 23 * 3600,
            "failure_since": now - 13 * 3600}
    ok, why = cs._gate_allows_fetch(gate, now)
    check("24h 内拒绝", not ok and "24" in why, f"-> {why}")

    gate = {"last_attempt_at": now - 25 * 3600,
            "failure_since": now - 11 * 3600}
    ok, why = cs._gate_allows_fetch(gate, now)
    check("失败不足 12h 拒绝", not ok and "12" in why, f"-> {why}")

    gate = {"last_attempt_at": now - 25 * 3600, "failure_since": 0}
    ok, why = cs._gate_allows_fetch(gate, now)
    check("未检测到全失败拒绝", not ok, f"-> {why}")

    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "gate.json")
        g = cs._load_gate(path)
        g = cs._update_gate_health(path, g, {"ok": True, "healthy": 0, "total": 5}, now)
        check("全失败设置 failure_since", g["failure_since"] == now)
        g = cs._update_gate_health(path, g, {"ok": True, "healthy": 2, "total": 5}, now + 100)
        check("有健康节点重置 failure_since", g["failure_since"] == 0)
    finally:
        shutil.rmtree(tmp)


def test_test_cooldown():
    print("\n=== 节点测试冷却 ===")
    import clash_subscriptions as cs

    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "last_test.json")
        check("无记录时冷却为 0", cs._test_cooldown_remaining(path, 3600) == 0)
        cs._record_test(path, {"total": 5, "available_total": 89, "healthy": 3})
        check("刚测试后进入冷却", cs._test_cooldown_remaining(path, 3600) > 0)
    finally:
        shutil.rmtree(tmp)


def test_degraded_response_keeps_cache():
    """机场限流时可能 200 + 只有一个占位节点，不能让坏响应覆盖好缓存。"""
    print("\n=== 异常响应保护（防止被降级订阅清空节点池）===")
    import clash_subscriptions as cs

    tmp = tempfile.mkdtemp()
    try:
        cache_dir = os.path.join(tmp, "cache")
        os.makedirs(cache_dir)
        url = "http://127.0.0.1:1/sub?token=example"
        raw, meta, _ = cs._cache_paths(cache_dir, url)
        good = ("proxies:\n"
                "  - {name: A, type: ss, server: 1.1.1.1, port: 1}\n"
                "  - {name: B, type: ss, server: 2.2.2.2, port: 2}\n"
                "  - {name: C, type: ss, server: 3.3.3.3, port: 3}\n")
        with open(raw, "w", encoding="utf-8") as f:
            f.write(good)
        with open(meta, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time()}, f)

        spec = SourceSpec(raw=url, kind="url", label="test", url=url)
        bad = ("proxies:\n"
               "  - {name: '剩余流量：1TB', type: ss, server: 9.9.9.9, "
               "port: 443, cipher: aes-256-gcm, password: pass}\n")

        old_get = cs._http_get
        cs._http_get = lambda u, timeout, ua, proxy=None: (bad, "text/plain")
        try:
            text, from_cache, note, _ = cs.fetch_source(
                spec, cache_dir, min_interval=0, force=True)
            check("坏响应被识别并回退缓存", from_cache and "有效节点" in note,
                  f"-> {note[:40]}")
            check("好缓存没有被覆盖",
                  cs._count_valid_proxies(cs._read_text_file(raw)) == 3,
                  f"-> {cs._count_valid_proxies(cs._read_text_file(raw))}")
        finally:
            cs._http_get = old_get
    finally:
        shutil.rmtree(tmp)


def test_write_outputs():
    print("\n=== 输出写入 ===")
    tmp = tempfile.mkdtemp()
    try:
        cfg = {"proxies": [{"name": "A", "type": "ss", "server": "1.1.1.1", "port": 1}],
               "proxy-groups": [{"name": "G", "type": "select", "proxies": ["A"]}],
               "rules": ["MATCH,G"]}
        merged = cfg["proxies"]
        out = os.path.join(tmp, "config.yaml")
        proxies = os.path.join(tmp, "merged.yaml")
        meta = os.path.join(tmp, "last.json")
        write_outputs(cfg, merged, out, proxies, meta, {"ok": True})
        check("配置已写", os.path.getsize(out) > 0)
        check("纯节点列表已写", os.path.getsize(proxies) > 0)
        check("元数据已写", json.load(open(meta, encoding="utf-8"))["ok"] is True)
    finally:
        shutil.rmtree(tmp)


def main():
    test_mask_and_kind()
    test_manifest()
    test_parse_formats()
    test_merge_filter()
    test_build_template()
    test_build_base()
    test_manual_fallback()
    test_min_interval_skip()
    test_update_gate()
    test_test_cooldown()
    test_degraded_response_keeps_cache()
    test_write_outputs()
    passed = sum(_ok)
    print(f"\n===== {passed}/{len(_ok)} 项通过 =====")
    sys.exit(0 if passed == len(_ok) else 1)


if __name__ == "__main__":
    main()

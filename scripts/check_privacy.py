#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_privacy.py — 公开仓库的「私有数据」提交前检查。

为什么需要
----------
``.gitignore`` 只对**未跟踪**文件生效。下面这些情况仍可能把真实订阅 /
节点列表 / 密钥带进公开仓库：

- ``git add -f`` 强制添加了本应被忽略的文件；
- 新增了一种 .gitignore 没覆盖的文件名（例如 ``sub-2026.yaml``）；
- 复制粘贴时把真实节点 URI / 密码写进了文档或测试。

本脚本做三层检查：

1. **路径规则**：真实配置 / 订阅 / 状态 / 私钥等文件名直接拒绝；
2. **ignore 规则**：被 .gitignore 忽略却出现在暂存区 / 跟踪列表（通常 = ``git add -f``）；
3. **内容规则**：代理 URI、密码、secret、私钥、订阅 token 等高置信特征。

用法::

    python scripts/check_privacy.py --staged   # pre-commit 钩子用（默认）
    python scripts/check_privacy.py --all      # 检查全部已跟踪文件
    python scripts/check_privacy.py --paths config/config.yaml local/proxies.txt

退出码：0 = 通过；1 = 发现疑似私有数据；2 = 无法执行检查。
"""

import argparse
import os
import re
import subprocess
import sys
from typing import Dict, Iterable, List, Sequence, Tuple

# 仓库根目录（本脚本位于 scripts/ 下）
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 这些是公开模板 / 说明文件：允许入库，且跳过内容扫描
SAFE_PATHS = {
    ".env.example",
    "config/README.md",
    "config/config.example.yaml",
    "scripts/check_privacy.py",
}

# 内容扫描上限：超过 2MB 的文件不扫（正常代码/文档不会这么大）
MAX_SCAN_BYTES = 2 * 1024 * 1024

# 占位符特征：出现任意一个就认为不是真实凭据
_PLACEHOLDER_HINTS = (
    "example", "your-", "your_", "replace", "xxxx", "****", "placeholder",
    "demo", "target", "目标", "你的", "替换", "示例", "占位",
    "user:pass", "user:pw", "@host:", "//host", "localhost",
    "127.0.0.1", "0.0.0.0", "1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4",
    "1.2.3.4", "5.6.7.8", "9.9.9.9", "...", "…",
    # RFC 5737 / RFC 3849 文档专用地址（测试数据用）
    "203.0.113.", "198.51.100.", "192.0.2.", "2001:db8",
)

# 高置信度内容特征
_PROXY_URI_RE = re.compile(
    r"\b(?:ss|vmess|vless|trojan|hysteria2?|hy2|tuic|socks5h?)://[^\s'\"<>%]{6,}",
    re.I,
)
# 带账号密码的 HTTP(S) 代理 URL（http://user:pass@host:port）
# 排除 %：避免把源码里的格式串（"http://%s:%s@%s:%s"）误判为真实凭据
_CRED_URL_RE = re.compile(
    r"https?://[^\s/:@'\"%]+:[^\s/@'\"%]+@[^\s/'\"%]+",
    re.I,
)
# 无 scheme 的静态列表行：host:port:user:pass
_STATIC_PROXY_LINE_RE = re.compile(
    r"^\s*(?:(?:\d{1,3}\.){3}\d{1,3}|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}):\d{2,5}:[^\s:]+:[^\s:]+\s*$"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
)
_SECRET_LINE_RE = re.compile(
    r"^\s*(?:secret|password|passwd|uuid|token)\s*:\s*['\"]?([^'\"\s#]+)",
    re.I,
)
_ENV_SECRET_RE = re.compile(r"^\s*CLASH_SECRET\s*=\s*(.+?)\s*$", re.I)
_BEARER_RE = re.compile(
    r"Authorization\s*:\s*Bearer\s+([A-Za-z0-9._~+/=-]{8,})", re.I
)
_SUB_TOKEN_RE = re.compile(
    r"(?:subscribe|subscription)[^\s'\"<>]*[?&]token=([A-Za-z0-9._~-]{10,})",
    re.I,
)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.I,
)


# ---------------------------------------------------------------- git 辅助

def _git(args: Sequence[str]) -> subprocess.CompletedProcess:
    """在仓库根目录执行 git 命令（失败不抛异常，由调用方判断）。"""
    return subprocess.run(
        ["git"] + list(args),
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _staged_files() -> List[str]:
    """暂存区里新增/修改/重命名的文件（不含删除）。"""
    r = _git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"])
    if r.returncode != 0:
        raise RuntimeError("git diff --cached 执行失败: %s" % (r.stderr or "").strip())
    return [p for p in r.stdout.split("\0") if p]


def _tracked_files() -> List[str]:
    r = _git(["ls-files", "-z"])
    if r.returncode != 0:
        raise RuntimeError("git ls-files 执行失败: %s" % (r.stderr or "").strip())
    return [p for p in r.stdout.split("\0") if p]


def _is_ignored(rel_path: str) -> bool:
    """路径是否命中 .gitignore（--no-index: 已跟踪文件也检查规则）。"""
    r = _git(["check-ignore", "--quiet", "--no-index", rel_path])
    return r.returncode == 0


def _rel(path: str) -> str:
    """把用户传入的路径规范成仓库相对路径（统一用 /）。"""
    p = path.replace("\\", "/")
    if os.path.isabs(p):
        try:
            p = os.path.relpath(p, REPO).replace("\\", "/")
        except ValueError:
            pass
    while p.startswith("./"):
        p = p[2:]
    return p


# ---------------------------------------------------------------- 路径规则

def _path_reasons(rel_path: str) -> List[str]:
    """按文件名 / 目录判断它是否属于私有数据。"""
    reasons: List[str] = []
    low = rel_path.lower()
    base = low.rsplit("/", 1)[-1]

    # config/ 下只允许模板与说明
    if low.startswith("config/") and rel_path not in SAFE_PATHS:
        reasons.append("config/ 下只允许 README.md 与 config.example.yaml")

    # 环境变量（.env.example 白名单）
    if low == ".env" or (low.startswith(".env.") and rel_path != ".env.example"):
        reasons.append("环境变量文件（可能含 API secret）")

    # 私有数据目录
    for d in ("local/", "private/", "secrets/"):
        if low.startswith(d):
            reasons.append("私有数据目录 %s" % d)

    # 根目录订阅 / 节点列表兜底命名
    if "/" not in rel_path:
        if re.match(r"^(?:subscriptions?|subs?|sub)[-_.]?.*\.ya?ml$", base, re.I):
            reasons.append("根目录订阅文件")
        if re.match(r"^.+\.clash\.ya?ml$", base, re.I):
            reasons.append("Clash 订阅文件")
        if re.match(r"^(?:proxies?|proxy[-_]?list|nodes?|node[-_]?list).*\.txt$", base, re.I):
            reasons.append("根目录节点列表")

    # 运行时状态
    if re.match(r"^clash_node_state.*\.json$", base, re.I):
        reasons.append("节点状态文件")
    if base == ".clash_priority.json" or re.match(r"^.+\.(?:state|priority)\.json$", base, re.I):
        reasons.append("状态 / 优先度文件")
    if base.endswith((".lock", ".tmp")):
        reasons.append("锁 / 临时文件")
    if base.endswith(".tsv") and ("exit_ip" in low or low.startswith("exit")):
        reasons.append("出口 IP 日志")

    # Docker 本地覆盖（可能含私有挂载路径）
    if base == "docker-compose.override.yml":
        reasons.append("Docker 本地覆盖文件")

    # 凭据 / 私钥
    if base.endswith((".pem", ".key")) or base.startswith(("id_rsa", "id_ed25519")):
        reasons.append("私钥文件")
    if re.match(r"^(?:credentials|secrets?).*\.json$", base, re.I):
        reasons.append("凭据文件")

    return reasons


# ---------------------------------------------------------------- 内容规则

_TYPE_NAMES = {
    "str", "int", "float", "bool", "dict", "list", "tuple", "set",
    "any", "optional", "sequence", "mapping", "none", "bytes", "object",
}


def _looks_like_type_value(value: str) -> bool:
    """`secret: str = ""` 这类 Python 类型标注不是凭据。"""
    low = (value or "").strip().lower()
    if not low:
        return False
    if low in _TYPE_NAMES:
        return True
    return bool(re.match(r"^(?:optional|dict|list|tuple|sequence|mapping)\[", low))


def _is_placeholder(value: str) -> bool:
    """凭据值是否明显是占位符（your-secret / replace-me / xxxx / 全零 UUID...）。"""
    low = (value or "").strip().strip("'\"").lower()
    if not low:
        return True
    if low in ("password", "passwd", "secret", "none", "null", "changeme", "todo"):
        return True
    # 00000000-0000-... / 纯 0
    if set(low) <= set("0-"):
        return True
    return any(h in low for h in _PLACEHOLDER_HINTS)


def _read_text(rel_path: str) -> str:
    """读取文本文件；二进制或过大文件返回空串（跳过内容扫描）。"""
    full = os.path.join(REPO, rel_path)
    try:
        if os.path.getsize(full) > MAX_SCAN_BYTES:
            return ""
        with open(full, "rb") as f:
            data = f.read()
    except OSError:
        # 暂存区里可能有已被删除/重命名的路径；路径规则仍然有效
        return ""
    if b"\x00" in data:
        return ""
    return data.decode("utf-8", errors="replace")


def _content_reasons(rel_path: str) -> List[Tuple[int, str]]:
    """返回 [(行号, 原因)]。"""
    text = _read_text(rel_path)
    if not text:
        return []

    findings: List[Tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        reasons: List[str] = []

        if _PRIVATE_KEY_RE.search(line):
            reasons.append("私钥内容")

        for m in _PROXY_URI_RE.finditer(line):
            if not _is_placeholder(m.group(0)):
                reasons.append("疑似代理 URI（%s...）" % m.group(0)[:12])
                break

        for m in _CRED_URL_RE.finditer(line):
            if not _is_placeholder(m.group(0)):
                reasons.append("疑似带账号密码的代理 URL")
                break

        if _STATIC_PROXY_LINE_RE.match(line) and not _is_placeholder(line):
            reasons.append("疑似静态代理行（host:port:user:pass）")

        m = _SECRET_LINE_RE.match(line)
        if (m and "def " not in line
                and not _looks_like_type_value(m.group(1))
                and not _is_placeholder(m.group(1))):
            reasons.append("疑似真实 %s 值" % line.strip().split(":", 1)[0].strip())

        m = _ENV_SECRET_RE.match(line)
        if m and not _is_placeholder(m.group(1)):
            reasons.append("疑似真实 CLASH_SECRET")

        m = _BEARER_RE.search(line)
        if m and not _is_placeholder(m.group(1)):
            reasons.append("疑似真实 Bearer token")

        m = _SUB_TOKEN_RE.search(line)
        if m and not _is_placeholder(m.group(1)):
            reasons.append("疑似订阅 token")

        for m in _UUID_RE.finditer(line):
            if not _is_placeholder(m.group(0)):
                reasons.append("疑似真实 UUID")
                break

        # 同一行去重，保持顺序
        if reasons:
            findings.append((lineno, "；".join(dict.fromkeys(reasons))))

    return findings


# ---------------------------------------------------------------- 主流程

def check(files: Iterable[str], check_ignored: bool = True) -> Dict[str, List[str]]:
    """返回 {路径: [原因, ...]}；空字典 = 通过。"""
    problems: Dict[str, List[str]] = {}

    for raw in files:
        rel_path = _rel(raw)
        if not rel_path:
            continue

        reasons: List[str] = []
        if rel_path not in SAFE_PATHS:
            reasons.extend(_path_reasons(rel_path))
            if check_ignored and _is_ignored(rel_path):
                reasons.append("被 .gitignore 忽略却出现在提交里（多半是 git add -f）")
            for lineno, why in _content_reasons(rel_path):
                reasons.append("第 %s 行：%s" % (lineno, why))

        if reasons:
            # 去重并保序
            problems[rel_path] = list(dict.fromkeys(reasons))

    return problems


def main() -> int:
    # Windows 控制台默认 GBK，输出 emoji / 中文原因会 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="公开仓库私有数据检查（真实订阅 / 节点列表 / 密钥 / 状态文件）"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true",
                       help="检查暂存区（默认，pre-commit 用）")
    group.add_argument("--all", action="store_true",
                       help="检查全部已跟踪文件")
    parser.add_argument("--paths", nargs="+", metavar="PATH",
                        help="只检查指定路径（调试用）")
    args = parser.parse_args()

    try:
        if args.paths:
            files = [_rel(p) for p in args.paths]
            check_ignored = False
        elif args.all:
            files = _tracked_files()
            check_ignored = True
        else:
            files = _staged_files()
            check_ignored = True
    except RuntimeError as e:
        print("[❌] %s" % e)
        return 2

    problems = check(files, check_ignored=check_ignored)

    if not problems:
        print("[✅] 隐私检查通过：%s 个文件" % len(files))
        return 0

    print("[❌] 发现疑似私有数据，已阻止提交：")
    for path, reasons in sorted(problems.items()):
        print("  %s" % path)
        for why in reasons:
            print("      - %s" % why)
    print("")
    print("如果确认这些文件可以公开，请把它们移出仓库或加入白名单；")
    print("如果已经不小心提交过：先轮换订阅 / 密钥，再清理 git 历史。")
    print("详见 docs/private-data.md")
    return 1


if __name__ == "__main__":
    sys.exit(main())

#!/bin/sh
# 启用本仓库的提交前隐私检查（只修改本地 git 配置，不会入库）。
#
# 用法:
#     sh scripts/install_git_hooks.sh
#
# 撤销:
#     git config --unset core.hooksPath

set -e
cd "$(dirname "$0")/.."

chmod +x .githooks/pre-commit 2>/dev/null || true
git config core.hooksPath .githooks

echo "[✅] 已启用 pre-commit 隐私检查"
echo "     core.hooksPath = $(git config --get core.hooksPath)"
echo "     手动检查: python scripts/check_privacy.py --staged"

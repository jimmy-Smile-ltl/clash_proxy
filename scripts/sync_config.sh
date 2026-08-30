#!/bin/bash
# sync_config.sh — 从你的订阅/源配置同步并适配 Docker
# 用法: CLASH_CONFIG_SOURCE=/path/to/source.yaml ./scripts/sync_config.sh [secret]
#
# 源配置可以是任何 Clash/Mihomo 格式的订阅文件（含 proxies 节点列表）。
# 脚本会做三件事:
#   1. 复制到 config/config.yaml
#   2. 适配 Docker（external-controller 改为 0.0.0.0、允许局域网访问）
#   3. 注入 secret（与 .env 中 CLASH_SECRET 保持一致）

SOURCE="${CLASH_CONFIG_SOURCE:?请设置环境变量 CLASH_CONFIG_SOURCE 指向源订阅配置文件}"
TARGET="$(dirname "$0")/../config/config.yaml"
SECRET="${1:-${CLASH_SECRET:-your-secret-here}}"

if [ ! -f "$SOURCE" ]; then
    echo "[❌] 源配置文件不存在: $SOURCE"
    exit 1
fi

cp "$SOURCE" "$TARGET"

# 适配 Docker 环境
sed -i "s|external-controller: '127.0.0.1:9090'|external-controller: '0.0.0.0:9090'|" "$TARGET"
sed -i 's|allow-lan: false|allow-lan: true|' "$TARGET"

# 注入 secret（如果已有则替换，否则在 external-controller 后添加）
if grep -q "^secret:" "$TARGET"; then
    sed -i "s|^secret:.*|secret: \"$SECRET\"|" "$TARGET"
else
    sed -i "/^external-controller:/a secret: \"$SECRET\"" "$TARGET"
fi

echo "[✅] 配置已同步到 $TARGET"
echo "     external-controller: 0.0.0.0:9090"
echo "     allow-lan: true"
echo "     secret: $SECRET"
echo ""
echo "  如需生效，请重启容器: docker compose restart"

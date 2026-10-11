# config/ 目录说明

`config/` 挂载到 Mihomo 容器（`/root/.config/mihomo`），需要以下文件：

| 文件 | 来源 | 说明 |
|------|------|------|
| `config.yaml` | 你的订阅 | 复制 `config.example.yaml` 后填入真实节点。**勿提交到仓库**（已在 .gitignore 中） |
| `geoip.metadb` | 官方发布页 | GeoIP 数据库（可选，规则分流用） |
| `GeoSite.dat` | 官方发布页 | 域名分类数据库（可选） |
| `cache.db` | 运行时生成 | Mihomo 自动创建的缓存 |

## 下载地理数据库

```bash
# 官方发布页: https://github.com/MetaCubeX/meta-rules-dat/releases
curl -L -o config/geoip.metadb \
  "https://github.com/MetaCubeX/meta-rules-dat/releases/latest/download/geoip.metadb"

curl -L -o config/GeoSite.dat \
  "https://github.com/MetaCubeX/meta-rules-dat/releases/latest/download/geosite.dat"
mv config/geosite.dat config/GeoSite.dat
```

## 快速验证配置

```bash
docker compose up -d
python clash_cli.py test
```

看到 `[✅] Clash API 可达` 即配置成功。

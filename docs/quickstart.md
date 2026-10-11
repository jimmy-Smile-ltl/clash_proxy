# 从零到一：用机场节点完成海外数据采集

本教程带你走完完整流程：**部署 Mihomo → 导入订阅 → 接入爬虫 → 观察负载均衡效果**。

适用场景：采集少量海外数据（学术文献、公开数据集、国外新闻、API 数据等），
不想为商业代理池付费，手上正好有一份机场订阅。

---

## 第 1 步：准备一个 Mihomo 实例

二选一即可。

### 方案 A：本地 Clash 客户端（零成本，适合调试）

以 Clash Verge 为例：

1. 导入你的机场订阅；
2. 打开「设置 → 网络」：开启 **Allow LAN**，记下**代理端口**（默认 7897）；
3. 打开「设置 → 外部控制」：记下 **API 端口**（默认 9090）和 **secret**（如无 secret 可留空字符串）。

> 其他客户端同理：Clash for Windows 代理端口 7890、API 端口 9090；mihomo-party 与 Clash Verge 一致。

### 方案 B：Docker 部署（适合服务器/长期运行）

```bash
git clone https://github.com/jimmy-Smile-ltl/clash_proxy.git
cd clash_proxy

# 1. 准备配置
cp config/config.example.yaml config/config.yaml
#    将订阅内容粘贴进 config.yaml（proxies 段 + proxy-groups 段）
#    或从订阅文件同步：
#    CLASH_CONFIG_SOURCE=/path/to/你的订阅.yaml ./scripts/sync_config.sh your-secret

# 2. （可选）下载地理数据库，见 config/README.md

# 3. 启动
docker compose up -d

# 4. 验证容器
docker compose logs clash | head -20
```

启动后：
- API 地址：`http://127.0.0.1:9099`（仅本机）
- 代理地址：`http://127.0.0.1:7777`（仅本机）

---

## 第 2 步：安装依赖并验证

```bash
pip install requests redis   # redis 可选

cp .env.example .env
# 编辑 .env：填入你的 secret、API 端口、代理端口

python clash_cli.py test
```

预期输出：

```
[✅] Clash连接正常，版本: v1.18.x
[✅] 代理出口可用 → https://www.gstatic.com/generate_204
```

再看一眼节点列表：

```bash
python clash_cli.py list
```

预期能看到你订阅里的所有节点及延迟（订阅里的"剩余流量""到期时间"等条目已被自动过滤）。

---

## 第 3 步：写第一个采集脚本

```python
# spider_demo.py
import requests
from clash_manager import ClashManager

manager = ClashManager({
    "api_url": "http://127.0.0.1:9099",       # 你的 API 端口
    "secret": "your-secret",                  # 与 config.yaml 一致
    "group_name": "🚀节点选择",               # 订阅节点所在的策略组
    "proxy_port": 7777,                       # 你的代理端口
    # 关键：填你实际要采集的目标站（切换后验证的就是它，黑名单也按它隔离）
    "post_switch_test_url": "https://目标网站/",
    "switch_strategy": "least_403",           # 403 最少的节点优先
    "max_downloads_per_node": 100,            # 演示：每节点 100 次就轮换
})

proxies = manager.get_proxy_config()

for i, url in enumerate(url_list):
    try:
        resp = requests.get(url, proxies=proxies, timeout=30)
    except requests.RequestException:
        # 链路不通：该节点进入冷却（会自动排除出轮换），累计达阈值则切换
        manager.report_verdict("dead")
        continue

    if resp.status_code == 403:
        manager.report_verdict("blocked")   # 累计 3 次 403 自动切节点
        continue
    elif resp.status_code == 200:
        manager.report_verdict("ok")  # 成功计数，满 100 次轮换
        with open(f"data/{i}.html", "wb") as f:
            f.write(resp.content)
```

运行几分钟后查看负载均衡效果：

```bash
python clash_cli.py stats
```

预期能看到各节点的下载次数趋于均衡、403 多的节点被策略冷落。

---

## 第 4 步（可选）：长期运行防护

### 后台健康守护

节点可能在任务中途失效。单独开一个终端：

```bash
python clash_cli.py monitor --interval 30
```

每 30 秒检测一次代理出口，不可用立即切换。

### 多进程共享黑名单

多个爬虫进程同时跑时，用 Redis 共享错误节点黑名单（可选）：

```bash
# 启动 Redis 后设置地址
export CLASH_REDIS_URL="redis://127.0.0.1:6379/0"
```

不装 Redis 也没关系，黑名单自动降级为各进程独立的内存版本。

---

## 策略怎么选？

| 场景 | 推荐策略 |
|------|----------|
| 目标站不敏感，想均匀分摊流量 | `round_robin` |
| 目标站对单一 IP 请求频率敏感 | `least_used`（把流量摊给最少使用的节点） |
| 目标站会封 IP（403） | `least_403`（403 历史最少的节点优先） |
| 节点质量参差不齐，想碰运气 | `random` |

---

## 常见问题排查

| 现象 | 原因 | 解决 |
|------|------|------|
| `[❌] Clash API 不可达` | API 端口/secret 不对 | 检查客户端「外部控制」设置；Docker 用 9099 |
| `[💡] 策略组不存在` | group_name 填错 | 不填或填错都会自动发现第一个 Selector 组 |
| 代理出口不可用 | 本地代理端口不对 | 检查 .env 的 CLASH_PROXY_PORT |
| 切换后仍然 403 | 该节点也被目标站封了 | 这是正常现象，会继续换下一个节点；换 `least_403` 策略 |
| 所有节点都被拉黑 | 黑名单误杀 | 内置保护：黑名单接近节点总数时自动清空 |
| 节点列表为空 | 订阅元数据全被过滤 | 检查策略组是否正确；`clash_cli.py list` 确认 |

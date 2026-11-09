# 多订阅合并与定期刷新

`clash_subscriptions.py` 把「多个机场订阅 → 一个节点池」这条流程自动化：

```text
来源列表（URL / 文件 / 目录）
   → 拉取（失败用缓存 / 手动文件）
   → 解析 Clash YAML / JSON / URI 列表
   → 过滤元数据与无效节点
   → 跨源去重、同名改名
   → 合并成一份 Clash 配置 + 一份纯节点列表
   → 可选：重载 Mihomo、逐个节点测延迟
```

真实订阅 URL、节点凭据、缓存、合并结果都只落在 `local/` 与 `config/`，
两者已被 `.gitignore` 忽略；详见 [private-data.md](private-data.md)。

---

## 1. 来源列表

默认文件：`local/subscriptions.txt`（整个 `local/` 被忽略）。

```text
# 每行一个来源：URL / 文件 / 目录；# 开头为注释
https://example.com/api/v1/client/subscribe?token=...
C:/Users/你/clash_private/second.yaml
local/subscriptions/extra/
```

- **URL**：`http://` / `https://` 开头，自动拉取；
- **文件**：已存在的路径（YAML / JSON / TXT）；
- **目录**：扫描目录下第一层的 `yaml / yml / json / txt`（不递归）。

命令行管理：

```bash
python clash_cli.py sub list
python clash_cli.py sub add https://example.com/sub?token=...
python clash_cli.py sub remove 2
```

> URL 在日志和 `sub list` 中会打码成 `https://host/***`，不会把 token 打出来。

---

## 2. 拉取失败怎么办

按以下顺序回退：

1. `local/subscriptions/cache/<hash>.raw` —— 上次成功拉取的缓存；
2. `local/subscriptions/manual/<hash>.yaml` —— **你手动复制进去的内容**；
3. 都没有 → 报错并打印手动文件的准确路径。

因此 URL 打不开时**不需要改来源列表**：把订阅内容复制到报错给出的
`local/subscriptions/manual/<hash>.yaml`，重新执行即可。

默认任何一个来源既无缓存又无手动文件时**中止合并**（避免把另一个机场的节点
当成全部）；确认可以跳过时加 `--allow-partial`。

有些机场的订阅域名会**屏蔽直连或屏蔽某些出口 IP**（表现为连接重置 / 403）。
这时让订阅拉取走你日常 Clash 的代理端口：

```bash
# .env
CLASH_SUBSCRIPTION_PROXY=http://127.0.0.1:7897
```

拉取时会先尝试直连，失败后再走这个代理；成功后仍会写入缓存。

---

## 3. 支持的格式

| 格式 | 说明 |
|---|---|
| Clash YAML（`proxies:`） | 主要格式，完整保留节点字段 |
| Clash JSON | 同上，JSON 版本 |
| base64 订阅 | 解码后按 URI 列表解析 |
| URI 列表 | 支持 `http/https/socks5/socks5h/socks4/ss/trojan`；`vmess/vless/hysteria2/tuic/mieru/anytls` 的 URI 暂不转换 |

> 机场订阅通常带 `clash=1` 参数返回 Clash YAML，这是最完整、最推荐的格式。
> URI 列表里不支持的协议会计数并跳过，不会影响其它节点。

过滤规则：

- 节点名含「剩余流量 / 距离下次重置剩余 / 套餐到期 / 订阅 / 官网 …」→ 元数据；
- `type` 是 `Selector / URLTest / Direct / Reject …` → 非节点；
- 缺少 `server` / `port`、端口越界、`server` 是 `127.0.0.1` 等 → 无效；
- 跨来源相同配置（除 `name` 外全同）→ 去重；
- 同名但配置不同 → 自动改名为 `名称 [2]`，保证配置合法。

---

## 4. 合并结果的两种基底

`--base`（或 `CLASH_SUBSCRIPTION_BASE`）决定用谁当完整配置的骨架：

| 值 | 行为 | 适用 |
|---|---|---|
| `auto`（默认） | 选**第一个带 `proxy-groups` + `rules` 的来源**当基底，其它来源节点合并进来 | 想保留某个机场的分流规则 |
| `template` | 用 `config/config.example.yaml`（或 `--template` 指定文件）当骨架 | 只要一个干净的最小配置 |
| `1` / `2` … | 指定第 N 个来源当基底 | 明确知道要用哪份规则 |

无论哪种基底，都会：

1. `proxies` 替换为**全部合并节点**；
2. 清理策略组里指向已删除旧节点的引用；
3. 把管理组（默认 `🚀节点选择`）更新为「全部合并节点 + 原有特殊项（DIRECT / 子组）」；
4. 保留 `rules` / `rule-providers` / `dns` 等字段。

输出：

- `config/config.yaml`（默认，可 `--out` 改）
- `local/subscriptions/merged-proxies.yaml` —— 只有 `proxies:` 的纯节点列表
- `local/subscriptions/last_sync.json` —— 本次同步计数（不含凭据）

---

## 5. 一次同步 / 定期刷新

```bash
# 拉取 + 合并 + 写配置（不碰运行中的 Clash）
python clash_cli.py sub sync

# 写入后重载 Mihomo，并测试合并后的节点
python clash_cli.py sub sync --reload --test

# 每 6 小时刷新一次（默认 reload + test）
python clash_cli.py sub watch --interval 21600
```

`sub sync` 常用参数：

| 参数 | 说明 |
|---|---|
| `--base auto|template|N` | 基底选择 |
| `--mode base|rule|global|direct` | 生成配置的 mode |
| `--group NAME` | 管理组名 |
| `--out PATH` | 输出配置路径 |
| `--api-listen HOST:PORT` | 写入 `external-controller` |
| `--mixed-port PORT` | 写入 `mixed-port` |
| `--secret STR` | 写入 API secret（建议用 `.env`） |
| `--reload` | 调用 Clash API 重载 |
| `--test` | 测试节点延迟 |
| `--allow-partial` | 某来源失败时跳过 |
| `--dry-run` | 只解析合并，不写文件 |

长期运行建议用系统服务 / 计划任务执行 `sub sync --reload --test`，
或者让 `sub watch` 常驻。Windows 可以直接用：

```powershell
# 启动专用实例 + 后台定期刷新（PID/日志都在 local/subscriptions/）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_watch.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop_watch.ps1
```

Linux / 服务器上可用 systemd timer 或 cron 调用同一条 `sub sync --reload --test`。

---

## 6. 推荐：专用 Mihomo 实例

不要把日常使用的 Clash Verge 配置直接替换成合并配置（可能改动 TUN / DNS /
端口，影响正常上网）。更稳的做法是**另起一个专用 mihomo 实例**给项目用：

| 实例 | API | 代理端口 | 用途 |
|---|---|---|---|
| Clash Verge（已有） | 9097 | 7897 | 日常上网 |
| 专用实例（本项目） | 9098 | 7898 | 爬虫 / 采集 |

`.env`：

```bash
CLASH_API_URL=http://127.0.0.1:9098
CLASH_PROXY_PORT=7898
CLASH_SECRET=你的强随机串
# 专用实例是 global 模式：实际出口是 GLOBAL 组
CLASH_GROUP_NAME=GLOBAL
# 合并后节点很多：别让每次 CLI 启动都全量测延迟
CLASH_STARTUP_HEALTH_CHECK=false
# 部分节点访问 api.ipify.org 会失败，换一个可达的出口 IP 服务
CLASH_EXIT_IP_URL=https://ifconfig.me/ip

CLASH_SUBSCRIPTION_OUT=local/mihomo/config.yaml
CLASH_SUBSCRIPTION_MODE=global
CLASH_SUBSCRIPTION_API_LISTEN=127.0.0.1:9098
CLASH_SUBSCRIPTION_MIXED_PORT=7898
CLASH_SUBSCRIPTION_SECRET=你的强随机串
```

生成配置：

```bash
python clash_cli.py sub sync --reload --test
```

> `--reload` 需要专用实例已经启动。第一次先生成配置，再启动实例。

启动（Windows，Clash Verge 自带 `verge-mihomo.exe`）：

```powershell
# 推荐：用项目自带脚本（自动处理带空格的路径、PID 文件、日志）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_mihomo.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop_mihomo.ps1

# 手动启动（路径有空格时必须整体加引号）
& "C:\Program Files\Clash Verge\verge-mihomo.exe" `
  -d "C:\path\to\clash_proxy\local\mihomo" `
  -f "C:\path\to\clash_proxy\local\mihomo\config.yaml"
```

> Windows PowerShell 5.1 读取 UTF-8 脚本需要 BOM；脚本已带 BOM。若你另存为
> UTF-8 无 BOM，中文注释可能被当成乱码并导致解析错误。

Linux / 服务器上同理，`-d` 目录里放 `geoip.dat` / `geosite.dat` / `Country.mmdb`
（可以从你现有 Clash 的 data 目录复制），避免启动时联网下载。

**为什么必须 global 模式 + GLOBAL 组**：rule 模式下流量按 `rules` 链走，
切换 `🚀节点选择` 组对流量可能是 no-op；global 模式下实际出口就是 `GLOBAL` 组。
本项目默认用 `GLOBAL` 组做切换，因此 `.env` 里 `CLASH_GROUP_NAME=GLOBAL`。

验证：

```bash
python clash_cli.py sub test
python clash_cli.py test        # API + 代理出口连通性
```

---

## 7. 节点测试

`sub test` / `sub sync --test` 通过 Clash API 的 `/proxies/{name}/delay`
逐节点测延迟，并发默认 8：

- 测试 URL 默认 `https://www.gstatic.com/generate_204`；
- 生产采集建议改成**目标站**（`--test-url` 或 `CLASH_TEST_URL`），
  因为「通 gstatic」不代表「能到目标站」；
- 并发不要调到节点数：几十个节点同时打同一个站会触发服务端整体限流。

测试结果会按延迟排序打印；节点名只出现在终端 / 本地日志，不会入库。

---

## 8. 隐私清单

| 内容 | 位置 | 是否入库 |
|---|---|---|
| 订阅来源列表（含 token） | `local/subscriptions.txt` | ❌ 已忽略 |
| 拉取缓存 / 手动文件 | `local/subscriptions/` | ❌ 已忽略 |
| 合并后的配置 / 纯节点列表 | `config/config.yaml`、`local/subscriptions/merged-proxies.yaml` | ❌ 已忽略 |
| API secret | `.env` / `local/mihomo/config.yaml` | ❌ 已忽略 |
| 示例模板 | `config/config.example.yaml`、`.env.example` | ✅ 只含占位符 |

提交前钩子会检查路径、`.gitignore` 状态和内容特征；详见
[private-data.md](private-data.md)。

---

## 9. 常见问题

**Q: 第一次拉取失败，但以前能拉？**
可能被机场限频 / token 过期。先看 `sub sync` 的报错；缓存会自动顶上。
把最新内容复制到报错给出的 `local/subscriptions/manual/<hash>.yaml` 即可。

**Q: 合并后节点数比两个订阅相加少？**
正常：去掉了跨源重复、订阅元数据和无效条目。`last_sync.json` 有详细计数。

**Q: 合并后 Clash 里还是只有一个机场的节点？**
检查 `--base` 与 `--reload`；确认重载的是专用实例（9098），不是 Clash Verge（9097）。

**Q: 切换了组，出口 IP 还是国内的？**
global 模式下实际出口是 `GLOBAL` 组，不是 `🚀节点选择`。把 `CLASH_GROUP_NAME`
设为 `GLOBAL`（本项目默认如此）。

**Q: 为什么 `sub sync` 不自动重载？**
重载会真实替换运行中的配置，默认保守不执行；加 `--reload` 或使用 `sub watch`。

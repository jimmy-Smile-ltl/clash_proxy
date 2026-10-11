# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.0.0] — 2026-10

一次围绕「切换到底有没有真的生效」的大修，外加可插拔来源与优先度管理。

### 新增

- **`clash_sources.py` —— 可插拔代理来源**。原先节点只能来自「某个正在运行的
  Clash 实例的策略组」，现在支持：
  - `ClashSelectorSource`：策略组（＝原来的行为）
  - `StaticListSource`：静态代理列表 / 订阅文件 / 订阅 URL，**完全不经过 Clash**
  - `MultiClashSource`：聚合多个 Clash 实例为一个池
- **`clash_policy.py` —— 节点优先度**：手动置顶/拉黑、地区偏好与排除、延迟阈值、
  流量倍率优先。置顶节点会绕过 round-robin 预旋转，且**同时覆盖两条选择路径**。
- **`clash_probe.py` —— 目标站探针与网络体检**：`SiteProbe` 支持按**业务返回码**
  判定（很多风控站的真实判据是 `HTTP 200 + {"code": 50000}`，而非 HTTP 状态码）；
  新增逐节点出口 IP（`get_exit_ip_of`）与 `clash_cli.py netcheck`。
- **`clash_clients.py` —— 客户端适配层**：一行拿到跟着代理池走的
  requests / aiohttp / curl_cffi / Playwright 会话，内置「切换后自动重建连接」。
- **全池熔断**：所有出口都在冷却时静默等待（`pool_exhausted_wait_sec`，默认 1 小时）。
- `tests/sources_test.py`：离线单测（不联网、不需要 Clash，可进 CI）。
- `AGENTS.md` / `llms.txt` / `docs/README.md` / `pyproject.toml` / CI。

### 修复

- **节点类型白名单丢节点（影响很大）**：原白名单没有 `Hysteria2`，实测一个 34 节点的
  订阅里有 **15 个节点被静默丢弃**（占池子一半，且是主力线路）。改为**黑名单**制
  （只排除 Selector/URLTest/Direct/Reject 这类非节点类型），新协议不再漏。
- **`switch_to_healthy_node()` 绕过所有策略**：它是第二条独立的选择路径，
  先前不受 `switch_strategy` 也不受优先度影响。现已一并覆盖。
- **探测把 403 判为「节点不通」**：风控站点上 403 往往恰恰代表节点正常且可达，
  被误判会把好节点踢出已验证池。现在「拿到任何 HTTP 响应即算可达」。
- 文档：端口两套值（本地客户端 9097/7897 vs Docker 9099/7777）现已明确对照说明；
  `config/README.md` 里指向不存在文件的命令已修正；`logic.md` 步骤编号错乱已重排。

### 变更（可能影响现有代码）

- `verify_exit_ip` 默认开启。若你的 Clash 处于 **rule 模式**，切换策略组对流量本就是
  no-op，现在会**明确报错**而不是静默「成功」。要按节点轮换出口请用 global 模式
  （或设 `force_global_mode: true`）。
- 少数机场**多个节点共用同一出口 IP**，这类情况请设 `allow_same_exit_ip: true`，
  否则会被判为「切换未生效」。

## [1.0.0] — 2026-08

首个版本。

- `ClashManager`：节点发现、延迟测试、负载均衡（4 种策略）、按站点隔离的错误黑名单、
  后台健康守护
- `clash_cli.py`：list / switch / status / stats / health / test / monitor
- Docker 部署模板、订阅同步脚本、配置讲解与快速开始文档

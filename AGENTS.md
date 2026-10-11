# AGENTS.md — 给 AI 编码 agent 的项目说明

本文件写给在你之前打开这个仓库的编码 agent（Claude Code / Cursor / Copilot 等）。
人类读者请看 [README.md](README.md)；机器可读的文档索引见 [llms.txt](llms.txt)。

## 这是什么

把 Clash / Mihomo（机场订阅客户端）里闲置的节点，管成一个可供爬虫使用的代理池。
核心能力：读节点 → 测速 → 切换出口 → 验证切换真的生效 → 失败冷却 → 自动恢复。

**已用于真实生产**：34 节点池在目标站主动风控下连续采集 1400+ 样本，期间处理了
headless 检测、浏览器指纹识别、单 IP 限流、服务端整体限流四层对抗。很多设计
决策来自这些实战，注释里的「实测」都不是推测。

## 文件结构

```
clash_manager.py   核心。ClashManager 类 + 状态持久化 + 三重验证 + 冷却。~1400 行，单文件。
clash_cli.py       命令行接口（9 个子命令）
clash_sources.py   可选扩展：可插拔代理来源（Clash 策略组 / 静态列表 / 多实例聚合）
clash_policy.py    可选扩展：节点优先度（置顶/拉黑/地区/延迟/倍率）
clash_probe.py     可选扩展：目标站探针、逐节点出口 IP、网络体检
clash_clients.py   可选扩展：requests / aiohttp / curl_cffi / Playwright 适配器
tests/             三个测试脚本（见下）
```

**扩展模块都是可选的**：删掉它们，`clash_manager.py` + `clash_cli.py` 依然完整可用
（主库用 `try: import ... except ImportError` 做优雅降级）。改动主库时**不要**引入
对扩展模块的硬依赖。

## 代码约定（请严格遵守）

- **注释与文档字符串用中文**，日志消息也是中文。
- **日志前缀带 emoji 标签**，含义固定：`[✅]` 成功 / `[❌]` 失败 / `[⚠️]` 可恢复警告 /
  `[‼]` 硬错误 / `[💡]` 给用户的提示 / `[🔍]` 发现与选择 / `[🔄]` 切换与刷新 /
  `[📊]` 计数 / `[🧊]` 冷却 / `[🌐]` 模式与出口 IP / `[⏳]` 等待重试 / `[⏭️]` 跳过候选。
- **日志参数用惰性 `%s`**，不要用 f-string。
- **类型标注用旧式写法**（`Dict` / `List` / `Optional`），不用 `dict[str, int]` 或 `X | None`。
  目标 Python 3.8（`asyncio.to_thread` 是 3.9+，扩展里已有兼容封装 `_to_thread`）。
- **docstring 写「为什么」**，尤其是反直觉的决策，要把踩过的坑写进去。
  这是本项目的核心风格，不是可选项。
- **异常处理**：宽 `except Exception` + 记日志 + 继续，不重新抛出（配置校验除外，用 `ValueError`）。
- **类内小节用横幅注释**：`# ======================== 中文小节名 ========================`。
- 配置项统一是扁平 `Dict` 的 key，与属性名 1:1 对应。

## 改动指引

**加一种代理来源** → 在 `clash_sources.py` 里继承 `ProxySource`，实现
`list_nodes()` / `activate()` / `probe_delay()`，然后在 `build_source()` 里注册。
不要改 `clash_manager.py` 的切换流程。

**加一种站点判据** → 用 `clash_probe.SiteProbe` 的 `body_code_field` +
`body_blocked_codes`，或直接调 `manager.report_verdict(...)`。
**不要**在库里硬编码「403 = 被封」——同一个 403 对不同站点含义相反。

**加一种优先度规则** → 改 `clash_policy.NodePolicy`。⚠️ 必须同时覆盖**两条**选择路径：
`_perform_switch()`（用 `_order_by_strategy`）和 `switch_to_healthy_node()`
（独立的第二条路径，不经过任何策略）。只改一条会导致规则时灵时不灵。

**改状态持久化** → `save_state()` 对计数器用单调 `max()` 合并（多进程安全）。
⚠️ 但**标志位/权重不能用 max 合并**，否则跨进程永远取消不掉置顶。
这类状态请放独立文件（参考 `NodePolicy.priority_file`）。

## 测试

```bash
python tests/sources_test.py       # 离线单测，不联网、不需要 Clash —— 改完必跑
python tests/smoke_test.py         # 冒烟自检，只读不切节点，采集跑着也能跑
python tests/switch_live_test.py   # 真实切换（会切节点，用另一个 Clash 实例跑）
```

改任何扩展模块后，`sources_test.py` 必须全绿。它已经抓到过一次真实回归
（过滤词用裸的 `"http"` 把静态来源的节点名整个误杀）。

## 这个项目最贵的几条经验（改代码前请先读）

1. **切换节点后，复用的连接仍走旧出口** —— mihomo 按**连接**选出口。keep-alive
   会让「切了节点但 IP 没变」。任何「切换后继续用同一个连接」的代码都是错的。
2. **「策略组 now 变了」≠「流量换了出口」** —— rule 模式下 `PUT /proxies/<组>`
   对流量是 no-op。唯一可靠判据是**实测出口 IP**。
3. **探活必须打目标站** —— 通 gstatic 不代表能到目标站，实测有一批节点骗过了通用探活。
4. **聚合请求速率会触发服务端整体限流** —— 几十个节点同时打一个站，单看每个都很温和。
   所以健康检测并发默认限 8，且全池失效时静默等待而不是继续试探。
5. **别用「允许列表」过滤节点类型** —— 协议一直在新增。曾经因为白名单没有
   `Hysteria2`，一个 34 节点的订阅里有 15 个被静默丢弃（占一半，且是主力线路）。

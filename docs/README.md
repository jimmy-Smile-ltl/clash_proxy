# 文档索引

所有文档的总入口。如果你是**第一次来**，按下面的「推荐阅读顺序」走。

---

## 推荐阅读顺序

| 你的目的 | 看这里 |
|---|---|
| 先搞清楚这东西适不适合我 | [../README.md](../README.md) 的〈它解决什么问题〉〈一分钟看懂它能干什么〉 |
| 想马上跑起来 | [quickstart.md](quickstart.md) |
| 已经在用，想知道怎么接进我的代码 | [http-clients.md](http-clients.md)（HTTP 库）／[browser-clients.md](browser-clients.md)（浏览器）／或直接用 `clash_clients.py` 一行接入 |
| 出问题了 / 想搞懂内部机制 | [logic.md](logic.md) |
| 要改 Clash 配置文件 | [config-guide.md](config-guide.md) |

---

## docs/ 目录

| 文档 | 内容 | 适合 |
|---|---|---|
| [quickstart.md](quickstart.md) | 从零到一：部署 Mihomo → 导入订阅 → 写第一个采集脚本 → 长期运行防护 | 新用户 |
| [http-clients.md](http-clients.md) | requests / curl_cffi / aiohttp 的完整写法；**开头就是最大的坑：keep-alive 复用旧隧道** | 用 HTTP 库采集 |
| [browser-clients.md](browser-clients.md) | Playwright / Selenium 用法；反检测最小清单；headless 检测、真实 Chrome、不要伪造 UA 等实战踩坑 | 需要真浏览器 |
| [config-guide.md](config-guide.md) | Clash 配置文件逐字段讲解、订阅导入三种方式、本地客户端 vs Docker 的差异、常见坑 | 配置 Mihomo |
| [logic.md](logic.md) | ClashManager 内部：切换状态机、三重验证、冷却、线程安全、多进程注意事项 | 二次开发 / 排查 |

---

## 仓库里其余的文档

这几个不在 `docs/` 下，但同样是正式文档：

| 位置 | 内容 |
|---|---|
| [../config/README.md](../config/README.md) | `config/` 目录需要哪些文件、地理数据库怎么下载、如何验证配置生效 |
| [../AGENTS.md](../AGENTS.md) | 给 AI 编码 agent 的项目约定、改动指引、以及「最贵的几条经验」 |
| [../llms.txt](../llms.txt) | 给 LLM 的策展文档索引（[llmstxt.org](https://llmstxt.org) 规范） |
| [../CHANGELOG.md](../CHANGELOG.md) | 版本变更记录 |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | 如何参与开发、测试怎么跑、代码约定 |
| [../SECURITY.md](../SECURITY.md) | 安全注意事项（代理端口暴露、凭证管理等） |
| [../promotion/](../promotion) | 三个平台的发布稿草稿（V2EX / Reddit-HN / 掘金-CSDN） |

---

## 代码即文档

这个项目的 docstring 承担了「为什么这样设计」的说明职责，尤其是踩过的坑。
不确定某个设计为什么存在时，直接读源码注释比读文档更快：

- `clash_manager.py` — 核心类，类 docstring 里列了「核心设计（生产踩坑沉淀）」
- `clash_sources.py` / `clash_policy.py` / `clash_probe.py` / `clash_clients.py` — 扩展模块，
  每个模块顶部都写了它解决的**具体**问题

命令行自查：`python clash_cli.py --help`

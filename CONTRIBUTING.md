# 参与开发

欢迎 issue 与 PR。这个项目不大，规矩也简单。

## 环境

```bash
git clone https://github.com/jimmy-Smile-ltl/clash_proxy.git
cd clash_proxy
pip install requests ruff
```

本地没有 Clash 也能跑**离线单测**——这是最重要的一条，绝大多数改动都应该有对应的离线测试。

## 跑测试

| 命令 | 需要什么 | 用途 |
|---|---|---|
| `python tests/sources_test.py` | 无（不联网、不需要 Clash） | **改完必跑**。覆盖来源解析、优先度规则、站点判据、客户端适配 |
| `python tests/smoke_test.py` | 一个可用的 Clash 实例 | 只读自检，不切节点，采集跑着也能安全执行 |
| `python tests/switch_live_test.py` | **两个** Clash 实例 | 真实切换验证。会切节点，别打在正在采集的实例上 |

```bash
# 冒烟测试的环境变量（Windows 用 set，Linux/macOS 用 export）
export CLASH_API_URL="http://127.0.0.1:9097"
export CLASH_SECRET="your-secret"
export CLASH_PROXY_PORT="7897"
export CLASH_TEST_URL="https://你的目标站/"
python tests/smoke_test.py
```

代码风格检查：

```bash
ruff check .
```

## 代码约定

完整约定见 [AGENTS.md](AGENTS.md)，这里列最容易踩的几条：

- **注释、docstring、日志消息都用中文**；日志前缀带 emoji 标签（含义见 AGENTS.md）。
- **类型标注用旧式写法**（`Dict` / `List` / `Optional`），保持 Python 3.8 兼容。
- **docstring 要写「为什么」**，尤其是反直觉的决策 —— 把踩过的坑写进去。这是本项目的核心风格。
- **扩展模块必须保持可选**：`clash_manager.py` / `clash_cli.py` 不能硬依赖
  `clash_sources.py` 等扩展，用 `try: import ... except ImportError` 降级。

## PR 里请写清楚

1. **你遇到的现象**（能复现最好），而不只是「改了什么」；
2. **为什么这么改** —— 如果推翻了某个现有设计，说明它原来解决什么问题、你的方案怎么覆盖；
3. 跑过哪些测试。

如果改动涉及「切换节点」「验证出口」「冷却策略」这三块，请特别谨慎：
它们都有过血泪教训，相关注释里写了原因，改之前请先读 `AGENTS.md` 末尾的
〈这个项目最贵的几条经验〉。

## 提交信息

参考现有历史，用中文，动词开头：

```
feat: 支持静态代理列表来源
fix: 修正节点类型白名单丢弃 Hysteria2 的问题
docs: 补充 keep-alive 复用旧隧道的说明
```

## 边界

本项目的定位是「**少量数据采集 + 你自己已有的机场节点**」，不是大规模爬虫基础设施。以下方向不在范围内：

- 每秒上百请求的高并发场景（那是商业代理池的活）
- 绕过目标站的账号/权限体系
- 任何违反目标站 robots.txt 或当地法律法规的用法

请遵守 [SECURITY.md](SECURITY.md) 里的注意事项，尤其别把含真实节点的配置文件提交上来。

# 安全说明

## 报告漏洞

发现安全问题请**不要**开公开 issue，直接邮件联系 `jimmysmileltl@gmail.com`，
或在 GitHub 上用 Private vulnerability reporting。我会尽快回复。

## 使用这个项目时必须注意的事

### 1. 别把含真实节点的配置提交到仓库

`config/config.yaml`、`clash_node_state.json`、`.env`、`*.priority.json`
以及各种状态文件**都已加入 `.gitignore`**。提交前请确认：

```bash
git status --short          # 确认没有 config.yaml / .env / 订阅文件
git diff --cached --stat    # 提交前再看一遍
```

订阅链接等同于账号凭证，泄露后别人可以直接消耗你的流量。

### 2. 代理端口与 API 端口不要暴露到公网

- `mixed-port`（默认 7890/7897）是一个**不需要认证的开放代理**；
- `external-controller`（默认 9090/9097）配合 `secret` 才能操作，但很多订阅的
  `secret` 是弱口令或默认值。

`docker-compose.yml` 与 `scripts/sync_config.sh` 默认都绑定 `127.0.0.1`。
**只有在容器网络里才需要改成 `0.0.0.0`** —— 改的时候务必同时：

1. 把 `secret` 设成强随机串；
2. 用防火墙或 Docker 网络限制来源；
3. 确认没有把端口映射到公网 IP。

### 3. 状态文件可能包含你的出口 IP

`exit_ip_log_file`（可选）会把「时间 / 节点名 / 真实出口 IP」写成 TSV。
节点名往往包含机场名与地区，出口 IP 能定位到具体线路 —— 分享日志前请先脱敏。

### 4. 本项目的定位边界

这个库提供的是**出口轮换**能力，它不替你决定用在哪里。请自行确认：

- 目标网站的 `robots.txt` 与 Terms of Service；
- 你所在地区关于数据采集与代理使用的法律法规；
- 不要用它做账号撞库、刷量、绕过付费墙等行为。

项目采用 MIT 协议，作者不对使用者的行为负责。

## 依赖安全

核心只依赖 `requests`。可选扩展（`redis` / `aiohttp` / `curl-cffi` / `playwright`）
按需安装 —— 不用某个客户端就不必引入它的依赖树。

CI 只跑静态检查与离线单测，不接触任何真实节点或目标站。

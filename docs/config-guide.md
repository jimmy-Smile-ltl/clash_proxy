# Clash / Mihomo 配置文件讲解

本项目不生成 Clash 配置，只**管理配置里的节点**。但用好本项目的前提是
把订阅节点正确地放进 `config.yaml`（或使用本地 Clash 客户端）。

本文面向第一次接触 Clash 配置的爬虫开发者，讲解与本项目相关的字段。

---

## 1. 配置文件是什么

Clash / Mihomo 用一份 YAML 文件描述「有什么节点、流量怎么走」。核心结构：

```yaml
# ① 入口与基础
mixed-port: 7890          # 代理端口：爬虫的流量入口
allow-lan: false          # 是否允许局域网访问
external-controller: '127.0.0.1:9090'  # ② RESTful API 地址（本项目靠它管理节点）
secret: "your-secret"     # ③ API 密钥

# ④ 节点列表（订阅的核心内容）
proxies: [...]

# ⑤ 代理组（节点如何组织、如何选择）
proxy-groups: [...]

# ⑥ 规则（哪些流量走代理、哪些直连）
rules: [...]
```

**本项目只依赖 ①②③④⑤**：通过 ② 的 API 读取 ④ 的节点、操作 ⑤ 的策略组
切换节点；③ 是 API 的认证密钥。⑥ 规则和 DNS 与节点管理无关，但会影响
「代理出口是否真的能访问目标站」。

---

## 2. 关键字段逐个讲

### 2.1 mixed-port — 代理端口（爬虫入口）

```yaml
mixed-port: 7890
```

爬虫的 HTTP/HTTPS 流量都从这一个端口走（同时支持 SOCKS5）。

- 本地客户端：这个端口由客户端决定（Clash Verge 默认 7897）；
- Docker 部署：容器内 7890 映射到宿主机的 `7777`（见 docker-compose.yml）。

对应本项目配置项 `proxy_port` / 环境变量 `CLASH_PROXY_PORT`。

### 2.2 external-controller — API 地址（管理入口）

```yaml
external-controller: '127.0.0.1:9090'
```

RESTful API 监听地址，ClashManager 的全部操作（读节点/测延迟/切换）都
通过它完成。安全上**务必只监听 127.0.0.1**，除非你明确知道自己在做什么：

- 本地客户端：`127.0.0.1:9090`；
- Docker 部署：容器内监听 `0.0.0.0:9090`（否则容器外访问不到），
  再通过端口映射只暴露给宿主机 127.0.0.1:9099 —— **不要直接把 9090
  映射到公网**。

对应本项目配置项 `api_url` / 环境变量 `CLASH_API_URL`。

### 2.3 secret — API 密钥

```yaml
secret: "your-secret"
```

API 的 Bearer 认证密钥。**必须与 `.env` 里的 `CLASH_SECRET` 一致**，
否则所有 API 请求 401。不写 secret 则 API 裸奔（仅限纯本机且无其他
本机程序乱来的场景）。

### 2.4 proxies — 节点列表（订阅的核心）

```yaml
proxies:
  - name: '香港-01'
    type: ss
    server: hk1.example.com
    port: 443
    cipher: aes-256-gcm
    password: 'xxxx'

  - name: '日本-01'
    type: vless
    server: jp1.example.com
    port: 443
    uuid: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
    udp: true
    tls: true
    servername: jp1.example.com
    network: ws
    ws-opts:
      path: /path
      headers:
        Host: jp1.example.com
```

- `name` 是节点的**唯一标识**（ClashManager 所有操作都按 name 进行）；
- `type` 支持 ss / ssr / vmess / vless / trojan / hysteria2 / tuic 等；
- 其余字段因协议而异，直接从订阅里整段复制即可，**不用理解每个字段**。

ClashManager 只认 `Shadowsocks / Vmess / Trojan / Socks5 / Vless / Http /
Snell / Wireguard` 这些类型（以及 Unknown），其他类型会被自动过滤。

### 2.5 proxy-groups — 代理组（ClashManager 操作的对象）

```yaml
proxy-groups:
  - name: '🚀节点选择'
    type: select        # 手动选择模式
    proxies:
      - '香港-01'
      - '日本-01'
```

- ClashManager 只操作 **`type: select`** 的组（通过 `PUT /proxies/{组名}`
  指定当前节点）；
- `name` 对应本项目配置项 `group_name`；填错也没关系，会自动发现第一个
  select 组；
- **订阅里的节点一般都挂在某个 select 组下**（机场生成的配置就是如此），
  所以通常不需要自己写这一段——整段粘贴订阅即可。

> 如果你想自定义：新建一个 `type: select` 的组，把想纳入管理的节点名
> 全部列进 `proxies`，再把 `group_name` 指向它。这可以做到「只轮换
> 质量好的那批节点」。

### 2.6 rules — 分流规则

```yaml
rules:
  - GEOIP,CN,DIRECT       # 国内直连
  - MATCH,🚀节点选择       # 其余走节点组
```

与管理节点无关，但有一条**非常关键**：最终走代理的流量必须来自你管理的
select 组（上面例子中 `MATCH` 指向 `🚀节点选择`）。如果订阅的规则把
流量导向了 `url-test` / `fallback` 自动组，那么 ClashManager 切换 select
组就不会影响实际出口——**这是最常见的「切换了但出口没变」原因**。

检查方法：`clash_cli.py switch` 后跑 `clash_cli.py test`，看出口是否
真正变化。

---

## 3. 把订阅放进 config.yaml 的三种方式

### 方式 A：整段粘贴（最简单）

机场订阅通常是一份完整的 Clash 配置。打开它，把 `proxies:`、`proxy-groups:`
、`rules:` 三段复制进 `config/config.yaml`，保留文件头的
`mixed-port / external-controller / secret` 三段（这三段属于部署环境，
不要被订阅覆盖）。

> 订阅里的 `external-controller` 通常是 `0.0.0.0:9090` 且没有 secret，
> 粘贴时务必把它改回 `127.0.0.1:9090`（或按第 2.2 节处理 Docker 场景）
> 并加上你自己的 secret。

### 方式 B：订阅转换工具

订阅可能是 base64 文本（v2ray 链接合集），用转换工具生成 Clash 配置：

- [subconverter](https://github.com/tindy2013/subconverter)（自建）
- 机场官网一般也提供「一键导入 Clash」按钮，直接下载 YAML

生成后再按方式 A 处理。

### 方式 C：sync_config.sh（Docker 场景推荐）

```bash
# 把任何来源的完整 Clash 配置同步进 config/ 并自动适配 Docker
CLASH_CONFIG_SOURCE=/path/to/订阅.yaml ./scripts/sync_config.sh your-secret
```

脚本自动完成三件事：复制 → `external-controller` 改为 `0.0.0.0:9090` +
`allow-lan: true` → 注入 secret。之后 `docker compose restart` 生效。

---

## 4. 本地客户端 vs Docker 的配置差异

| 字段 | 本地客户端（Clash Verge 等） | Docker（本项目 compose） |
|------|------------------------------|--------------------------|
| `external-controller` | `127.0.0.1:9090`（客户端默认） | `0.0.0.0:9090`（容器内） |
| `allow-lan` | 开 | 开（容器外需访问） |
| `secret` | 客户端设置里配置 | config.yaml 里写 |
| API 访问地址（爬虫侧） | `http://127.0.0.1:9090` | `http://127.0.0.1:9099` |
| 代理访问地址（爬虫侧） | `127.0.0.1:7897` | `127.0.0.1:7777` |

---

## 5. 常见坑

| 现象 | 原因 | 解决 |
|------|------|------|
| API 返回 401 | secret 与 `.env` 不一致 | 两边统一；`clash_cli.py test` 验证 |
| `切换成功但出口没变` | **① Clash 处于 rule 模式**，流量按规则链走，切策略组是 no-op | 切到 global 模式（`mode: global`），或设 `force_global_mode: true` 让库里自动切 |
| 同上 | **② 规则把流量导向了 url-test/fallback 组** | 让 `MATCH` 规则指向你管理的 select 组 |
| 同上 | **③ keep-alive 复用旧连接** | 切换后重新建连（新 Session / `Connection: close` / 页面 reload） |
| 节点列表为空 | 订阅粘贴不完整 / 节点类型全被过滤 | `clash_cli.py list` 看输出；确认 proxies 段存在 |
| 组名自动变了 | group_name 填错被自动发现替换 | 正常行为；用 `clash_cli.py status` 看实际组名 |
| Docker 里 API 连不上 | external-controller 还是 127.0.0.1 | 容器内必须 `0.0.0.0:9090`（sync_config.sh 已处理） |
| 测延迟全失败 | 订阅节点挂了 / DNS 没配 | 先 curl 直连节点服务器试试；检查 dns 段 |

> 上面三条「切换成功但出口没变」的共同点是**都很静默**：`PUT` 返回 204、
> 策略组的 `now` 也确实变了、目标站也访问得通 —— 因为目标站本来就能访问，
> 只是走的还是老出口。`verify_exit_ip`（默认开）会用实测出口 IP 把它们揪出来。

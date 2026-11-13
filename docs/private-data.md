# 私有数据隔离：在公开仓库里安全地使用真实节点

本仓库是**公开项目**，但它的使用者（包括作者本人）需要一份**真实订阅**才能跑起来。
真实订阅、节点列表、API 密钥、状态文件都属于私有数据，**一旦进入 git 历史就等于公开**。

本文说明本项目的隔离方案：私有数据放哪里、`.gitignore` 怎么兜底、提交前如何自动拦截，
以及不小心泄露后怎么处理。

> 一句话原则：**真实数据只放本地私有目录；仓库里只保留占位模板。**

---

## 1. 哪些东西属于私有数据

| 数据 | 常见文件 | 泄露后果 |
|---|---|---|
| 机场订阅配置 | `config/config.yaml`、`sub*.yaml`、`*.clash.yaml` | 别人可直接消耗你的流量 |
| 多订阅来源列表 | `local/subscriptions.txt` | 订阅 token 泄露即被白嫖；别人高频拉取还可能连累你的账号被机场判定滥用而封禁 |
| 订阅拉取缓存 / 手动文件 | `local/subscriptions/cache/`、`manual/` | 含真实节点凭据 |
| 合并结果 / 专用实例配置 | `config/config.yaml`、`local/mihomo/config.yaml` | 含全部节点与 API secret |
| 静态节点列表 | `proxies.txt`、`nodes.txt` | 节点 / 账号被白嫖 |
| API 密钥 | `.env` 里的 `CLASH_SECRET` | 可操作你的 Clash 实例 |
| 节点状态 | `clash_node_state.json`、`.clash_priority.json` | 暴露节点名、使用习惯 |
| 出口 IP 日志 | `*exit_ip*.tsv` | 可定位到具体线路 |
| 本地部署路径 | `docker-compose.override.yml` | 暴露本机目录结构 |

以下文件是**公开模板**，可以入库，但只能包含占位数据：

- `config/config.example.yaml`
- `.env.example`
- `config/README.md`

---

## 2. 三层隔离方案（按强度递增）

### 2.1 默认层：`config/` 白名单

`.gitignore` 采用**白名单**而不是黑名单：

```gitignore
config/*
!config/README.md
!config/config.example.yaml
```

这意味着：**以后往 `config/` 里放任何新文件都会自动被忽略**，不需要每次改 `.gitignore`。
真实订阅照旧放在 `config/config.yaml`（Docker 部署的标准位置）：

```bash
cp config/config.example.yaml config/config.yaml
# 编辑 config/config.yaml，填入真实节点
```

静态代理列表也可以放在 `config/` 下，例如 `config/proxies.txt`，同样自动被忽略。

### 2.2 推荐层：`local/` 私有目录

仓库根目录下的 `local/`（以及 `private/`、`secrets/`）整个目录都被忽略。
把**所有非 Docker 必需**的私有数据都放这里，最省心：

```text
clash_proxy/
├── config/
│   ├── config.example.yaml   # 公开模板（已跟踪）
│   └── config.yaml           # 真实订阅（被忽略）
├── local/                    # 整个目录被忽略
│   ├── subscriptions.txt     # 多订阅来源列表（含 token）
│   ├── subscriptions/        # 拉取缓存 / 手动文件 / 合并结果
│   ├── mihomo/               # 专用实例配置 + 地理数据库
│   ├── subscription.yaml     # 原始订阅备份
│   ├── proxies.txt           # 静态节点列表
│   ├── state/                # 状态文件
│   └── clash_exit_ips.tsv    # 出口 IP 日志
├── .env                      # 被忽略
└── ...
```

`.env` 示例（相对路径按项目根目录解析）：

```bash
CLASH_SECRET=你的强随机密钥
CLASH_STATIC_PROXIES=local/proxies.txt
CLASH_EXIT_IP_LOG=local/clash_exit_ips.tsv
```

Python 里指定状态 / 优先度文件同理：

```python
manager = ClashManager({
    "post_switch_test_url": "https://你的目标站/",
    "state_file": "local/state/clash_node_state.json",
    "policy": {"priority_file": "local/state/.clash_priority.json"},
})
```

> 不指定时默认文件（`clash_node_state.json`、`.clash_priority.json`）也在 `.gitignore` 里，
> 直接放在项目根目录同样安全。

### 2.3 最强层：数据完全放在仓库外

如果连「误操作 `git add -f local/`」都不放心，把私有数据放到仓库外的目录：

```bash
# .env 用绝对路径
CLASH_STATIC_PROXIES=C:/Users/你/clash_private/proxies.txt
CLASH_EXIT_IP_LOG=C:/Users/你/clash_private/clash_exit_ips.tsv
```

Docker 部署时，不要再改被跟踪的 `docker-compose.yml`，而是新建
**`docker-compose.override.yml`**（Compose 会自动合并，且已被 `.gitignore` 忽略）：

```yaml
# docker-compose.override.yml（本地文件，不入库）
services:
  clash:
    volumes:
      - "C:/Users/你/clash_private:/root/.config/mihomo"
```

这样真实配置完全不落在仓库工作区内。

---

## 3. 提交前防线：pre-commit 钩子

`.gitignore` 只对**未跟踪**文件生效，`git add -f` 可以绕过它。所以再加一层**提交前检查**：

```bash
# 每个克隆执行一次（只改本地 git 配置，不会入库）
sh scripts/install_git_hooks.sh

# 等价于：
git config core.hooksPath .githooks
```

之后每次 `git commit` 都会自动执行 `scripts/check_privacy.py --staged`，检查三层：

1. **路径规则**：真实配置 / 订阅 / 状态 / 私钥等文件名直接拒绝；
2. **ignore 规则**：被 `.gitignore` 忽略却出现在暂存区（通常 = `git add -f`）；
3. **内容规则**：代理 URI（`ss://` / `vmess://` / `vless://` / `trojan://` /
   `hysteria2://` / `tuic://`…）、带账号密码的 HTTP(S) 代理 URL、
   静态列表行（`host:port:user:pass`）、真实 `password` / `secret` / `uuid` /
   Bearer token、订阅链接 token、私钥内容。

手动检查（不提交也能跑）：

```bash
python scripts/check_privacy.py --staged   # 暂存区（默认）
python scripts/check_privacy.py --all      # 全部已跟踪文件
python scripts/check_privacy.py --paths local/proxies.txt   # 单文件调试
```

钩子失败时会列出文件与原因并阻止提交。`git commit --no-verify` 可以跳过，
但**除非你逐项确认过，否则不要用**。

> 钩子只装在本地，不会随仓库分发。换机器 / 重新克隆后记得再执行一次
> `git config core.hooksPath .githooks`。

---

## 4. 自检：确认私有文件真的被忽略

```bash
# 1. 私有路径应当命中忽略规则（输出 .gitignore:行号:规则）
git check-ignore -v config/config.yaml local/proxies.txt .env
# 预期：各输出一行，说明被忽略

# 2. 公开模板不应被忽略（应当没有输出）
git check-ignore -v config/config.example.yaml .env.example

# 3. 查看所有被忽略的文件（确认私有数据都在里面）
git status --ignored --short | grep -E "config/|local/|\.env"

# 4. 确认仓库跟踪列表里没有私有文件（应当没有输出）
git ls-files | grep -Ei "config/config\.yaml|^local/|^private/|\.env$|proxies.*\.txt|nodes.*\.txt"
```

正常结果：

```text
$ git check-ignore -v config/config.yaml local/proxies.txt .env
.gitignore:XX:config/*          config/config.yaml
.gitignore:XX:local/            local/proxies.txt
.gitignore:XX:.env              .env
```

---

## 5. 如果不小心提交了

**第一优先级是轮换凭证，而不是清理历史。**

1. **立刻轮换**：重新生成订阅链接 / 更新机场密码 / 更换 `CLASH_SECRET`；
   旧凭证泄露后，清理历史也不能让它重新变安全。
2. **还没 push**：
   ```bash
   git rm --cached config/config.yaml
   git commit --amend
   ```
3. **已经 push 到公开仓库**：
   - 把仓库临时设为 private；
   - 用 `git filter-repo` 重写历史并强制推送；
   - 联系 GitHub Support 清理缓存视图 / fork；
   - **假设它已经泄露**，完成第 1 步的轮换。
4. **排查历史中是否还有其它泄露**：
   ```bash
   git log --all -p | grep -nE "(ss|vmess|vless|trojan|hysteria2|tuic)://"
   git log --all -p -S "CLASH_SECRET"
   ```

---

## 6. 常见误区

- **「加到 .gitignore 就安全了」** —— 不，`git add -f` 可以绕过；钩子才是最后防线。
- **「删掉文件就没事了」** —— 不，历史里还在；公开仓库的 fork / 缓存也可能还在。
- **「私有目录里的文件可以随便 stage」** —— 不，`local/` 只是被忽略，不是加密。
- **「日志 / 截图不算敏感」** —— 节点名、出口 IP、订阅域名都可能定位到你的账号。
- **「示例配置里可以放真实数据」** —— `config.example.yaml` 是公开文件，只能放占位符。

---

## 相关文件

- [`.gitignore`](../.gitignore) —— 忽略规则（白名单式 `config/*`）
- [`scripts/check_privacy.py`](../scripts/check_privacy.py) —— 三层隐私检查
- [`.githooks/pre-commit`](../.githooks/pre-commit) —— 提交前钩子
- [`scripts/install_git_hooks.sh`](../scripts/install_git_hooks.sh) —— 一键启用钩子
- [`.gitattributes`](../.gitattributes) —— 保证钩子脚本在 Windows 上也是 LF
- [`SECURITY.md`](../SECURITY.md) —— 安全边界与漏洞报告
- [`config/README.md`](../config/README.md) —— `config/` 目录说明
- [`subscriptions.md`](subscriptions.md) —— 多订阅合并与定期刷新

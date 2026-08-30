# ClashManager 逻辑介绍

本文介绍 `clash_manager.py` 的内部工作机制，帮助你理解为什么节点切换
可靠、以及出问题时如何排查。适合想在源码基础上二次开发的读者。

---

## 1. 整体数据流

```
                    ┌──────────────────────────────────────────┐
                    │              爬虫进程                     │
                    │                                          │
  url_list ──────►  │  Spider ──代理流量(127.0.0.1:7777)────┐   │
                    │     │                                  │   │
                    │     │ 上报成功/403/异常                 │   │
                    │     ▼                                  ▼   │
                    │  ClashManager ◄──RESTful API 直连── Mihomo │
                    │     │                    (读节点/测延迟/PUT) │
                    │     ▼                                      │
                    │  本地状态 clash_node_state.json            │
                    │  可选 Redis 错误黑名单(跨进程)              │
                    └──────────────────────────────────────────┘
```

要点：

- **代理流量与管理流量分离**：爬虫请求走 7777 代理端口；ClashManager
  通过 API 端口直连 Mihomo（内部会临时禁用环境代理变量，避免「用代理
  访问代理自己的 API」导致 502）。
- **ClashManager 从不代理任何请求**：它只负责「决定当前用哪个节点」。
  爬虫的每个请求经 Mihomo 出口时，用的就是当前策略组选中的节点。

---

## 2. 启动流程

`ClashManager(config)` 初始化时依次做：

```text
load_state()          从 clash_node_state.json 恢复: 当前节点索引、下载计数、
                      per-node 统计、已验证节点池
        │
        ▼
refresh_nodes()       经 API 读取策略组节点列表:
                      过滤订阅元数据(剩余流量/到期等) → 过滤嵌套策略组
                      → 过滤不支持类型 → 过滤错误黑名单
        │
        ▼
refresh_node_health() 线程池并发测全部节点延迟, 标记 healthy/unhealthy
```

失败不抛异常：API 连不上只是打日志，爬虫代码可以照常运行
（`enable_node_switching=False` 时可完全跳过）。

---

## 3. 节点生命周期

```mermaid
stateDiagram-v2
    [*] --> 发现: refresh_nodes
    发现 --> 健康: 延迟 > 0
    发现 --> 不健康: 延迟失败
    不健康 --> 健康: 下次刷新通过
    健康 --> 已验证: 切换时通过双重验证
    已验证 --> 轮换池: 加入 verified_nodes
    轮换池 --> 拉黑: 切换时验证失败 / 被 record_now 记录
    拉黑 --> [*]: 从 available_nodes 排除
```

两个核心集合：

| 集合 | 存储位置 | 含义 |
|------|----------|------|
| `verified_nodes` | clash_node_state.json | 曾经通过「双重验证」的节点名，优先轮换池 |
| `error_node_set`（黑名单） | Redis(可选)/内存 | 被目标站封过的节点名，refresh_nodes 时直接排除 |

---

## 4. 切换流程（核心状态机）

`switch_node()` → `_perform_switch()` 的完整流程：

```text
触发切换(手动/403计数/下载计数/健康守护)
  │
  ├─ 1. 选定候选列表
  │     有已验证节点 → 按 switch_strategy 排序 verified 池     (mode=verified_only)
  │     无已验证节点 → 全量扫描构建验证池                       (mode=build_pool)
  │
  ├─ 2. 逐个尝试候选 (最多 post_switch_max_retries 个)
  │     │
  │     ├─ 预检: API 测候选节点延迟, 测不通直接跳过 ⏭️
  │     │        (pre_switch_delay_check, 默认开)
  │     │
  │     ├─ PUT /proxies/{group} {"name": 候选}   → 204 只是"收到指令"
  │     │
  │     ├─ 验证1: 轮询 GET /proxies/{group}, now == 候选?   (最多3次)
  │     │
  │     └─ 验证2: 经代理出口真实请求 post_switch_test_url
  │              状态码 ∈ {200,204}?  (最多 post_switch_max_retries 次)
  │              │
  │              ├─ 通过 → 加入 verified_nodes, 重置计数, 保存状态, 返回成功 ✅
  │              └─ 失败 → 从 verified_nodes 移除, 尝试下一个候选 ⏭️
  │
  └─ 全部失败 → 返回 False(爬虫自行决定重试/暂停)
```

**为什么需要验证2（双重验证）**：`PUT` 返回 204 只代表 Mihomo 接受了
指令；节点可能欠费、被墙、线路故障。只有真实发出请求并拿到 200/204，
才能确认「这个节点此刻能访问你的目标站」。这是生产环境最重要的经验。

**预检的意义**：每个候选节点验证失败要花 `3s 等待 + 最多3次轮询`，
如果候选本身延迟就测不通，预检能直接跳过，避免几十秒的无谓等待。

---

## 5. 四种切换触发方式

| 触发 | 调用 | 条件 | 典型场景 |
|------|------|------|----------|
| 手动 | `switch_node()` / `switch_to_healthy_node()` | 立即 | CLI、任务开始前主动换 |
| 403 计数 | `increment_403_error()` | 累计 `max_403_errors`(默认3)次 | 目标站封 IP |
| 下载计数 | `increment_download_count()` | 单节点满 `max_downloads_per_node`(默认500) | 主动轮换摊流量 |
| 健康守护 | `start_auto_health_check()` 后台线程 | 每 interval 秒出口检测失败 | 长时间运行自愈 |

注意 403 计数是「连续」的：一次下载成功就会 `reset_403_error()` 清零，
避免历史 403 累积误触发。

---

## 6. 负载均衡策略

候选列表排序逻辑（`_order_by_strategy`）：

| 策略 | 行为 | 依赖数据 |
|------|------|----------|
| `round_robin` | 从当前节点之后顺序轮转 | 无 |
| `random` | 随机打乱 | 无 |
| `least_403` | 403 历史最少的节点优先 | `node_403_counts` |
| `least_used` | 下载次数最少的节点优先 | `node_download_counts` |

per-node 统计在 `increment_*` 时累计，随 `save_state()` 持久化，重启不丢。
用 `clash_cli.py stats` 可以直观看到各节点负载。

> 策略只在「候选列表」内排序。默认 `only_use_verified_nodes=True` 时，
> 候选列表是已验证节点池——即先保证「能用」，再谈「均衡」。

---

## 7. 错误黑名单（按站点隔离）

```python
web_loc = urlparse(self.post_switch_test_url).netloc.replace(".", "_")
self.error_node_set = _ErrorNodeStore(f"clash_error_node_set_{web_loc}")
```

- 黑名单 key 带目标站域名：**A 站封的节点不影响 B 站**；
- 这就是为什么建议 `post_switch_test_url` 填实际采集的目标站；
- 存储优先 Redis（跨进程共享），不可用时降级为进程内内存 set；
- `switch_to_healthy_node(record_now=True)` 会把当前节点记入黑名单。

**误杀保护**：`refresh_nodes()` 时如果发现黑名单数量 ≈ 节点总数
（差 < 5），说明目标站可能大面积封禁，继续过滤就没有节点可用了，
此时自动清空黑名单并走全量扫描重建。

---

## 8. 线程安全设计

多线程爬虫（ThreadPoolExecutor 等）可以共享同一个 ClashManager 实例：

- `switch_node()` 用 `threading.Condition` 串行化：并发触发切换时，
  后来的线程会阻塞等待前一次完成，不会出现「抢着 PUT」的竞争；
- `increment_*` / `save_state` 只改内存和文件，无共享可变状态竞争；
- 切换期间其他线程的请求会走**旧节点直到 PUT 生效**，随后自动走新
  节点——请求侧无需感知切换，偶发失败重试即可；
- 若需要「等切换完成后才能发请求」的严格场景，调用
  `wait_for_switch_complete()`。

多进程场景：每个进程各建一个 ClashManager 实例；黑名单经 Redis 共享，
`clash_node_state.json` 建议按进程分开（`state_file` 可配置）。

---

## 9. 与爬虫代码的协作约定

ClashManager 是「被动的」：它不会拦截你的请求，需要爬虫在上报点调用
计数方法。三个上报点缺一不可：

```python
resp = requests.get(url, proxies=manager.get_proxy_config())

if resp.status_code == 403:
    manager.increment_403_error()          # 上报点①: 被目标站封
elif resp.status_code == 200:
    manager.increment_download_count()     # 上报点②: 成功计数(负载均衡)
    ...
else:
    manager.switch_to_healthy_node()       # 上报点③: 节点本身故障(连接失败/502等)
```

三种 HTTP 库（requests / curl_cffi / aiohttp）的具体搭配写法见
[docs/http-clients.md](http-clients.md)。

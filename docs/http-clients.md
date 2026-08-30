# 代理搭配 HTTP 库用法大全

ClashManager 只负责「把当前节点定下来」，请求怎么发由你选的 HTTP 库决定。
本文给出 **requests / curl_cffi / aiohttp** 三种库与代理池的完整搭配写法。

**通用原则**：

1. 所有请求都带 `manager.get_proxy_config()` 提供的代理；
2. 三个上报点必须写全（403 → 成功 → 节点故障），否则轮换逻辑不生效：

| 情况 | 调用 |
|------|------|
| 目标站返回 403 | `manager.increment_403_error()`（累计 3 次自动切换） |
| 请求成功 | `manager.increment_download_count()`（负载均衡计数） |
| 连接失败/502/超时（节点故障而非被封） | `manager.switch_to_healthy_node(record_now=True)` |

---

## 1. requests（同步，最简单）

```python
import requests
from clash_manager import ClashManager

manager = ClashManager({
    "api_url": "http://127.0.0.1:9099",
    "secret": "your-secret",
    "post_switch_test_url": "https://目标网站/",
})
proxies = manager.get_proxy_config()   # {"http": "http://127.0.0.1:7777", "https": "..."}

for url in url_list:
    try:
        resp = requests.get(url, proxies=proxies, timeout=30)
    except requests.RequestException:
        manager.switch_to_healthy_node(record_now=True)  # 节点故障
        continue

    if resp.status_code == 403:
        manager.increment_403_error()
    elif resp.status_code == 200:
        manager.increment_download_count()
        save(resp.content)
```

注意：`proxies` dict 是启动时取一次即可（代理地址不变，变的是节点出口）。

---

## 2. curl_cffi（同步，伪装 TLS 指纹）

目标站做 TLS 指纹检测（curl/requests 被拒、浏览器能开）时用 curl_cffi，
`impersonate` 伪装成 Chrome 等浏览器的 TLS 指纹。用法与 requests 几乎一致：

```bash
pip install curl_cffi
```

```python
from curl_cffi import requests as cffi_requests
from clash_manager import ClashManager

manager = ClashManager({...})
proxies = manager.get_proxy_config()

for url in url_list:
    try:
        resp = cffi_requests.get(
            url,
            proxies=proxies,                # 同样支持 requests 风格 dict
            impersonate="chrome110",        # TLS 指纹伪装
            timeout=30,
        )
    except Exception:
        manager.switch_to_healthy_node(record_now=True)
        continue

    if resp.status_code == 403:
        manager.increment_403_error()
    elif resp.status_code == 200:
        manager.increment_download_count()
        save(resp.content)
```

curl_cffi 支持 requests 的全部姿势（session 复用、headers、cookies 等）：

```python
session = cffi_requests.Session(impersonate="chrome110")
resp = session.get(url, proxies=proxies, headers=headers)
```

---

## 3. aiohttp（异步，高并发）

异步采集用 aiohttp。与 requests 最大的区别：**proxy 参数是字符串**，
且默认会读环境变量里的代理（用 `trust_env=False` 关掉，避免被全局
代理变量干扰）。

```bash
pip install aiohttp
```

```python
import asyncio
import aiohttp
from clash_manager import ClashManager

manager = ClashManager({...})
PROXY = "http://127.0.0.1:7777"   # aiohttp 用字符串，http/https 都走这一个

async def fetch(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 403:
                manager.increment_403_error()
                return None
            if resp.status == 200:
                manager.increment_download_count()
                return await resp.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        manager.switch_to_healthy_node(record_now=True)   # 节点故障
    return None

async def main(url_list):
    # trust_env=False: 只走显式代理，忽略系统代理环境变量
    async with aiohttp.ClientSession(proxy=PROXY, trust_env=False) as session:
        tasks = [fetch(session, url) for url in url_list]
        return await asyncio.gather(*tasks)

results = asyncio.run(main(url_list))
```

要点：

- `ClientSession(proxy=...)` 全局设置；也可单请求 `session.get(url, proxy=PROXY)`；
- 并发限制：`asyncio.Semaphore(10)` 包在 fetch 里控制同时请求数；
- `trust_env=False` 是**必须的**，否则 aiohttp 会优先用 `HTTP_PROXY`
  环境变量（可能指向别的代理或造成循环代理）。

---

## 4. curl_cffi 异步版（AsyncSession）

既要 TLS 指纹伪装又要异步并发：

```python
from curl_cffi.requests import AsyncSession

async def main(url_list):
    # proxies 支持 requests 风格 dict
    async with AsyncSession(
        proxies=manager.get_proxy_config(),
        impersonate="chrome110",
    ) as session:
        tasks = [session.get(url, timeout=30) for url in url_list]
        return await asyncio.gather(*tasks, return_exceptions=True)
```

---

## 5. 同步多线程（requests + ThreadPoolExecutor）

requests 本身是阻塞的，用线程池提速。ClashManager 线程安全，所有线程
共享一个实例：

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

manager = ClashManager({...})
proxies = manager.get_proxy_config()

def fetch_one(url):
    try:
        resp = requests.get(url, proxies=proxies, timeout=30)
    except requests.RequestException:
        manager.switch_to_healthy_node(record_now=True)
        return None
    if resp.status_code == 403:
        manager.increment_403_error()
        return None
    if resp.status_code == 200:
        manager.increment_download_count()
        return resp.content
    return None

with ThreadPoolExecutor(max_workers=8) as pool:
    futures = [pool.submit(fetch_one, url) for url in url_list]
    for future in as_completed(futures):
        data = future.result()
        if data:
            save(data)
```

**切换期间的请求**：某个线程触发切换（`increment_403_error` 攒满 3 次）
时，其他线程的请求仍走旧节点直到 PUT 生效，期间可能继续 403——这是
预期行为，重试即可。若想严格「切换完成后再发请求」：

```python
manager.wait_for_switch_complete()   # 阻塞到切换结束
```

---

## 6. 各库选型建议

| 场景 | 选择 | 理由 |
|------|------|------|
| 目标站无 TLS 检测、量小 | requests | 最简单，生态最熟 |
| 目标站检测 TLS 指纹（Java/Go 指纹被拒） | curl_cffi | `impersonate` 一行伪装 |
| 吞吐优先、需要异步 | aiohttp | 纯异步最快 |
| 既要 TLS 伪装又要异步 | curl_cffi.AsyncSession | 两者兼得 |
| 线程模型简单化、代码从 requests 迁移 | ThreadPoolExecutor + requests | 改动最小 |

**切换策略建议**：TLS 检测严格、爱封 IP 的目标站，配
`switch_strategy="least_403"` + 偏小的 `max_downloads_per_node`，
让每个节点承担尽量少的请求。

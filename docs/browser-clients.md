# 代理搭配浏览器用法大全（Playwright / Selenium）

目标站有 JS 渲染、验证码、或做 TLS/指纹检测时，HTTP 库不够用，得上真浏览器。
本文给出 **Playwright / Selenium** 与代理池的完整搭配写法，并附上实战踩坑。

**核心差异（与 HTTP 库相比）**：

| | HTTP 库 | 浏览器 |
|--|---------|--------|
| 代理设置时机 | 每次请求传 `proxies=` | **启动时一次性固定**（`--proxy-server=...`） |
| 切换节点后 | 下一次请求立即走新节点 | **已建立的连接仍走旧节点**，必须制造新连接 |
| 生效方式 | 自动 | `page.reload()` / 新开 Tab / 新开 context |

**通用原则**：

1. 浏览器启动参数里的代理 = `manager.get_proxy_config()` 的地址（**地址不变，变的是节点出口**）；
2. 切换节点后必须**重载页面**（或新开页签）让请求走新节点；
3. 三个上报点照旧要写全（403 → 成功 → 节点故障）。

---

## 1. Playwright（异步，推荐）

```bash
pip install playwright
playwright install chromium
```

```python
import asyncio
from playwright.async_api import async_playwright
from clash_manager import ClashManager

manager = ClashManager({
    "api_url": "http://127.0.0.1:9097",
    "secret": "your-secret",
    "proxy_port": 7897,
    "post_switch_test_url": "https://目标网站/",
})

PROXY = manager.get_proxy_config()["http"]      # http://127.0.0.1:7897


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,                      # 见第 4 节：被检测时 headless 是第一嫌疑
            proxy={"server": PROXY},             # ★ 浏览器全部流量走代理池
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(locale="zh-CN")
        page = await ctx.new_page()

        # ① 拦截响应，自动上报 403 / 成功
        async def on_response(resp):
            if resp.request.resource_type not in ("document", "xhr", "fetch"):
                return
            if resp.status == 403:
                manager.report_verdict("blocked")    # 攒满 max_403_errors 次自动换节点
            elif resp.status == 200:
                manager.report_verdict("ok")

        page.on("response", on_response)

        while True:
            try:
                await page.goto(TARGET_URL, wait_until="load", timeout=30000)
            except Exception:
                manager.report_verdict("dead")   # 节点故障
                continue

            if await extract(page):
                break

            # ② 换节点后必须重载页面 —— 已建立的连接仍走旧节点！
            manager.report_verdict("dead")
            await asyncio.sleep(1)

        await browser.close()


asyncio.run(main())
```

---

## 2. Playwright（同步版）

```python
from playwright.sync_api import sync_playwright

PROXY = manager.get_proxy_config()["http"]

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        proxy={"server": PROXY},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = browser.new_context(locale="zh-CN").new_page()

    def on_response(resp):
        if resp.status == 403:
            manager.report_verdict("blocked")
        elif resp.status == 200:
            manager.report_verdict("ok")

    page.on("response", on_response)

    while True:
        try:
            page.goto(TARGET_URL, wait_until="load", timeout=30000)
        except Exception:
            manager.report_verdict("dead")
            continue
        if extract(page):
            break
        manager.report_verdict("dead")
        page.reload()                      # ★ 新连接才走新节点

    browser.close()
```

---

## 3. Selenium

Selenium 的代理同样是**启动时固定**，写法：

```python
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

opts = Options()
opts.add_argument(f"--proxy-server={manager.get_proxy_config()['http']}")
opts.add_argument("--disable-blink-features=AutomationControlled")
opts.add_experimental_option("excludeSwitches", ["enable-automation"])
opts.add_experimental_option("useAutomationExtension", False)

driver = webdriver.Chrome(options=opts)
driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
    "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
})

while True:
    driver.get(TARGET_URL)
    if extract(driver):
        break
    manager.switch_to_healthy_node()       # 换个出口再试
    driver.refresh()                       # ★ 新连接才走新节点
```

> Selenium 4 也可以用 `--proxy-server` + CDP，但 Playwright 对「代理 / 新连接 / 响应事件」
> 的支持更直接，本文推荐 Playwright。

---

## 4. 实战踩坑（真实项目沉淀）

### 4.1 切换节点后必须制造新连接 ★ 最大的坑

HTTP keep-alive 会把到目标站的 TCP/TLS 连接**复用**，而 mihomo 是**按连接**选出口的——
**切换节点后，旧连接上的后续请求仍然走旧节点**，表现为「切了节点但 IP 没变」。

```python
manager.switch_to_healthy_node()   # 先换出口
page.reload()                      # ✅ 重载 → 新连接 → 新节点
# 或  await ctx.new_page()         # 新页签同样有效
```

验证出口 IP 的办法：用 `manager.get_exit_ip()`（内部每次都新建连接），或
`page.request.get("https://api.ipify.org")`。**别复用连接**——包括别复用 `requests.Session`。

库内部在切换前后也会各查一次出口 IP 比对（`verify_exit_ip`，默认开）：
若切换后出口 IP 没变，会直接报错并提示常见原因（rule 模式 / keep-alive），
而不是静默放过。少数机场多节点共用同一出口 IP 时设 `allow_same_exit_ip: true`。

### 4.2 headless 是第一个嫌疑

实测同一 IP、同一 UA：**headless → 目标站返回风控错误；headed → 正常**。
目标站对无头浏览器的检测越来越准（`navigator.webdriver`、渲染差异、字体等）。

**优先用有头模式**；必须在服务器上跑无头时，先评估目标站是否检测（`--headless=new` 好于老版）。

### 4.3 用真实 Chrome，别用内置 Chromium

Playwright 自带的 Chromium 与用户真实 Chrome 存在可检测差异。
Playwright 支持直接驱动本机已安装的 Chrome / Edge：

```python
browser = await p.chromium.launch(channel="chrome", ...)   # 真实 Chrome
# 或 channel="msedge"
```

实测同一节点、同一配置：**内置 Chromium → 被风控拦截；真实 Chrome → 正常**。

### 4.4 不要伪造 UA（用真实浏览器时）

用真实 Chrome 时，`User-Agent` 与 `Sec-CH-UA` 客户端提示头、内核版本必须**自洽**。
把 UA 改成老版本（如 UA 写 Chrome/150 而内核是 154）会因版本不一致暴露自动化特征。

```python
# ❌ ctx = browser.new_context(user_agent="...Chrome/150...")   # 与真实内核版本不符
# ✅ 什么都不设，用浏览器原生 UA
```

### 4.5 代理地址不要写进 `requests` 的环境变量

ClashManager 内部已处理；你自己的脚本里用 `requests` 时记得
`session.trust_env = False`，避免系统代理环境变量叠加。

### 4.6 稳定的浏览器复用策略

浏览器**不要每轮重开**（启动开销 10-30 秒，且频繁开关窗口本身就是异常特征）：

| 做法 | 说明 |
|------|------|
| 保持单窗口常驻 | 一轮任务只在最初启动一次浏览器 |
| 取新页面用「页面内的刷新/换一批」按钮 | 不重载整页、不重开窗口 |
| 换节点只 `reload()` | 见 4.1，不重建浏览器 |

---

## 5. 反检测最小清单

```python
browser = await p.chromium.launch(
    headless=False,                       # ① 有头
    channel="chrome",                     # ② 真实 Chrome
    proxy={"server": PROXY},
    args=[
        "--disable-blink-features=AutomationControlled",   # ③ 去 webdriver 标志
    ],
)
ctx = await browser.new_context(
    locale="zh-CN",
    viewport={"width": 1200, "height": 800},
    # ④ 不设置 user_agent —— 原生 UA 才自洽
)
```

> 目标站风控较强（有验证码、TLS 指纹、行为检测）时，配合本机/服务器上的
> **真实住宅或机房出口**（即本项目管理的节点）一起用，效果是「真实浏览器 + 真实出口 IP」。

---

## 6. 选型建议

| 场景 | 推荐 |
|------|------|
| 纯接口、无 JS | requests / curl_cffi（见 [http-clients.md](http-clients.md)）|
| 有 JS 渲染，无风控 | Playwright（可 headless）|
| 有风控 / 验证码 | Playwright + **headed + 真实 Chrome + 真实出口** |
| 需要并发多窗口 | 多个 context（同一 browser 进程），每 context 走同一代理池出口 |

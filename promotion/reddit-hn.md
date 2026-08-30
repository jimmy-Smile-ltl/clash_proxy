# Reddit / Hacker News post (EN)

---

**Title (Reddit r/selfhosted or r/webscraping):**
Show r/selfhosted: clash_proxy — turn the unused nodes in your airport subscription into a scraping proxy pool

**Body:**

For anyone scraping overseas sites, proxy cost is the annoying part: commercial proxy pools (residential/DC IPs) are billed by traffic and cost hundreds a month, even when your task only needs a few thousand pages.

Meanwhile most of us with an "airport" subscription (dozens of nodes for a few dollars) only ever use one or two nodes — the other 90% sit idle.

So I built [clash_proxy](https://github.com/jimmy-Smile-ltl/clash_proxy): a single-file Python manager that turns those idle subscription nodes into a managed proxy pool via the Clash/Mihomo RESTful API.

What it does:

- **Node discovery** — pulls all nodes from a Clash proxy group, filters subscription metadata ("remaining traffic", "expiry"), nested groups and unsupported types
- **Dual verification** — an API 204 after `PUT /proxies/{group}` means nothing; after switching it makes a real request through the proxy egress to your target site and only then considers the switch successful
- **Load balancing** — four strategies: `round_robin`, `random`, `least_403` (fewest historical 403s), `least_used`; per-node download/403 counters are persisted
- **Auto failover** — 3 consecutive 403s trigger a switch; an optional background daemon re-checks the egress every 30s and self-heals
- **Per-site blacklists** — a node banned by site A isn't blacklisted for site B (the Redis key contains the target domain)
- **Over-kill protection** — if the blacklist approaches the total node count it auto-clears so you don't deadlock

Usage in a scraper:

```python
manager = ClashManager({
    "api_url": "http://127.0.0.1:9099",
    "post_switch_test_url": "https://your-target-site/",
    "switch_strategy": "least_403",
})
proxies = manager.get_proxy_config()
resp = requests.get(url, proxies=proxies)
if resp.status_code == 403:
    manager.increment_403_error()   # auto-switches after 3
```

Honest scope note: this is built for **light-weight scraping** (academic papers, public datasets, news) with a limited node pool — not hundreds of req/s; for that you still want a commercial pool.

Stack: one ~700-line Python file + a CLI + a Dockerized Mihomo. No framework dependencies, works with any Clash client (Clash Verge, mihomo, routers). MIT licensed. Stars, issues and PRs welcome.

---

## Posting tips

- **Reddit**: r/selfhosted (weekend "Show" flair), r/webscraping, r/Python (Showcase Saturday), r/homelab
- **HN**: post the GitHub link as a "Show HN" with the first paragraph as the story
- Keep the title short; lead with the cost-pain point

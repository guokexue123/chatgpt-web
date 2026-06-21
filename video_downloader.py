#!/usr/bin/env python3
"""
视频下载器 - 自动获取 Cloudflare Cookie 版

Cookie 获取策略（按优先级自动尝试）：
  方案 A: playwright-stealth  — 免费，无需额外服务，成功率中等
  方案 B: FlareSolverr        — 免费，需本地 Docker，成功率高
  方案 C: CapSolver API       — 付费，无需本地服务，成功率最高

获取到的 cf_clearance 会缓存到 cookie_cache.json，过期前无需重复求解。

依赖安装：
  pip install playwright playwright-stealth httpx
  playwright install firefox chromium
"""

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright, Page, BrowserContext

# ===== 用户配置 =====
TARGET_URL = "https://supjav.com/132824.html"
DOWNLOAD_DIR = Path("downloads")
COOKIE_CACHE_FILE = Path("cookie_cache.json")

# 【手动 Cookie 文件】把浏览器 F12 复制的 Cookie 字符串存入任意文件，在下面列出即可
# 支持四种格式（自动识别）：
#   格式1 纯文本 : cf_clearance=xxx; _cfuvid=xxx
#   格式2 带引号 : "cf_clearance=xxx; _cfuvid=xxx"
#   格式3 JSON数组: [{"name":"cf_clearance","value":"xxx",...}, ...]
#   格式4 脚本缓存: {"saved_at":..., "cookies":[...]}
# 脚本按列表顺序查找，找到包含 cf_clearance 的第一个文件就使用
BROWSER_COOKIE_FILES = [
    "cf_cookies.json",     # 用户自定义文件（优先）
    "cookie_cache.json",   # 脚本自动缓存文件
    "cookies.txt",         # 备用纯文本文件
]

# --- 方案 A: playwright-stealth ---
# 不需要额外配置，脚本自动处理
STEALTH_HEADLESS = True          # 改为 False 可以看到浏览器（调试用）
STEALTH_WAIT_SEC = 25            # 等待 CF 自动放行的秒数（增加到25秒）

# --- 方案 B: FlareSolverr (本地 Docker) ---
# 启动命令: docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
FLARESOLVERR_URL = "http://localhost:8191/v1"
FLARESOLVERR_TIMEOUT = 60        # 秒

# --- 方案 C: CapSolver API (付费) ---
# 注册: https://capsolver.com  充值约 $2 可解数千次
CAPSOLVER_API_KEY = ""           # 填入你的 API Key 启用此方案

# --- 通用 ---
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"
M3U8_TIMEOUT = 60
# ===== 配置结束 =====

M3U8_RE = re.compile(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', re.IGNORECASE)
DOMAIN = urlparse(TARGET_URL).netloc


# ─────────────────────────────────────────────
# Cookie 工具（解析 / 缓存 / 从文件加载）
# ─────────────────────────────────────────────

def _parse_cookie_string(cookie_str: str, domain: str) -> list[dict]:
    """把 F12 复制的 'name=value; name2=value2' 字符串解析成列表"""
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": domain,
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        })
    return cookies


def load_cookies_from_file(path: str | Path) -> list[dict] | None:
    """
    从文件加载 Cookie，自动识别三种格式：
      格式1 纯文本  : cf_clearance=xxx; _cfuvid=xxx
      格式2 JSON数组: [{"name":"cf_clearance","value":"xxx",...}, ...]
      格式3 脚本缓存: {"saved_at":..., "cookies":[...]}
    """
    p = Path(path)
    if not p.exists():
        return None
    content = p.read_text(encoding="utf-8").strip()
    if not content:
        return None

    # 先尝试 JSON 解析（覆盖格式2/3，以及格式4：JSON 编码的字符串 "xxx=yyy; ..."）
    try:
        data = json.loads(content)
        # 格式4：整个内容是被双引号包裹的 JSON 字符串，解开后当纯文本处理
        if isinstance(data, str):
            content = data          # 剥掉外层引号，继续走下面的纯文本逻辑
        # 格式3：脚本内部缓存格式 {"saved_at":..., "cookies":[...]}
        elif isinstance(data, dict) and "cookies" in data:
            remaining = 3000 - (time.time() - data.get("saved_at", 0))
            if remaining <= 0:
                print(f"  ⚠ {p.name} 中的缓存 Cookie 已过期")
                return None
            cookies = data["cookies"]
            print(f"  ✓ 从 {p.name} 加载 {len(cookies)} 个 Cookie"
                  f"（缓存格式，剩余约 {int(remaining/60)} 分钟）")
            return cookies
        # 格式2：JSON 数组
        elif isinstance(data, list):
            cookies = _normalize_cookies(data, DOMAIN)
            print(f"  ✓ 从 {p.name} 加载 {len(cookies)} 个 Cookie（JSON 数组格式）")
            return cookies
    except json.JSONDecodeError:
        pass

    # 格式1/4：纯文本 Cookie 字符串（或解包后的 JSON 字符串）
    if "=" in content:
        cookies = _parse_cookie_string(content, DOMAIN)
        if cookies:
            print(f"  ✓ 从 {p.name} 加载 {len(cookies)} 个 Cookie（纯文本格式）")
            return cookies

    print(f"  ⚠ {p.name} 无法识别格式，内容预览: {content[:80]}")
    return None


def load_cached_cookies() -> list[dict] | None:
    """读取脚本自己写的 JSON 缓存（带时间戳）"""
    return load_cookies_from_file(COOKIE_CACHE_FILE)


def save_cookies(cookies: list[dict]):
    payload = {"saved_at": time.time(), "cookies": cookies}
    COOKIE_CACHE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"  ✓ Cookie 已缓存到 {COOKIE_CACHE_FILE}")


def has_cf_clearance(cookies: list[dict]) -> bool:
    return any(c.get("name") == "cf_clearance" for c in cookies)


# ─────────────────────────────────────────────
# 方案 A: playwright-stealth
# ─────────────────────────────────────────────

async def get_cookies_via_stealth(url: str) -> list[dict] | None:
    """
    使用 playwright-stealth 伪装浏览器指纹，让 CF 认为是真人。
    对 CF JS Challenge 有效，对 Turnstile 成功率约 30-50%。
    兼容 playwright-stealth v1（stealth_async）和 v2（Stealth 类）。
    """
    print("\n  [方案A] playwright-stealth 尝试获取 Cookie...")

    # 兼容 playwright-stealth v1 和 v2，逐一尝试所有已知 API
    apply_stealth = None
    _import_errors: list[str] = []

    # v1: from playwright_stealth import stealth_async
    try:
        from playwright_stealth import stealth_async as _fn
        apply_stealth = _fn
        print("  ✓ playwright-stealth v1 API (stealth_async)")
    except Exception as e:
        _import_errors.append(f"v1/stealth_async: {e}")

    # v2 候选方法名（不同小版本名字不同）
    if apply_stealth is None:
        _v2_methods = ["apply_stealth_async", "use_async", "async_stealth", "__call__"]
        try:
            from playwright_stealth import Stealth as _Stealth  # type: ignore
            _s = _Stealth()
            for _m in _v2_methods:
                if callable(getattr(_s, _m, None)):
                    apply_stealth = getattr(_s, _m)
                    print(f"  ✓ playwright-stealth v2 API (Stealth.{_m})")
                    break
            if apply_stealth is None:
                available = [a for a in dir(_s) if not a.startswith("_")]
                _import_errors.append(f"v2/Stealth 存在但无可用异步方法，可用属性: {available}")
        except Exception as e:
            _import_errors.append(f"v2/Stealth: {e}")

    if apply_stealth is None:
        print("  ✗ 无法使用 playwright-stealth，详细错误:")
        for err in _import_errors:
            print(f"    • {err}")
        print("  → 跳过方案A，尝试方案B/C")
        return None

    # 方案A 先尝试 Firefox（TLS 指纹更像真实浏览器），再试 Chromium
    for engine_name, launch_fn_attr, extra_args in [
        ("Firefox",  "firefox",  []),
        ("Chromium", "chromium", [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-automation",
            "--exclude-switches=enable-automation",
            "--disable-infobars",
            "--window-size=1920,1080",
        ]),
    ]:
        print(f"  ▶ 尝试 {engine_name}...")
        try:
            result = await _stealth_attempt(apply_stealth, launch_fn_attr, extra_args, url)
            if result:
                return result
        except Exception as e:
            print(f"  ⚠ {engine_name} 失败: {e}")

    print("  ✗ 方案A 未获得 cf_clearance（CF 仍在拦截）")
    return None


async def _stealth_attempt(apply_stealth, engine_attr: str, extra_args: list, url: str) -> list[dict] | None:
    """实际执行一次 stealth 浏览器尝试"""
    import random

    async with async_playwright() as p:
        engine = getattr(p, engine_attr)
        launch_kwargs: dict = {"headless": STEALTH_HEADLESS}
        if extra_args:
            launch_kwargs["args"] = extra_args

        browser = await engine.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page = await context.new_page()

        # 应用 stealth patch
        try:
            await apply_stealth(page)
        except Exception as e:
            print(f"  ⚠ apply_stealth 失败: {e}，继续（无 stealth）")

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        # 模拟人类鼠标随机移动，帮助通过行为检测
        try:
            for _ in range(5):
                await page.mouse.move(
                    random.randint(200, 1600),
                    random.randint(100, 800),
                    steps=random.randint(5, 15),
                )
                await asyncio.sleep(random.uniform(0.3, 0.8))
        except Exception:
            pass

        # 等待 CF 放行：必须同时满足「无 challenge iframe」且「存在 cf_clearance cookie」
        print(f"  ▶ 等待 CF 验证最多 {STEALTH_WAIT_SEC} 秒...")
        cf_solved = False
        for i in range(STEALTH_WAIT_SEC):
            await asyncio.sleep(1)
            cf_frames = [f for f in page.frames if "challenges.cloudflare.com" in f.url]
            raw_now = await context.cookies()
            got_clearance = any(c.get("name") == "cf_clearance" for c in raw_now)

            if got_clearance:
                print(f"  ✓ 第 {i+1} 秒：获得 cf_clearance，验证成功")
                cf_solved = True
                break
            elif not cf_frames and i > 0:
                # 无 challenge 但也无 cf_clearance：
                # 可能是 IP 直接放行（不需要 cookie）或页面还没加载完
                print(f"  ℹ 第 {i+1} 秒：无 challenge iframe，等待 cookie 写入...")
            # 每5秒移动鼠标模拟人类行为
            if i % 5 == 4:
                try:
                    await page.mouse.move(
                        random.randint(300, 1500),
                        random.randint(200, 700),
                        steps=10,
                    )
                except Exception:
                    pass

        if not cf_solved:
            print(f"  ✗ {STEALTH_WAIT_SEC} 秒内未获得 cf_clearance")

        raw = await context.cookies()
        await browser.close()

    cookies = _normalize_cookies(raw, DOMAIN)
    if has_cf_clearance(cookies):
        print(f"  ✓ 方案A ({engine_attr}) 成功获取 cf_clearance")
        return cookies
    return None


# ─────────────────────────────────────────────
# 方案 B: FlareSolverr
# ─────────────────────────────────────────────

async def get_cookies_via_flaresolverr(url: str) -> list[dict] | None:
    """
    调用本地 FlareSolverr 服务自动解 CF 挑战。
    FlareSolverr 在内部使用 undetected-chromedriver，成功率很高。

    启动 FlareSolverr:
      docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
    """
    print(f"\n  [方案B] FlareSolverr ({FLARESOLVERR_URL}) 尝试获取 Cookie...")
    try:
        async with httpx.AsyncClient(timeout=FLARESOLVERR_TIMEOUT + 10) as client:
            resp = await client.post(
                FLARESOLVERR_URL,
                json={
                    "cmd": "request.get",
                    "url": url,
                    "maxTimeout": FLARESOLVERR_TIMEOUT * 1000,
                },
            )
        raw_text = resp.text.strip()
        if not raw_text:
            print(f"  ✗ FlareSolverr 返回空响应 (HTTP {resp.status_code})")
            print(f"    可能原因: Docker 容器刚启动还未就绪，或请求被拒绝")
            return None
        data = resp.json()
    except httpx.ConnectError:
        print(f"  ✗ 无法连接 FlareSolverr（端口 8191 未监听）")
        print(f"    启动命令: docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest")
        return None
    except json.JSONDecodeError as e:
        print(f"  ✗ FlareSolverr 响应不是有效 JSON: {e}")
        print(f"    原始响应 (前200字符): {resp.text[:200]!r}")
        return None
    except Exception as e:
        print(f"  ✗ FlareSolverr 请求异常: {type(e).__name__}: {e}")
        return None

    if data.get("status") != "ok":
        print(f"  ✗ FlareSolverr 返回错误: {data.get('message', data)}")
        return None

    raw_cookies = data.get("solution", {}).get("cookies", [])
    cookies = _normalize_cookies(raw_cookies, DOMAIN)
    if has_cf_clearance(cookies):
        print("  ✓ 方案B 成功获取 cf_clearance")
        return cookies

    print("  ✗ 方案B 未获得 cf_clearance")
    return None


# ─────────────────────────────────────────────
# 方案 C: CapSolver (付费 CAPTCHA API)
# ─────────────────────────────────────────────

async def get_cookies_via_capsolver(url: str) -> list[dict] | None:
    """
    使用 CapSolver API 解 Cloudflare Turnstile，获取 cf_clearance。
    流程：
      1. 从页面 HTML 提取 Turnstile sitekey
      2. 提交任务到 CapSolver
      3. 轮询结果获取 token
      4. 用 token 通过 CF 验证，拿到 cf_clearance cookie
    """
    if not CAPSOLVER_API_KEY:
        print("\n  [方案C] 未配置 CAPSOLVER_API_KEY，跳过")
        return None

    print(f"\n  [方案C] CapSolver API 尝试获取 Cookie...")

    # 第一步：获取页面，提取 sitekey
    sitekey = await _extract_turnstile_sitekey(url)
    if not sitekey:
        print("  ✗ 未找到 Turnstile sitekey，无法使用 CapSolver")
        return None
    print(f"  ✓ 找到 sitekey: {sitekey}")

    # 第二步：提交 CapSolver 任务
    token = await _capsolver_solve_turnstile(url, sitekey)
    if not token:
        print("  ✗ CapSolver 未返回 token")
        return None
    print(f"  ✓ 获得 Turnstile token: {token[:30]}...")

    # 第三步：用 token 过验证，拿 cf_clearance
    cookies = await _submit_turnstile_token(url, token, sitekey)
    if cookies and has_cf_clearance(cookies):
        print("  ✓ 方案C 成功获取 cf_clearance")
        return cookies

    print("  ✗ 方案C 未获得 cf_clearance")
    return None


async def _extract_turnstile_sitekey(url: str) -> str | None:
    """不带 Cookie 加载页面，提取 Turnstile sitekey"""
    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)
        page = await browser.new_page(user_agent=USER_AGENT)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(3)

        # 从 iframe src 或 data-sitekey 属性提取
        sitekey = await page.evaluate("""() => {
            // 方式1：data-sitekey 属性
            const el = document.querySelector('[data-sitekey]');
            if (el) return el.getAttribute('data-sitekey');
            // 方式2：从 CF iframe URL 提取
            const frames = [...document.querySelectorAll('iframe')];
            for (const f of frames) {
                const src = f.src || '';
                const m = src.match(/sitekey=([^&]+)/);
                if (m) return m[1];
            }
            // 方式3：从 window.__CF$cv$params 提取
            try {
                const p = window.__CF$cv$params;
                if (p && p.k) return p.k;
            } catch(e) {}
            return null;
        }""")

        if not sitekey:
            # 从页面 HTML 正则搜索
            html = await page.content()
            m = re.search(r'sitekey["\s:=\']+([0-9a-zA-Z_-]{20,})', html)
            if m:
                sitekey = m.group(1)

        await browser.close()
    return sitekey


async def _capsolver_solve_turnstile(page_url: str, sitekey: str) -> str | None:
    """提交 Turnstile 任务到 CapSolver，轮询直到完成"""
    async with httpx.AsyncClient(timeout=120) as client:
        # 创建任务
        create_resp = await client.post(
            "https://api.capsolver.com/createTask",
            json={
                "clientKey": CAPSOLVER_API_KEY,
                "task": {
                    "type": "AntiTurnstileTaskProxyLess",
                    "websiteURL": page_url,
                    "websiteKey": sitekey,
                },
            },
        )
        result = create_resp.json()
        if result.get("errorId"):
            print(f"  ✗ CapSolver 创建任务失败: {result.get('errorDescription')}")
            return None

        task_id = result["taskId"]
        print(f"  ▶ CapSolver 任务已创建: {task_id}，等待结果...")

        # 轮询结果
        for _ in range(30):
            await asyncio.sleep(3)
            poll_resp = await client.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": CAPSOLVER_API_KEY, "taskId": task_id},
            )
            poll = poll_resp.json()
            status = poll.get("status")
            if status == "ready":
                return poll["solution"]["token"]
            if status == "failed" or poll.get("errorId"):
                print(f"  ✗ CapSolver 任务失败: {poll.get('errorDescription')}")
                return None

    print("  ✗ CapSolver 超时")
    return None


async def _submit_turnstile_token(url: str, token: str, sitekey: str) -> list[dict] | None:
    """将 CapSolver 返回的 token 提交给 CF，获取 cf_clearance cookie"""
    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass

        # 注入 token 并提交表单（CF Turnstile 验证流程）
        await page.evaluate(f"""(token) => {{
            // 找到 turnstile 响应 input
            const inputs = document.querySelectorAll('input[name="cf-turnstile-response"], input[name="g-recaptcha-response"]');
            inputs.forEach(i => i.value = token);
            // 尝试触发提交
            const form = document.querySelector('form#challenge-form');
            if (form) form.submit();
        }}""", token)

        await asyncio.sleep(5)
        raw = await context.cookies()
        await browser.close()

    return _normalize_cookies(raw, DOMAIN)


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _normalize_cookies(raw: list[dict], domain: str) -> list[dict]:
    """统一 cookie 格式，确保 Playwright context.add_cookies 能接受"""
    result = []
    for c in raw:
        result.append({
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", domain).lstrip("."),
            "path": c.get("path", "/"),
            "httpOnly": c.get("httpOnly", False),
            "secure": c.get("secure", True),
            "sameSite": c.get("sameSite", "None"),
        })
    return result


async def acquire_cookies() -> list[dict]:
    """
    按优先级尝试获取 Cookie：
      0. BROWSER_COOKIE_FILES 列表中的文件（最高优先级，自动扫描）
      1. playwright-stealth 自动获取
      2. FlareSolverr 本地服务
      3. CapSolver 付费 API
    """
    # 优先级 0：按顺序扫描 BROWSER_COOKIE_FILES 列表
    for fname in BROWSER_COOKIE_FILES:
        fpath = Path(fname)
        if not fpath.exists():
            continue
        file_cookies = load_cookies_from_file(fpath)
        if file_cookies and has_cf_clearance(file_cookies):
            print(f"  ✓ 使用 {fname} 中的浏览器 Cookie")
            return file_cookies
        elif file_cookies:
            print(f"  ⚠ {fname} 中没有 cf_clearance，继续查找...")

    print("\n  ℹ 开始自动获取 Cookie...")

    # 方案 A
    cookies = await get_cookies_via_stealth(TARGET_URL)
    if cookies:
        save_cookies(cookies)
        return cookies

    # 方案 B
    cookies = await get_cookies_via_flaresolverr(TARGET_URL)
    if cookies:
        save_cookies(cookies)
        return cookies

    # 方案 C
    cookies = await get_cookies_via_capsolver(TARGET_URL)
    if cookies:
        save_cookies(cookies)
        return cookies

    print("\n  ✗ 三种方案均未能自动获取 Cookie")
    print("  建议：")
    print("  1. 在浏览器手动访问目标页，从 F12 复制 Cookie 存入 cookie_cache.json")
    print("  2. 启动 FlareSolverr: docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest")
    print("  3. 配置 CAPSOLVER_API_KEY（付费，最可靠）")
    sys.exit(1)


# ─────────────────────────────────────────────
# m3u8 提取
# ─────────────────────────────────────────────

async def find_m3u8_via_network(page: Page, timeout: int) -> str | None:
    found = asyncio.Event()
    result: list[str] = []

    def capture(url: str):
        if ".m3u8" in url.lower() and not result:
            result.append(url)
            found.set()

    page.on("request", lambda r: capture(r.url))
    page.on("response", lambda r: capture(r.url))
    page.on("framenavigated", lambda f: capture(f.url))

    try:
        await asyncio.wait_for(found.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass

    return result[0] if result else None


async def try_click_play(page: Page):
    for sel in ["video", ".vjs-big-play-button", ".play-btn", "[class*='play']", "#player"]:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click(timeout=3000)
                print(f"  ✓ 点击播放: {sel}")
                return
        except Exception:
            pass

    for frame in page.frames:
        if frame.url and "cloudflare.com" not in frame.url:
            for sel in ["video", ".vjs-big-play-button", ".play-btn"]:
                try:
                    el = frame.locator(sel).first
                    if await el.count() > 0:
                        await el.click(timeout=3000)
                        print(f"  ✓ 在 iframe 中点击播放: {sel}")
                        return
                except Exception:
                    pass


async def extract_m3u8_fallback(page: Page) -> str | None:
    """DOM + JS 备用提取"""
    for frame in [page] + page.frames:
        try:
            html = await frame.content()
            m = M3U8_RE.search(html)
            if m:
                return m.group(0)
        except Exception:
            pass

    for script in [
        "(() => { try { return jwplayer().getPlaylistItem().file } catch(e) { return null } })()",
        """(() => {
            for (const s of document.querySelectorAll('script')) {
                const m = s.textContent.match(/https?:\\/\\/[^\\s"'<>]+\\.m3u8/i);
                if (m) return m[0];
            }
            return null;
        })()""",
    ]:
        try:
            r = await page.evaluate(script)
            if r and ".m3u8" in r.lower():
                return r
        except Exception:
            pass

    return None


# ─────────────────────────────────────────────
# 下载
# ─────────────────────────────────────────────

def download_m3u8(m3u8_url: str, output_path: Path) -> bool:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-headers", f"Referer: {TARGET_URL}\r\nUser-Agent: {USER_AGENT}\r\n",
        "-i", m3u8_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(output_path),
    ]
    return subprocess.run(cmd).returncode == 0


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

async def main():
    print("视频下载器 (自动 Cookie 获取版)")
    print("=" * 60)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # 第一步：自动获取 Cookie
    cookies = await acquire_cookies()
    cf = next((c for c in cookies if c["name"] == "cf_clearance"), None)
    print(f"  ✓ cf_clearance: {cf['value'][:40]}..." if cf else "  ⚠ 无 cf_clearance")

    # 第二步：加载目标页面并截获 m3u8
    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)
        context: BrowserContext = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
        )
        await context.add_cookies(cookies)

        page = await context.new_page()
        m3u8_task = asyncio.create_task(find_m3u8_via_network(page, M3U8_TIMEOUT))

        print(f"\n  ▶ 加载页面: {TARGET_URL}")
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  ⚠ 加载超时（继续）: {e}")

        # 等待 CF iframe 消失（最多15秒）
        for _ in range(15):
            if not any("challenges.cloudflare.com" in f.url for f in page.frames):
                break
            await asyncio.sleep(1)

        await asyncio.sleep(3)

        # ── 关键检测：CF 拦截页面 ──────────────────────────────────────────
        # CF 可能以整页形式返回验证页（URL 不变，但内容是 CF 挑战），需单独检测
        page_title = await page.title()
        page_html_snippet = (await page.content())[:2000].lower()
        cf_blocked = (
            "performing security verification" in page_html_snippet
            or "cf-browser-verification" in page_html_snippet
            or "just a moment" in page_html_snippet
            or "enable javascript and cookies" in page_html_snippet
            or (page_title and "just a moment" in page_title.lower())
        )
        if cf_blocked:
            screenshot = DOWNLOAD_DIR / "debug.png"
            await page.screenshot(path=str(screenshot))
            await browser.close()
            print(f"\n  ✗ Cloudflare 仍在拦截（cf_clearance Cookie 已过期或 IP 不匹配）")
            print(f"  截图: {screenshot}")
            print()
            print("  ━━━ 解决方法 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            print("  cf_clearance 与生成它的 IP 绑定，约 1 小时后失效。")
            print()
            print("  请按以下步骤获取新 Cookie：")
            print("  1. 用浏览器打开目标页面，完成 CF 人机验证")
            print("  2. 按 F12 → 网络 → 点击任意请求 → 找到「请求头」中的 Cookie 行")
            print("  3. 复制整行 Cookie 值，替换 cf_cookies.json 中的内容")
            print("     （格式：纯文本一行，或上方提供的 JSON 格式均可）")
            print("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            # 只清除自动缓存，不删用户手动维护的 cf_cookies.json
            if COOKIE_CACHE_FILE.exists():
                COOKIE_CACHE_FILE.unlink()
            sys.exit(1)
        # ────────────────────────────────────────────────────────────────────

        # 调试：打印所有 frame
        print("  ▶ 页面 frames:")
        for i, f in enumerate(page.frames):
            print(f"    [{i}] {f.url}")

        # 尝试触发播放
        if not m3u8_task.done():
            print("\n  ▶ 触发视频播放...")
            await try_click_play(page)
            await asyncio.sleep(3)

        # 等待网络拦截结果
        try:
            m3u8_url = await asyncio.wait_for(
                asyncio.shield(m3u8_task), timeout=30
            ) if not m3u8_task.done() else m3u8_task.result()
        except asyncio.TimeoutError:
            m3u8_url = None

        # 备用方案
        if not m3u8_url:
            print("  ▶ 网络拦截无结果，尝试 DOM/JS 搜索...")
            m3u8_url = await extract_m3u8_fallback(page)

        if not m3u8_url:
            screenshot = DOWNLOAD_DIR / "debug.png"
            await page.screenshot(path=str(screenshot), full_page=True)
            print(f"\n  ✗ 未找到 m3u8（页面已正常加载，但未检测到视频流）")
            print(f"  截图: {screenshot}  当前 URL: {page.url}")
            await browser.close()
            sys.exit(1)

        print(f"\n  ✓ m3u8 URL:\n    {m3u8_url}")
        await browser.close()

    # 第三步：下载
    output = DOWNLOAD_DIR / "video.mp4"
    print(f"\n  ▶ 开始下载 → {output}")
    if download_m3u8(m3u8_url, output):
        print(f"\n  ✓ 下载完成: {output}")
    else:
        print(f"\n  ✗ ffmpeg 下载失败，可手动执行:")
        print(f'  ffmpeg -i "{m3u8_url}" -c copy output.mp4')
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

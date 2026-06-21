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

# --- 方案 A: playwright-stealth ---
# 不需要额外配置，脚本自动处理
STEALTH_HEADLESS = True          # 改为 False 可以看到浏览器（调试用）
STEALTH_WAIT_SEC = 15            # 等待 CF 自动放行的秒数

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
# Cookie 缓存（避免每次都重新求解）
# ─────────────────────────────────────────────

def load_cached_cookies() -> list[dict] | None:
    if not COOKIE_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(COOKIE_CACHE_FILE.read_text())
        # cf_clearance 通常有效期 1 小时，保守取 50 分钟
        if time.time() - data.get("saved_at", 0) > 3000:
            print("  ⚠ 缓存 Cookie 已过期")
            return None
        print(f"  ✓ 使用缓存 Cookie（剩余约 {int((3000 - (time.time() - data['saved_at'])) / 60)} 分钟有效）")
        return data["cookies"]
    except Exception:
        return None


def save_cookies(cookies: list[dict]):
    payload = {"saved_at": time.time(), "cookies": cookies}
    COOKIE_CACHE_FILE.write_text(json.dumps(payload, indent=2))
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
    """
    print("\n  [方案A] playwright-stealth 尝试获取 Cookie...")
    try:
        from playwright_stealth import stealth_async
    except ImportError:
        print("  ⚠ 未安装 playwright-stealth，跳过方案A")
        print("    安装命令: pip install playwright-stealth")
        return None

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=STEALTH_HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        page = await context.new_page()
        await stealth_async(page)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        # 等待 CF 自动放行（CF JS Challenge 通常几秒内完成）
        print(f"  ▶ 等待 CF 验证 {STEALTH_WAIT_SEC} 秒...")
        for _ in range(STEALTH_WAIT_SEC):
            await asyncio.sleep(1)
            cf_frames = [f for f in page.frames if "challenges.cloudflare.com" in f.url]
            if not cf_frames:
                break

        raw = await context.cookies()
        await browser.close()

    cookies = _normalize_cookies(raw, DOMAIN)
    if has_cf_clearance(cookies):
        print("  ✓ 方案A 成功获取 cf_clearance")
        return cookies

    print("  ✗ 方案A 未获得 cf_clearance（CF 仍在拦截）")
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
        data = resp.json()
    except httpx.ConnectError:
        print(f"  ✗ 无法连接 FlareSolverr，请确认 Docker 已启动")
        print(f"    docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest")
        return None
    except Exception as e:
        print(f"  ✗ FlareSolverr 请求失败: {e}")
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
    按优先级尝试三种方案，成功后缓存结果。
    如果全部失败则退出。
    """
    # 先检查缓存
    cached = load_cached_cookies()
    if cached:
        return cached

    print("\n  ℹ 未找到有效缓存，开始自动获取 Cookie...")

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

        # 等待 CF 放行
        for _ in range(15):
            if not any("challenges.cloudflare.com" in f.url for f in page.frames):
                break
            await asyncio.sleep(1)

        await asyncio.sleep(3)

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
            print(f"\n  ✗ 未找到 m3u8，截图: {screenshot}")
            print(f"  当前 URL: {page.url}")

            # Cookie 可能已失效，清除缓存让下次重新获取
            if COOKIE_CACHE_FILE.exists():
                COOKIE_CACHE_FILE.unlink()
                print("  ℹ 已清除 Cookie 缓存，下次运行将重新获取")

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

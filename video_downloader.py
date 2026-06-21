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

# 【直接指定播放器 URL】
# 如果你已经从浏览器 F12 拿到了具体的播放器 iframe URL（如 playmogo.com/e/...），
# 填在这里即可跳过主页面加载和服务器切换，直接加载播放器页面提取 m3u8。
# 留空 "" 则走正常流程（从 TARGET_URL 开始）。
PLAYER_URL = ""

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

# --- ffmpeg 路径 ---
# 留空 "" 则自动在 PATH 和常见目录中查找
# Windows 示例: r"D:\Tool\ffmpeg\bin\ffmpeg.exe"
# macOS/Linux 示例: "/usr/local/bin/ffmpeg"
FFMPEG_PATH = r"D:\Tool\ffmpeg\bin\ffmpeg.exe"

# --- 视频服务器选择 ---
# 页面上有多个线路按钮时，脚本会先点击指定服务器再播放
# 常见值: "DS" / "TV" / "JPA" / "ST"，留空 "" 使用页面默认线路
VIDEO_SERVER = "DS"

# --- 播放器 Cloudflare 验证 ---
# 某些视频播放器域名（如 playmogo.com）有自己的 CF 保护。
# 脚本会等待最多这么多秒让它自动通过（stealth 模式下通常可自动解决）。
# 若始终无法自动通过，将 BROWSER_HEADLESS 改为 False 手动完成验证。
PLAYER_CF_WAIT_SEC = 30

# --- 主下载浏览器 ---
# True = 无头（正常使用）；False = 显示浏览器窗口（调试 / 手动过 CF 验证）
BROWSER_HEADLESS = True

# --- 方案 C: CapSolver API (付费) ---
# 注册: https://capsolver.com  充值约 $2 可解数千次
CAPSOLVER_API_KEY = ""           # 填入你的 API Key 启用此方案

# --- 通用 ---
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"
M3U8_TIMEOUT = 60
# ===== 配置结束 =====

M3U8_RE = re.compile(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', re.IGNORECASE)
DOMAIN = urlparse(TARGET_URL).netloc

# 广告/追踪 iframe 关键词——触发播放时跳过这些 iframe，避免误点广告视频
_AD_IFRAME_KEYWORDS = (
    "mayzaent.", "googlesyndication.", "doubleclick.", "adnxs.",
    "amazon-adsystem.", "creative.", "adtech.", "advertising.",
    "tracker.", "tracking.", "analytics.", "metrics.", "pixel.",
    "campaign", "banner", "widget",
)


def _load_stealth_fn():
    """返回 playwright-stealth 异步应用函数（兼容 v1/v2），不可用返回 None"""
    try:
        from playwright_stealth import stealth_async
        return stealth_async
    except ImportError:
        pass
    try:
        from playwright_stealth import Stealth
        s = Stealth()
        for method in ["apply_stealth_async", "use_async", "async_stealth", "__call__"]:
            fn = getattr(s, method, None)
            if callable(fn):
                return fn
    except ImportError:
        pass
    return None


async def wait_for_player_cf(page: "Page", timeout: int = PLAYER_CF_WAIT_SEC) -> bool:
    """
    检测并等待播放器 iframe 内的 CF 人机验证自动通过。
    stealth 模式下 CF JS Challenge 通常几秒内自动解决；
    若 CF 要求用户交互（Turnstile 复选框），需将 BROWSER_HEADLESS 改为 False。
    返回 True 表示已通过或本来就无验证，False 表示超时仍未通过。
    """
    cf_frames = [f for f in page.frames if "challenges.cloudflare.com" in (f.url or "")]
    if not cf_frames:
        return True

    print(f"\n  ⚠ 播放器 iframe 内检测到 CF 人机验证，等待最多 {timeout} 秒自动通过...")
    print(f"    （若长时间卡住，可将 BROWSER_HEADLESS = False 改为可见模式手动完成）")
    for i in range(timeout):
        await asyncio.sleep(1)
        cf_frames = [f for f in page.frames if "challenges.cloudflare.com" in (f.url or "")]
        if not cf_frames:
            print(f"  ✓ 第 {i+1} 秒：播放器 CF 验证已通过")
            await asyncio.sleep(1)
            return True
        if (i + 1) % 5 == 0:
            print(f"  ⏳ 已等待 {i+1}/{timeout} 秒...")

    print(f"  ✗ {timeout} 秒内播放器 CF 验证未自动通过")
    return False


async def _find_player_iframe_url(page: "Page", timeout: int = 15) -> str | None:
    """
    等待播放器 embed iframe URL 出现（最多 timeout 秒）。
    优先匹配路径含 /e/、/embed 等 embed 模式的真正播放器，
    避免误选 lk1.supremejav.com/supjav.php?l=... 这类会话绑定的中间包装页。
    中间包装页通常先于内嵌播放器加载，需要等待嵌套 iframe 出现。
    """
    # 这些路径模式通常出现在真正的播放器 embed 页面
    PLAYER_PATTERNS = ("/e/", "/embed", "/player/", "/hls/", "stream.")

    def _candidates() -> list[str]:
        urls = []
        for frame in page.frames:
            url = frame.url or ""
            if not url or not url.startswith("http"):
                continue
            if DOMAIN in url:
                continue
            if "challenges.cloudflare.com" in url:
                continue
            if any(kw in url.lower() for kw in _AD_IFRAME_KEYWORDS):
                continue
            urls.append(url)
        return urls

    def _is_player(url: str) -> bool:
        u = url.lower()
        return any(p in u for p in PLAYER_PATTERNS)

    fallback: str | None = None
    for i in range(timeout):
        cands = _candidates()
        player_cands = [u for u in cands if _is_player(u)]
        if player_cands:
            if i > 0:
                print(f"    （等待 {i+1} 秒后出现）")
            return player_cands[0]
        # 记录第一个非 player 候选作为兜底
        if cands and fallback is None:
            fallback = cands[0]
        if i < timeout - 1:
            await asyncio.sleep(1)

    # 超时仍未找到 player 模式的 URL，返回兜底值（可能是包装页）
    if fallback:
        print(f"  ⚠ 未找到 embed 模式播放器，使用兜底 URL: {fallback[:80]}")
    return fallback


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

    apply_stealth = _load_stealth_fn()
    if apply_stealth is None:
        print("  ✗ 无法导入 playwright-stealth，跳过方案A")
        print("  → pip install playwright-stealth")
        return None
    print("  ✓ playwright-stealth 已加载")

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

    def _accept(url: str, content_type: str = "") -> bool:
        if ".m3u8" in url.lower():
            return True
        ct = content_type.lower()
        return "mpegurl" in ct or "x-mpegurl" in ct

    def on_request(r):
        if not result and _accept(r.url):
            result.append(r.url)
            found.set()

    def on_response(r):
        if not result:
            ct = r.headers.get("content-type") or "" if hasattr(r, "headers") else ""
            if _accept(r.url, ct):
                result.append(r.url)
                found.set()

    def on_frame(f):
        if not result and _accept(f.url):
            result.append(f.url)
            found.set()

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("framenavigated", on_frame)

    try:
        await asyncio.wait_for(found.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass

    return result[0] if result else None


async def click_server_button(page: Page, server_name: str) -> bool:
    """
    点击视频服务器切换按钮（如 DS、TV、ST、JPA 等）。
    点击后等待 2 秒让播放器重新初始化，再捕获新的 m3u8。
    """
    if not server_name:
        return False

    # 按文本内容匹配，覆盖常见的按钮/链接/列表项写法
    selectors = [
        f"button:text-is('{server_name}')",
        f"a:text-is('{server_name}')",
        f"li:text-is('{server_name}')",
        f"span:text-is('{server_name}')",
        f"div:text-is('{server_name}')",
        f"[class*='server']:text-is('{server_name}')",
        f"[class*='source']:text-is('{server_name}')",
        # 宽松匹配（文本包含，可能误匹配但最后兜底）
        f":text('{server_name}')",
    ]

    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click(timeout=3000)
                print(f"  ✓ 已切换到服务器: {server_name}（选择器: {sel}）")
                await asyncio.sleep(5)   # 等待播放器重新加载（DS 播放器需要更长时间）
                return True
        except Exception:
            continue

    # 所有选择器都失败，打印页面上的所有按钮文本帮助调试
    try:
        btn_texts = await page.evaluate("""() => {
            const els = [...document.querySelectorAll('button, a, li, [class*="server"], [class*="source"]')];
            return els.map(e => e.textContent.trim()).filter(t => t && t.length < 20);
        }""")
        unique = list(dict.fromkeys(btn_texts))[:30]
        print(f"  ⚠ 未找到 '{server_name}' 按钮，页面上检测到的按钮文字: {unique}")
    except Exception:
        print(f"  ⚠ 未找到 '{server_name}' 按钮")
    return False


async def try_click_play(page: Page):
    # 先尝试将视频区域滚动到视口，某些播放器只在可见时才响应点击
    try:
        await page.evaluate(
            "document.querySelector('video')?.scrollIntoView({behavior:'instant',block:'center'})"
        )
    except Exception:
        pass

    # ── DOM 点击（主页面）────────────────────────────────────────────────────
    dom_selectors = [
        "video",
        ".vjs-big-play-button",
        ".jw-display-icon-container",
        ".jw-icon-display",
        ".play-btn",
        ".btn-play",
        ".player-play-btn",
        "[class*='play']",
        "[aria-label*='play' i]",
        "#player",
    ]
    for sel in dom_selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                try:
                    await el.scroll_into_view_if_needed()
                except Exception:
                    pass
                await el.click(timeout=3000)
                print(f"  ✓ 点击播放: {sel}")
                return
        except Exception:
            pass

    # ── DOM 点击（所有 iframe）──────────────────────────────────────────────
    iframe_selectors = [
        "video",
        ".vjs-big-play-button",
        ".jw-display-icon-container",
        ".jw-icon-display",
        ".play-btn",
        "[class*='play']",
    ]
    for frame in page.frames:
        frame_url = frame.url or ""
        if not frame_url or "cloudflare.com" in frame_url:
            continue
        # 跳过广告 iframe，避免误点广告视频
        if any(kw in frame_url.lower() for kw in _AD_IFRAME_KEYWORDS):
            print(f"  ⚠ 跳过广告 iframe: {frame_url[:80]}")
            continue
        for sel in iframe_selectors:
            try:
                el = frame.locator(sel).first
                if await el.count() > 0:
                    await el.click(timeout=3000)
                    print(f"  ✓ 在 iframe 中点击播放: {sel} ({frame.url[:70]})")
                    return
            except Exception:
                pass

    # ── JS 强制播放（主页面 + 所有 frame）───────────────────────────────────
    print("  ▶ DOM 点击未命中，尝试 JS 强制播放...")
    for ctx in [page] + list(page.frames):
        try:
            ctx_url = getattr(ctx, "url", page.url) or ""
            if "cloudflare.com" in ctx_url:
                continue
            # 广告 iframe 同样跳过
            if any(kw in ctx_url.lower() for kw in _AD_IFRAME_KEYWORDS):
                continue
            count = await ctx.evaluate("""() => {
                const vs = document.querySelectorAll('video');
                vs.forEach(v => { try { v.play(); } catch(e) {} });
                return vs.length;
            }""")
            if count:
                print(f"  ✓ JS video.play() 触发 {count} 个视频（{str(ctx_url)[:70]}）")
                return
        except Exception:
            pass

    # ── 播放器 API（JWPlayer / VideoJS）────────────────────────────────────
    for js, label in [
        ("try { jwplayer().play(); return true; } catch(e) { return false; }",
         "jwplayer().play()"),
        ("try { videojs(document.querySelector('.video-js')).play(); return true; } catch(e) { return false; }",
         "videojs().play()"),
        ("const p=document.querySelector('#player'); if(p&&p.play){p.play();return true;} return false;",
         "#player.play()"),
    ]:
        try:
            r = await page.evaluate(f"(() => {{ {js} }})()")
            if r:
                print(f"  ✓ {label} 触发播放")
                return
        except Exception:
            pass

    print("  ⚠ 未找到可触发的播放元素，等待播放器自动加载...")


async def extract_m3u8_fallback(page: Page) -> str | None:
    """
    DOM + JS 备用提取。
    优先从各 iframe 的播放器 API / video 元素读取实际播放地址，
    不限制必须包含 .m3u8（video.currentSrc 可能是无扩展名的流）。
    """
    # 收集所有非广告、非 CF 的 frame 上下文
    ctxs = [page] + [
        f for f in page.frames
        if f.url
        and "cloudflare.com" not in f.url
        and not any(kw in f.url.lower() for kw in _AD_IFRAME_KEYWORDS)
    ]

    # ── 1. 播放器 API + video.currentSrc（在每个 frame 里尝试）──────────────
    api_scripts = [
        # HTML5 video 元素当前播放地址（最可靠，视频在播就有值）
        "(() => { const v = document.querySelector('video'); return v && (v.currentSrc || v.src) || null; })()",
        # JWPlayer
        "(() => { try { return jwplayer().getPlaylistItem().file || null; } catch(e) { return null; } })()",
        # VideoJS
        "(() => { try { const p = videojs(document.querySelector('.video-js')); return p ? p.currentSrc() : null; } catch(e) { return null; } })()",
    ]
    for ctx in ctxs:
        for script in api_scripts:
            try:
                r = await ctx.evaluate(script)
                if r and isinstance(r, str) and r.startswith("http"):
                    print(f"  ✓ 从播放器 API 获取 URL: {r[:80]}")
                    return r
            except Exception:
                pass

    # ── 2. 搜索页面 HTML / script 标签内的 m3u8 URL ─────────────────────────
    for ctx in ctxs:
        try:
            html = await ctx.content()
            m = M3U8_RE.search(html)
            if m:
                return m.group(0)
        except Exception:
            pass

    try:
        r = await page.evaluate("""() => {
            for (const s of document.querySelectorAll('script')) {
                const m = s.textContent.match(/https?:\\/\\/[^\\s"'<>]+\\.m3u8/i);
                if (m) return m[0];
            }
            return null;
        }""")
        if r:
            return r
    except Exception:
        pass

    return None


# ─────────────────────────────────────────────
# 下载
# ─────────────────────────────────────────────

def _find_ffmpeg() -> str | None:
    """查找 ffmpeg：优先使用 FFMPEG_PATH 配置，再查 PATH 和常见目录"""
    import shutil
    # 1. 用户手动配置的路径
    if FFMPEG_PATH:
        if Path(FFMPEG_PATH).exists():
            return FFMPEG_PATH
        print(f"  ⚠ FFMPEG_PATH 指定的路径不存在: {FFMPEG_PATH}")
    # 2. 系统 PATH
    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        return found
    # 3. Windows 常见安装路径
    for p in [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    ]:
        if Path(p).exists():
            return p
    return None


def download_m3u8_ffmpeg(m3u8_url: str, output_path: Path, ffmpeg_bin: str) -> bool:
    """使用 ffmpeg 下载（速度快，支持加密流）"""
    cmd = [
        ffmpeg_bin, "-y",
        "-headers", f"Referer: {TARGET_URL}\r\nUser-Agent: {USER_AGENT}\r\n",
        "-i", m3u8_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(output_path),
    ]
    print(f"  使用 ffmpeg: {ffmpeg_bin}")
    return subprocess.run(cmd).returncode == 0


async def download_m3u8_python(m3u8_url: str, output_path: Path) -> bool:
    """
    纯 Python 备用下载器（不需要 ffmpeg）。
    抓取 m3u8 → 逐段下载 .ts → 合并为 .ts 文件（可用 VLC 播放）。
    不支持 AES 加密流，加密流需用 ffmpeg。
    """
    import urllib.parse

    headers = {
        "Referer": TARGET_URL,
        "User-Agent": USER_AGENT,
    }
    base_url = m3u8_url.rsplit("/", 1)[0] + "/"

    print("  使用内置 Python 下载器（无需 ffmpeg）")
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        # 1. 下载并解析 m3u8
        resp = await client.get(m3u8_url)
        playlist = resp.text

        # 检测加密（EXT-X-KEY）
        if "#EXT-X-KEY" in playlist:
            print("  ⚠ 检测到加密流（AES-128），Python 下载器不支持")
            print("  请安装 ffmpeg 后重试")
            return False

        # 提取所有 .ts 分片 URL
        segments: list[str] = []
        for line in playlist.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                url = line if line.startswith("http") else urllib.parse.urljoin(base_url, line)
                segments.append(url)

        if not segments:
            print("  ✗ m3u8 中未找到分片")
            return False

        print(f"  ✓ 共 {len(segments)} 个分片，开始下载...")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        ts_output = output_path.with_suffix(".ts")

        with open(ts_output, "wb") as out:
            for i, seg_url in enumerate(segments, 1):
                for attempt in range(3):
                    try:
                        seg_resp = await client.get(seg_url, timeout=20)
                        out.write(seg_resp.content)
                        break
                    except Exception as e:
                        if attempt == 2:
                            print(f"  ✗ 分片 {i}/{len(segments)} 下载失败: {e}")
                            return False
                        await asyncio.sleep(1)

                if i % 20 == 0 or i == len(segments):
                    pct = int(i / len(segments) * 100)
                    print(f"  ▶ 已下载 {i}/{len(segments)} 分片 ({pct}%)", end="\r")

        print(f"\n  ✓ 下载完成: {ts_output}")
        print("  ℹ 文件格式为 .ts，可用 VLC 播放，或安装 ffmpeg 后转为 .mp4：")
        print(f'  ffmpeg -i "{ts_output}" -c copy "{output_path}"')
        return True


async def download_m3u8(m3u8_url: str, output_path: Path) -> bool:
    """自动选择下载方式：优先 ffmpeg，不可用则用 Python 内置"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _find_ffmpeg()
    if ffmpeg:
        return download_m3u8_ffmpeg(m3u8_url, output_path, ffmpeg)

    print("  ⚠ 未找到 ffmpeg，使用内置 Python 下载器")
    print("  建议安装 ffmpeg 获得更好兼容性：")
    print("    winget install ffmpeg          (Windows 推荐)")
    print("    或从 https://ffmpeg.org/download.html 下载后加入 PATH")
    return await download_m3u8_python(m3u8_url, output_path)


# ─────────────────────────────────────────────
# 播放器 URL 直接提取（PLAYER_URL 快捷路径）
# ─────────────────────────────────────────────

async def _extract_from_player_url(player_url: str) -> str | None:
    """
    直接加载播放器页面（如 playmogo.com/e/...），等待 CF 验证通过，
    点击播放，然后从 video.currentSrc / 网络拦截中提取视频流 URL。
    不需要 supjav.com 的 cf_clearance Cookie。
    """
    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=BROWSER_HEADLESS)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()

        # 应用 stealth，有助于播放器 CF 自动通过
        _stealth = _load_stealth_fn()
        if _stealth:
            try:
                await _stealth(page)
                print("  ✓ stealth 已应用")
            except Exception:
                pass

        # 开始网络监听
        m3u8_task = asyncio.create_task(find_m3u8_via_network(page, M3U8_TIMEOUT))

        print(f"  ▶ 加载播放器页面...")
        try:
            await page.goto(player_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  ⚠ 加载超时（继续）: {e}")

        # 等待播放器页面 CF 验证（如有）
        await wait_for_player_cf(page)
        await asyncio.sleep(2)

        # 尝试点击播放按钮
        m3u8_url: str | None = None
        if not m3u8_task.done():
            print("  ▶ 触发播放...")
            await try_click_play(page)
            cf_cleared = await wait_for_player_cf(page)
            if cf_cleared:
                await asyncio.sleep(2)
                m3u8_url = await extract_m3u8_fallback(page)
                if m3u8_url:
                    print("  ✓ 从播放器 API 获取 URL（CF 通过后）")
            if not m3u8_url:
                await asyncio.sleep(1)

        # 等待网络捕获
        if not m3u8_url:
            if m3u8_task.done():
                try:
                    m3u8_url = m3u8_task.result()
                except Exception:
                    pass
            else:
                try:
                    m3u8_url = await asyncio.wait_for(
                        asyncio.shield(m3u8_task), timeout=30
                    )
                except asyncio.TimeoutError:
                    pass

        # 最终 DOM/JS 搜索
        if not m3u8_url:
            m3u8_url = await extract_m3u8_fallback(page)

        if not m3u8_url:
            screenshot = DOWNLOAD_DIR / "debug_player.png"
            await page.screenshot(path=str(screenshot), full_page=True)
            print(f"  截图已保存: {screenshot}")

        await browser.close()
    return m3u8_url


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

async def main():
    print("视频下载器 (自动 Cookie 获取版)")
    print("=" * 60)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # ── PLAYER_URL 快捷路径 ──────────────────────────────────────────────────
    # 若直接填了播放器 URL，跳过 Cookie 获取和主页面导航，直接加载播放器
    if PLAYER_URL:
        print(f"  ▶ 使用直接播放器 URL 模式: {PLAYER_URL[:80]}")
        m3u8_url = await _extract_from_player_url(PLAYER_URL)
        if not m3u8_url:
            print("\n  ✗ 未能从播放器 URL 中提取视频流")
            sys.exit(1)
        print(f"\n  ✓ 视频流 URL:\n    {m3u8_url}")
        output = DOWNLOAD_DIR / "video.mp4"
        print(f"\n  ▶ 开始下载 → {output}")
        ok = await download_m3u8(m3u8_url, output)
        if not ok:
            print(f"\n  ✗ 下载失败，可手动执行:")
            print(f'  ffmpeg -i "{m3u8_url}" -c copy output.mp4')
            sys.exit(1)
        return
    # ────────────────────────────────────────────────────────────────────────

    # 第一步：自动获取 Cookie
    cookies = await acquire_cookies()
    cf = next((c for c in cookies if c["name"] == "cf_clearance"), None)
    print(f"  ✓ cf_clearance: {cf['value'][:40]}..." if cf else "  ⚠ 无 cf_clearance")

    # 第二步：加载目标页面并截获 m3u8
    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=BROWSER_HEADLESS)
        context: BrowserContext = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
        )
        await context.add_cookies(cookies)

        page = await context.new_page()

        # 应用 stealth 伪装——有助于播放器 iframe（如 playmogo.com）内的
        # CF 验证自动通过，不需要用户手动点击复选框
        _stealth = _load_stealth_fn()
        if _stealth:
            try:
                await _stealth(page)
                print("  ✓ 已应用 stealth 伪装（有助于播放器 CF 验证自动通过）")
            except Exception as e:
                print(f"  ⚠ stealth 应用失败（继续）: {e}")

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

        _detected_player_url: str | None = None

        # 切换视频服务器（在播放前点击指定线路按钮）
        if VIDEO_SERVER:
            print(f"\n  ▶ 切换到 {VIDEO_SERVER} 服务器...")
            # ★ 关键：在点击按钮之前重置监听，保证 DS 播放器 iframe 初始化时
            #   发出的 m3u8 预加载请求（VideoJS 会在播放前自动 prefetch manifest）
            #   能被新 task 捕获。若在点击后再重置，预加载请求已经过去了。
            if not m3u8_task.done():
                m3u8_task.cancel()
            m3u8_task = asyncio.create_task(
                find_m3u8_via_network(page, M3U8_TIMEOUT)
            )

            switched = await click_server_button(page, VIDEO_SERVER)
            # click_server_button 内部等待 5 秒——DS 播放器 iframe 在这段时间里
            # 完成加载并预取 m3u8，新 task 会自动捕获到。

            if switched:
                if m3u8_task.done():
                    print("  ✓ DS 播放器预加载期间已捕获 m3u8")
                else:
                    print("  ✓ 等待播放触发...")

            # 服务器切换后检测 player iframe 内的 CF 验证并等待通过
            await wait_for_player_cf(page)

            # 打印切换后的 frame 列表（调试：看 DS 播放器加载了哪个 iframe）
            print("  ▶ 切换后页面 frames:")
            for i, f in enumerate(page.frames):
                print(f"    [{i}] {f.url}")

            # 自动检测播放器 iframe URL（等待嵌套 player embed 出现，最多15秒）
            print("  ▶ 等待播放器 iframe 出现...")
            _detected_player_url = await _find_player_iframe_url(page)
            if _detected_player_url:
                print(f"  ✓ 自动检测到播放器 URL: {_detected_player_url[:80]}")
            else:
                print("  ⚠ 未检测到播放器 iframe（将继续常规提取流程）")
        else:
            # 未配置服务器时打印 frames 供参考
            print("  ▶ 页面 frames:")
            for i, f in enumerate(page.frames):
                print(f"    [{i}] {f.url}")

            _detected_player_url = await _find_player_iframe_url(page)
            if _detected_player_url:
                print(f"  ✓ 自动检测到播放器 URL: {_detected_player_url[:80]}")

        # 触发播放（如果 m3u8 尚未被预加载时捕获到）
        m3u8_url: str | None = None
        if not m3u8_task.done():
            print("\n  ▶ 触发视频播放...")
            await try_click_play(page)
            # 点击播放后检测 player iframe CF（点击可能触发 CF 验证）
            cf_cleared = await wait_for_player_cf(page)
            if cf_cleared:
                # CF 通过后视频已开始播放，立即从播放器 API 读取 URL
                # （video.currentSrc 在 iframe 里，extract_m3u8_fallback 会扫描所有 frame）
                await asyncio.sleep(2)
                m3u8_url = await extract_m3u8_fallback(page)
                if m3u8_url:
                    print(f"  ✓ CF 通过后从播放器 API 直接获取 URL（无需等待网络拦截）")
            if not m3u8_url:
                await asyncio.sleep(1)

        # 等待网络拦截结果（如果播放器 API 没拿到）
        if not m3u8_url:
            if m3u8_task.done():
                try:
                    m3u8_url = m3u8_task.result()
                except Exception:
                    m3u8_url = None
            else:
                try:
                    m3u8_url = await asyncio.wait_for(
                        asyncio.shield(m3u8_task), timeout=30
                    )
                except asyncio.TimeoutError:
                    m3u8_url = None

        # 最终备用：DOM/JS 全量搜索
        if not m3u8_url:
            print("  ▶ 网络拦截无结果，尝试 DOM/JS 搜索...")
            m3u8_url = await extract_m3u8_fallback(page)

        if not m3u8_url:
            if _detected_player_url:
                # 常规流程未拿到流，但有播放器 iframe URL——关闭当前浏览器，
                # 用专用函数直接加载播放器页面重新提取
                print(f"\n  ▶ 常规提取失败，切换到直接播放器模式...")
                print(f"    播放器: {_detected_player_url[:80]}")
                await browser.close()
            else:
                screenshot = DOWNLOAD_DIR / "debug.png"
                await page.screenshot(path=str(screenshot), full_page=True)
                print(f"\n  ✗ 未找到 m3u8（页面已正常加载，但未检测到视频流）")
                print(f"  截图: {screenshot}  当前 URL: {page.url}")
                await browser.close()
                sys.exit(1)
        else:
            print(f"\n  ✓ m3u8 URL:\n    {m3u8_url}")
            await browser.close()

    # 若常规流程失败但检测到了播放器 iframe URL，直接加载播放器页面提取
    if not m3u8_url and _detected_player_url:
        m3u8_url = await _extract_from_player_url(_detected_player_url)
        if not m3u8_url:
            print("\n  ✗ 未能从播放器 URL 中提取视频流")
            sys.exit(1)
        print(f"\n  ✓ m3u8 URL:\n    {m3u8_url}")

    # 第三步：下载
    output = DOWNLOAD_DIR / "video.mp4"
    print(f"\n  ▶ 开始下载 → {output}")
    ok = await download_m3u8(m3u8_url, output)
    if not ok:
        print(f"\n  ✗ 下载失败，可手动用 ffmpeg 执行:")
        print(f'  ffmpeg -i "{m3u8_url}" -c copy output.mp4')
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

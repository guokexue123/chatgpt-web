#!/usr/bin/env python3
"""
视频下载器 - 网络请求拦截版
通过拦截浏览器网络请求来捕获 m3u8 URL，避免 DOM 解析失败的问题。
"""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Page, BrowserContext

# ===== 用户配置 =====
TARGET_URL = "https://supjav.com/132824.html"
DOWNLOAD_DIR = Path("downloads")
HEADLESS = False  # True=后台运行, False=显示浏览器窗口（调试推荐）

# 从 F12 → 请求头 → Cookie 字段完整复制粘贴
COOKIE_STRING = (
    "cf_clearance=fXsEx8zWa5v5zwmkgiZd1ZkNwMLSogUyZD0Wkkq29XE-1782003467-1.2.1.1-"
    "fo9agvoxl4edc_ckpOnalaBc5b8b7Le_n1rHLJI2xFEKgNM5MY9YWiC6h57Zk7_0.U7zcQq0kA5i43qt8WXvU3wk"
    "pQUwm6dH5Dbf7RFolwqaZiPTSNb5Vb4DO87LwPpmwII9FoalZJ9vVdajamEp5yZzenRALqdYkRAb0suySGf9ABEaZk"
    "gdQEdqaKFo.jhyaBOd2jpS8StVAq2e6oZOmhH5vYPTUP5.6jRYiu3xCe2rNPTtiQ5ssMJsBkZLzKZ_LLrGsA5_mr6k"
    "h5i5gKQ3kEfg0iHWLg78PQIqox3O1j.hqmzAl1gAF1Jq5_SEJCkE5exu19Y7vJeljKZFD6N8GIl7Wb44vYlOICEr6K"
    "faOLm6tH8euOQwMbLBJtddthmPfQ0hHZZxU9UMWVLr619uEVC1osiUm_2qOFbPPH5ai2UVoV1R4ud.tVCKTwXjSFtp"
    "; _cfuvid=yNP3lLSXAWcNQtb9vgPDWc9ToIJM6OSBASmn3VvMiYo-1782002441.7939458-1.0.1.1-"
    "4.i89FJemT4cqyvrPJRm3muZZBGBi88mcD2jEuVuuBs"
)

# 与 F12 里看到的完全一致的 User-Agent
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"

# 等待 m3u8 出现的最长时间（秒）
M3U8_TIMEOUT = 60

# ===== 配置结束 =====

M3U8_PATTERN = re.compile(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', re.IGNORECASE)


def parse_cookies(cookie_str: str, domain: str) -> list[dict]:
    """把 Cookie 字符串解析成 Playwright 所需的格式"""
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


async def wait_for_cf_clearance(page: Page, timeout: int = 30) -> bool:
    """
    等待 Cloudflare 验证真正通过：
    检测条件是页面不再包含 CF 验证 iframe。
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        frames = page.frames
        cf_frames = [f for f in frames if "challenges.cloudflare.com" in f.url]
        if not cf_frames:
            return True
        await asyncio.sleep(1)
    return False


async def find_m3u8_via_network(page: Page, timeout: int = M3U8_TIMEOUT) -> str | None:
    """
    核心方法：监听所有网络请求/响应，捕获 m3u8 URL。
    比 DOM 解析可靠得多，能捕获动态加载的流地址。
    """
    found = asyncio.Event()
    m3u8_url: list[str] = []

    def on_request(request):
        url = request.url
        if ".m3u8" in url.lower():
            if not m3u8_url:
                print(f"  ▶ [网络拦截-请求] 发现 m3u8: {url[:100]}...")
                m3u8_url.append(url)
                found.set()

    def on_response(response):
        url = response.url
        if ".m3u8" in url.lower():
            if not m3u8_url:
                print(f"  ▶ [网络拦截-响应] 发现 m3u8: {url[:100]}...")
                m3u8_url.append(url)
                found.set()

    page.on("request", on_request)
    page.on("response", on_response)

    # 同时也监听所有子 frame 的请求
    def on_frame_navigated(frame):
        url = frame.url
        if ".m3u8" in url.lower() and not m3u8_url:
            print(f"  ▶ [Frame导航] 发现 m3u8: {url[:100]}...")
            m3u8_url.append(url)
            found.set()

    page.on("framenavigated", on_frame_navigated)

    try:
        await asyncio.wait_for(found.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)
        page.remove_listener("framenavigated", on_frame_navigated)

    return m3u8_url[0] if m3u8_url else None


async def try_click_play(page: Page):
    """尝试点击视频播放按钮，触发视频加载"""
    selectors = [
        "video",
        ".play-btn",
        ".vjs-big-play-button",
        "[class*='play']",
        "button[aria-label*='play' i]",
        ".player-container",
        "#player",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click(timeout=3000)
                print(f"  ✓ 点击了播放元素: {sel}")
                return
        except Exception:
            continue

    # 尝试点击 iframe 内的播放按钮
    for frame in page.frames:
        if frame.url and "supjav.com" not in frame.url and "cloudflare.com" not in frame.url:
            for sel in selectors:
                try:
                    el = frame.locator(sel).first
                    if await el.count() > 0:
                        await el.click(timeout=3000)
                        print(f"  ✓ 在 iframe 中点击了播放元素: {sel} ({frame.url[:60]})")
                        return
                except Exception:
                    continue


async def extract_m3u8_from_dom(page: Page) -> str | None:
    """从页面 HTML 源码里用正则搜索 m3u8（备用方案）"""
    try:
        html = await page.content()
        match = M3U8_PATTERN.search(html)
        if match:
            return match.group(0)
    except Exception:
        pass

    # 搜索所有 frame
    for frame in page.frames:
        try:
            html = await frame.content()
            match = M3U8_PATTERN.search(html)
            if match:
                return match.group(0)
        except Exception:
            continue

    return None


async def extract_m3u8_from_js(page: Page) -> str | None:
    """通过 JS 执行查找 m3u8 相关变量"""
    scripts = [
        # JW Player
        "(() => { try { return jwplayer().getPlaylistItem().file } catch(e) { return null } })()",
        # Video.js
        "(() => { try { return videojs(document.querySelector('video')).currentSrc() } catch(e) { return null } })()",
        # 通用 window 变量搜索
        """(() => {
            const keys = Object.keys(window);
            for (const k of keys) {
                try {
                    const v = String(window[k]);
                    if (v.includes('.m3u8')) {
                        const m = v.match(/https?:\\/\\/[^\\s"'<>]+\\.m3u8[^\\s"'<>]*/i);
                        if (m) return m[0];
                    }
                } catch(e) {}
            }
            return null;
        })()""",
        # 搜索 script 标签内容
        """(() => {
            const scripts = document.querySelectorAll('script');
            for (const s of scripts) {
                const m = s.textContent.match(/https?:\\/\\/[^\\s"'<>]+\\.m3u8[^\\s"'<>]*/i);
                if (m) return m[0];
            }
            return null;
        })()""",
    ]

    for script in scripts:
        try:
            result = await page.evaluate(script)
            if result and ".m3u8" in result.lower():
                return result
        except Exception:
            continue

    return None


def download_m3u8(m3u8_url: str, output_path: Path, referer: str) -> bool:
    """用 ffmpeg 下载 m3u8 流"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-headers", f"Referer: {referer}\r\nUser-Agent: {USER_AGENT}\r\n",
        "-i", m3u8_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(output_path),
    ]
    print(f"\n  ▶ 执行 ffmpeg 下载...\n  命令: {' '.join(cmd[:6])} ...")
    result = subprocess.run(cmd, capture_output=False)
    return result.returncode == 0


async def main():
    print("视频下载器 (网络拦截版)")
    print("=" * 60)

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    domain = urlparse(TARGET_URL).netloc
    cookies = parse_cookies(COOKIE_STRING, domain)
    print(f"  ✓ 已解析 {len(cookies)} 个 Cookie (域名: {domain})")

    async with async_playwright() as p:
        # 使用 Firefox，与产生 Cookie 的浏览器一致
        browser = await p.firefox.launch(headless=HEADLESS)
        context: BrowserContext = await browser.new_context(
            user_agent=USER_AGENT,
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,en-US;q=0.6,en;q=0.5",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            },
            viewport={"width": 1920, "height": 1080},
        )

        # 在导航之前注入 Cookie（关键：必须先设 cookie 再加载页面）
        await context.add_cookies(cookies)
        print(f"  ✓ Cookie 已注入浏览器上下文")

        page = await context.new_page()

        # 启动网络监听（在导航前注册，确保不遗漏任何请求）
        m3u8_task = asyncio.create_task(find_m3u8_via_network(page, timeout=M3U8_TIMEOUT))

        print(f"\n  ▶ 加载页面: {TARGET_URL}")
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  ⚠ 页面加载超时（继续等待）: {e}")

        # 等待 CF 验证真正通过（检查是否还有 CF challenge iframe）
        print("  ▶ 等待 Cloudflare 验证通过...")
        cf_passed = await wait_for_cf_clearance(page, timeout=30)
        if cf_passed:
            print("  ✓ Cloudflare 验证已通过（无 challenge iframe）")
        else:
            print("  ⚠ 仍检测到 Cloudflare challenge，尝试继续...")
            if not HEADLESS:
                print("  ⚠ 请在浏览器窗口手动完成验证，等待 30 秒...")
                await asyncio.sleep(30)

        # 等待页面稳定
        await asyncio.sleep(3)

        # 打印当前所有 frame，帮助调试
        print(f"\n  ▶ 当前页面 frames:")
        for i, frame in enumerate(page.frames):
            print(f"    frame[{i}] url={frame.url}")

        # 先检查网络拦截是否已经找到 m3u8
        if m3u8_task.done() and m3u8_task.result():
            m3u8_url = m3u8_task.result()
        else:
            # 触发播放，激活视频加载
            print("\n  ▶ 尝试触发视频播放...")
            await try_click_play(page)
            await asyncio.sleep(3)

            # 等待网络拦截结果（最多再等 30 秒）
            print(f"  ▶ 等待网络请求中的 m3u8（最多 {M3U8_TIMEOUT} 秒）...")
            m3u8_url = await asyncio.wait_for(m3u8_task, timeout=M3U8_TIMEOUT) if not m3u8_task.done() else m3u8_task.result()

        if not m3u8_url:
            # 备用方案：DOM + JS 搜索
            print("  ▶ 网络拦截未找到，尝试 DOM 搜索...")
            m3u8_url = await extract_m3u8_from_dom(page)

        if not m3u8_url:
            print("  ▶ 尝试 JS 变量搜索...")
            m3u8_url = await extract_m3u8_from_js(page)

        if not m3u8_url:
            screenshot_path = DOWNLOAD_DIR / "debug_screenshot.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"\n  ✗ 无法找到 m3u8 URL，截图已保存: {screenshot_path}")
            print("\n  诊断信息：")
            print(f"    当前页面 URL: {page.url}")
            print(f"    页面标题: {await page.title()}")
            html_preview = (await page.content())[:500]
            print(f"    页面内容预览:\n{html_preview}")
            await browser.close()
            sys.exit(1)

        print(f"\n  ✓ 找到 m3u8 URL:\n    {m3u8_url}")
        await browser.close()

    # 下载
    output_file = DOWNLOAD_DIR / "video.mp4"
    print(f"\n  ▶ 开始下载到: {output_file}")
    success = download_m3u8(m3u8_url, output_file, referer=TARGET_URL)

    if success:
        print(f"\n  ✓ 下载完成！文件: {output_file}")
    else:
        print(f"\n  ✗ ffmpeg 下载失败")
        print(f"  可手动执行:")
        print(f"  ffmpeg -i \"{m3u8_url}\" -c copy output.mp4")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

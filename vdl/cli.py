#!/usr/bin/env python3
"""
vdl/cli.py — 浏览器辅助视频流下载工具（TLS 指纹绕过版）

功能：
  · 用 Playwright Chromium 打开目标页面，从网络请求中捕获 HLS / 直链视频 URL
  · 用 curl-cffi 伪造 Chrome TLS 指纹下载（绕过 CDN JA3/JA4 指纹检测）
  · curl-cffi 不可用时降级为 requests（部分 CDN 仍会拒绝）
  · 最终合并用 ffmpeg（m3u8 分段流）或直接保存（MP4 直链）

依赖安装：
  pip install playwright playwright-stealth curl-cffi
  playwright install chromium

用法：
  python vdl/cli.py URL [选项]          # 直接传 URL
  python vdl/cli.py                      # 使用脚本内 MANUAL_URL

选项：
  -b / --browser         启用浏览器模式（Playwright Chromium，必须）
  --profile PATH         持久化 Profile 目录（复用 Cookie / 登录状态）
  --no-headless          显示浏览器窗口（调试 / 手动过 CF 验证）
  --keep-browser         完成后保持浏览器窗口不关闭
  --browser-timeout N    等待视频 URL 的最长秒数（默认 300）
  -q QUALITY             目标画质，如 1080p / 720p / 480p（默认 1080p）
  -o FILE                输出文件路径（默认 downloads/<title>.mp4）
  --ffmpeg PATH          ffmpeg 可执行文件路径（留空自动查找）
  --no-tls-bypass        禁用 curl-cffi TLS 伪造（回退到标准 requests）
"""

import argparse
import asyncio
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urljoin

# ===== 用户配置（CLI 参数会覆盖这里）=====
MANUAL_URL = ""                     # 留空则必须用命令行传入 URL
BROWSER_PROFILE = ""                # 持久化 Profile 目录路径；留空则用临时 profile
HEADLESS = True
KEEP_BROWSER = False
BROWSER_TIMEOUT = 300               # 秒
TARGET_QUALITY = "1080p"
DOWNLOAD_DIR = Path("downloads")
FFMPEG_PATH = ""                    # 留空自动查找
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
# ===== 配置结束 =====

_HLS_CT = ("application/vnd.apple.mpegurl", "application/x-mpegurl",
            "audio/mpegurl", "audio/x-mpegurl")

_SKIP_CT = ("image/", "font/", "text/javascript", "text/css",
            "text/html", "application/javascript", "application/json")

_SKIP_EXT = ('.js', '.css', '.html', '.htm', '.json', '.png', '.jpg',
             '.jpeg', '.gif', '.svg', '.woff', '.woff2', '.ttf', '.eot', '.ico')

_AD_KW = ("doubleclick", "googlesyndication", "adnxs", "amazon-adsystem",
          "tracker", "tracking", "analytics", "banner", "widget")

_QUALITY_ORDER = ["2160p", "1440p", "1080p", "720p", "480p", "360p", "240p"]


# ────────────────────────────────────────────────────────
CURL_CFFI 旁路 路 HTTP 客户端
# ────────────────────────────────────────────────────────

def _make_tls_session(impersonate: str = "chrome120"):
    """
    返回一个伪造 Chrome TLS 指纹的同步 HTTP Session。
    优先使用 curl-cffi；不可用时返回普通 requests.Session（部分 CDN 会拒绝）。
    """
    try:
        from curl_cffi import requests as curl_req
        session = curl_req.Session(impersonate=impersonate)
        session._tls_bypass = True
        return session
    except ImportError:
        import requests as req
        session = req.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        session._tls_bypass = False
        return session


def _tls_get(url: str, headers: dict | None = None,
             timeout: int = 30, stream: bool = False):
    """单次 GET，自动选择 TLS 旁路客户端。"""
    session = _make_tls_session()
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    if getattr(session, "_tls_bypass", False):
        return session.get(url, headers=h, timeout=timeout, stream=stream)
    else:
        import requests
        return requests.get(url, headers=h, timeout=timeout, stream=stream,
                            verify=False)


def _check_tls_bypass_available() -> bool:
    try:
        import curl_cffi  # noqa
        return True
    except ImportError:
        return False


# ────────────────────────────────────────────────────────
CDN 预检
# ────────────────────────────────────────────────────────

def _precheck_url(url: str, referer: str = "") -> tuple[bool, str]:
    """
    对 CDN URL 做一次 Range 预检，返回 (ok, reason)。
    · 使用 curl-cffi 伪造 TLS 指纹
    · HTTP 200 / 206 均视为成功
    """
    headers = {
        "Referer": referer,
        "Range": "bytes=0-0",
        "Accept": "*/*",
    }
    try:
        resp = _tls_get(url, headers=headers, timeout=10)
        code = resp.status_code
        if code in (200, 206):
            return True, f"HTTP {code}"
        return False, f"HTTP {code}"
    except Exception as e:
        msg = str(e)
        if "tls" in msg.lower() or "ssl" in msg.lower() or "handshake" in msg.lower():
            return False, "TLS 握手被中断"
        if "connection" in msg.lower():
            return False, "连接被拒绝"
        return False, str(e)[:60]


# ────────────────────────────────────────────────────────
质量选择
# ────────────────────────────────────────────────────────

def _parse_m3u8_variants(content: str, base_url: str) -> list[tuple[str, str]]:
    """
    解析 HLS master playlist，返回 [(quality_label, url), ...] 按从高到低排序。
    quality_label 形如 "1080p", "720p", …
    """
    variants: list[tuple[int, str, str]] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            attrs = dict(re.findall(r'(\w[\w-]*)=(["\w@,.]+)', line))
            res = attrs.get("RESOLUTION", "")
            bandwidth = int(attrs.get("BANDWIDTH", "0"))
            height = 0
            if "x" in res:
                try:
                    height = int(res.split("x")[1])
                except ValueError:
                    pass
            if i + 1 < len(lines):
                seg_url = lines[i + 1].strip()
                if seg_url and not seg_url.startswith("#"):
                    if not seg_url.startswith("http"):
                        seg_url = urljoin(base_url, seg_url)
                    label = f"{height}p" if height else f"{bandwidth // 1000}k"
                    variants.append((height or bandwidth, label, seg_url))
                i += 2
                continue
        i += 1
    variants.sort(key=lambda x: x[0], reverse=True)
    return [(lbl, url) for _, lbl, url in variants]


def _pick_quality(variants: list[tuple[str, str]], target: str) -> tuple[str, str]:
    """
    从 variants 中选最接近 target 的质量，返回 (chosen_label, url)。
    """
    if not variants:
        raise ValueError("variants 为空")
    target_height = 0
    m = re.match(r"(\d+)p", target)
    if m:
        target_height = int(m.group(1))

    exact = [(lbl, url) for lbl, url in variants if lbl == target]
    if exact:
        return exact[0]

    # 找最近的高度
    def _height(lbl: str) -> int:
        m2 = re.match(r"(\d+)p", lbl)
        return int(m2.group(1)) if m2 else 0

    if target_height:
        best = min(variants, key=lambda v: abs(_height(v[0]) - target_height))
        print(f"  源里没有 {target}，取最接近的 {best[0]}")
        return best
    return variants[0]


# ────────────────────────────────────────────────────────
播放列表保存
# ────────────────────────────────────────────────────────

def _save_playlist(url: str, content: str, out_dir: Path) -> Path:
    """将 m3u8 内容保存到本地，相对 URL → 绝对 URL。"""
    base = url.rsplit("/", 1)[0] + "/"
    lines = []
    for line in content.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("http"):
            line = urljoin(base, s)
        lines.append(line)
    local = out_dir / "playlist_cache.m3u8"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("\n".join(lines), encoding="utf-8")
    return local


# ────────────────────────────────────────────────────────
ffmpeg 下载
# ────────────────────────────────────────────────────────

def _find_ffmpeg(explicit: str = "") -> str | None:
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        return found
    for p in [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ]:
        if Path(p).exists():
            return p
    return None


def _ffmpeg_download(m3u8_path: str, output: Path, referer: str,
                     ffmpeg_bin: str) -> bool:
    """用 ffmpeg 下载 m3u8 流并封装为 mp4。"""
    cmd = [
        ffmpeg_bin, "-y",
        "-allowed_extensions", "ALL",
        "-headers", f"Referer: {referer}\r\nUser-Agent: {USER_AGENT}\r\n",
        "-i", m3u8_path,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(output),
    ]
    print(f"  ffmpeg: {ffmpeg_bin}")
    result = subprocess.run(cmd)
    return result.returncode == 0


# ────────────────────────────────────────────────────────
curl-cffi 分段下载（ffmpeg 不可用时的备用）
# ────────────────────────────────────────────────────────

def _download_segments(m3u8_url: str, content: str, output: Path,
                       referer: str) -> bool:
    """
    解析 m3u8，用 curl-cffi 逐段下载 .ts，合并输出。
    不支持 AES-128 加密流（需 ffmpeg）。
    """
    base = m3u8_url.rsplit("/", 1)[0] + "/"
    if "#EXT-X-KEY" in content:
        print("  ✗ 加密流不支持纯 Python 下载，需要 ffmpeg")
        return False

    segments = []
    for line in content.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            url = s if s.startswith("http") else urljoin(base, s)
            segments.append(url)

    if not segments:
        print("  ✗ playlist 中无分段")
        return False

    print(f"  共 {len(segments)} 个分段，curl-cffi 下载中...")
    output.parent.mkdir(parents=True, exist_ok=True)
    ts_out = output.with_suffix(".ts")
    headers = {"Referer": referer}

    with open(ts_out, "wb") as fh:
        for i, seg_url in enumerate(segments, 1):
            for attempt in range(3):
                try:
                    resp = _tls_get(seg_url, headers=headers, timeout=20)
                    fh.write(resp.content)
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"  ✗ 分段 {i}/{len(segments)} 失败: {e}")
                        return False
                    time.sleep(1)
            if i % 20 == 0 or i == len(segments):
                print(f"  ▶ {i}/{len(segments)} ({i*100//len(segments)}%)", end="\r")

    print(f"\n  ✓ 已保存: {ts_out}")
    return True


# ────────────────────────────────────────────────────────
直链 MP4 下载（Streamtape 等）
# ────────────────────────────────────────────────────────

def _download_direct(url: str, output: Path, referer: str) -> bool:
    """
    直链下载（非 m3u8），用 curl-cffi 流式写入，显示进度。
    """
    print(f"  直链下载: {url[:80]}")
    headers = {"Referer": referer}
    try:
        resp = _tls_get(url, headers=headers, timeout=60, stream=True)
        total = int(resp.headers.get("content-length", 0))
        output.parent.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        with open(output, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded * 100 // total
                        mb = downloaded / 1_048_576
                        print(f"  ▶ {mb:.1f} MB ({pct}%)", end="\r")
        print(f"\n  ✓ 已保存: {output}")
        return True
    except Exception as e:
        print(f"  ✗ 直链下载失败: {e}")
        return False


# ────────────────────────────────────────────────────────
下载入口
# ────────────────────────────────────────────────────────

def download_url(url: str, output: Path, referer: str,
                 quality: str = "1080p", ffmpeg_bin: str | None = None) -> bool:
    """
    根据 URL 类型选择下载策略：
      · m3u8 master playlist → 选质量 → ffmpeg（或 curl-cffi 分段）
      · m3u8 media playlist  → ffmpeg（或 curl-cffi 分段）
      · 直链 mp4 / ts        → curl-cffi 流式下载
    """
    is_m3u8 = ".m3u8" in url.lower() or url.endswith("/")

    # 1. 获取内容
    headers = {"Referer": referer}
    try:
        resp = _tls_get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "").lower()
        body_text = resp.text
    except Exception as e:
        print(f"  ✗ 获取 URL 失败: {e}")
        return False

    # 2. 判断是否 HLS playlist
    is_playlist = (
        ".m3u8" in url.lower()
        or any(ct in content_type for ct in _HLS_CT)
        or body_text.strip().startswith("#EXTM3U")
    )

    if not is_playlist:
        # 直链下载
        return _download_direct(url, output, referer)

    # 3. Master playlist → 选质量
    base_url = url.rsplit("/", 1)[0] + "/"
    if "#EXT-X-STREAM-INF" in body_text:
        variants = _parse_m3u8_variants(body_text, base_url)
        if not variants:
            print("  ✗ 无法解析 variants")
            return False
        chosen_lbl, chosen_url = _pick_quality(variants, quality)
        print(f"  ✓ 选择画质: {chosen_lbl}")
        try:
            resp2 = _tls_get(chosen_url, headers=headers, timeout=30)
            resp2.raise_for_status()
            media_content = resp2.text
        except Exception as e:
            print(f"  ✗ 获取 media playlist 失败: {e}")
            return False
        media_local = _save_playlist(chosen_url, media_content, output.parent)
        print(f"  ✓ 播放列表已保存: {media_local}")
        m3u8_path = str(media_local)
    else:
        # 直接是 media playlist
        media_local = _save_playlist(url, body_text, output.parent)
        m3u8_path = str(media_local)

    # 4. ffmpeg 下载（首选）
    if ffmpeg_bin:
        if _ffmpeg_download(m3u8_path, output, referer, ffmpeg_bin):
            return True
        print("  ✗ ffmpeg 失败，尝试 curl-cffi 分段下载...")

    # 5. curl-cffi 分段下载（备用）
    if "#EXT-X-STREAM-INF" in body_text:
        final_content = media_content  # type: ignore[possibly-undefined]
        final_url = chosen_url  # type: ignore[possibly-undefined]
    else:
        final_content = body_text
        final_url = url
    return _download_segments(final_url, final_content, output, referer)


# ────────────────────────────────────────────────────────
Playwright 浏览器捕获
# ────────────────────────────────────────────────────────

async def _browser_capture(
    target_url: str,
    timeout: int,
    headless: bool,
    profile: str,
    keep_browser: bool,
    quality: str,
    output: Path,
    ffmpeg_bin: str | None,
    use_tls_bypass: bool,
) -> bool:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("✗ 请先安装: pip install playwright && playwright install chromium")
        return False

    captured: list[str] = []          # m3u8 / mp4 URLs
    found_event = asyncio.Event()
    tls_bypass_ok = _check_tls_bypass_available() and use_tls_bypass

    if tls_bypass_ok:
        print("  ✓ curl-cffi 可用 — 启用 TLS 指纹伪造（Chrome 模式）")
    else:
        print("  ⚠ curl-cffi 不可用，使用标准 TLS（部分 CDN 可能拒绝）")
        print("    安装方法: pip install curl-cffi")

    def _is_video(url: str, ct: str = "") -> bool:
        u = url.lower().split("?")[0]
        if ".m3u8" in url.lower() or ".ts" in u:
            return True
        ct_low = ct.lower()
        if any(t in ct_low for t in ("mpegurl", "video/", "octet-stream")):
            return True
        return False

    def _is_ad(url: str) -> bool:
        return any(kw in url.lower() for kw in _AD_KW)

    async def on_response(response):
        if found_event.is_set():
            return
        url = response.url
        if _is_ad(url):
            return
        ct = ""
        try:
            ct = response.headers.get("content-type", "")
        except Exception:
            pass
        if not _is_video(url, ct):
            # 候选：无扩展名、非标准 CT 的响应可能是 m3u8
            url_clean = url.lower().split("?")[0]
            if any(url_clean.endswith(e) for e in _SKIP_EXT):
                return
            if any(ct.lower().startswith(t) for t in _SKIP_CT):
                return
            try:
                body = await response.text()
                if not body.strip().startswith("#EXTM3U"):
                    return
            except Exception:
                return

        if not captured:
            captured.append(url)
            print(f"\n  ● 捕获: {url[:100]}")
            found_event.set()

    async with async_playwright() as pw:
        launch_args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        if profile:
            context = await pw.chromium.launch_persistent_context(
                profile,
                headless=headless,
                args=launch_args,
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
            )
            page = context.pages[0] if context.pages else await context.new_page()
        else:
            browser = await pw.chromium.launch(headless=headless, args=launch_args)
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
            )
            page = await context.new_page()

        # Stealth（可选）
        try:
            from playwright_stealth import stealth_async
            await stealth_async(page)
        except Exception:
            pass

        page.on("response", on_response)

        print(f"  ▶ 加载: {target_url}")
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  ⚠ 加载超时（继续）: {e}")

        # 自动点击播放
        await _auto_click_play(page)

        deadline = time.time() + timeout
        printed_wait = False
        while not found_event.is_set():
            remaining = int(deadline - time.time())
            if remaining <= 0:
                print(f"\n  ✗ {timeout} 秒内未捕获到视频 URL")
                if not keep_browser:
                    await context.close()
                return False
            if not printed_wait:
                print(f"  等待中…（还剩 {remaining//60} 分 {remaining%60} 秒，"
                      f"完成验证并点击播放即可继续；--browser-timeout 可调长）",
                      end="\r")
                printed_wait = True
            await asyncio.sleep(2)

        video_url = captured[0]
        if not keep_browser:
            await context.close()
        else:
            print("\n  ℹ --keep-browser 已设置：浏览器保持打开")

    # 下载
    print(f"\n  ▶ 开始下载...")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # CDN 预检
    print(f"  ▶ CDN 预检: {video_url[:80]}")
    ok, reason = _precheck_url(video_url, referer=target_url)
    if ok:
        print(f"    ✓ {reason}")
    else:
        print(f"    ✗ {reason}")
        if not tls_bypass_ok:
            print("    ↳ 安装 curl-cffi 可绕过 TLS 指纹: pip install curl-cffi")
            return False
        else:
            print("    ↳ curl-cffi 预检也失败，URL 可能已过期")
            return False

    return download_url(video_url, output, target_url, quality, ffmpeg_bin)


async def _auto_click_play(page) -> None:
    """
    尝试自动触发播放。
    注意：CF 验证未完成时点击可能无效，需用户手动完成验证后再点击。
    """
    # 等待 CF 挑战消失
    for _ in range(20):
        cf_frames = [f for f in page.frames
                     if "challenges.cloudflare.com" in (f.url or "")]
        if not cf_frames:
            break
        await asyncio.sleep(1)

    selectors = [
        "video",
        ".vjs-big-play-button",
        ".jw-icon-display",
        ".play-btn",
        "[class*='play']",
        "[aria-label*='play' i]",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if await el.count() > 0:
                await el.click(timeout=3000)
                print(f"  ✓ 自动点击播放: {sel}")
                return
        except Exception:
            pass

    # 在所有 iframe 中尝试
    for frame in page.frames:
        if not frame.url or "challenges.cloudflare.com" in frame.url:
            continue
        if any(kw in frame.url for kw in _AD_KW):
            continue
        for sel in selectors:
            try:
                el = frame.locator(sel).first
                if await el.count() > 0:
                    await el.click(timeout=2000)
                    print(f"  ✓ 自动点击播放（iframe）: {sel}")
                    return
            except Exception:
                pass

    print("  ⚠ 未找到播放按鈕，请手动点击浏览器中的播放按鈕")


# ────────────────────────────────────────────────────────
CLI 入口
# ────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python vdl/cli.py",
        description="浏览器辅助视频流下载（TLS 指纹绕过）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", nargs="?", default="", help="目标页面 URL")
    p.add_argument("-b", "--browser", action="store_true",
                   help="启用 Playwright 浏览器模式（必须）")
    p.add_argument("--profile", default=BROWSER_PROFILE, metavar="PATH",
                   help="持久化浏览器 Profile 目录")
    p.add_argument("--headless", dest="headless", action="store_true",
                   default=HEADLESS, help="无头模式（默认）")
    p.add_argument("--no-headless", dest="headless", action="store_false",
                   help="显示浏览器窗口（调试用）")
    p.add_argument("--keep-browser", action="store_true", default=KEEP_BROWSER,
                   help="完成后保持浏览器窗口")
    p.add_argument("--browser-timeout", type=int, default=BROWSER_TIMEOUT,
                   metavar="N", help="等待视频 URL 的超时秒数（默认 300）")
    p.add_argument("-q", "--quality", default=TARGET_QUALITY,
                   help="目标画质（1080p / 720p / 480p，默认 1080p）")
    p.add_argument("-o", "--output", default="", metavar="FILE",
                   help="输出文件路径")
    p.add_argument("--ffmpeg", default=FFMPEG_PATH, metavar="PATH",
                   help="ffmpeg 路径（留空自动查找）")
    p.add_argument("--no-tls-bypass", action="store_true",
                   help="禁用 curl-cffi TLS 伪造（回退到标准 requests）")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    target_url = args.url or MANUAL_URL
    if not target_url:
        print("✗ 未指定 URL，请用命令行传入或设置脚本内 MANUAL_URL")
        return 1

    # 输出路径
    if args.output:
        output = Path(args.output)
    else:
        slug = re.sub(r'[^\w\-]', '_', urlparse(target_url).path.strip("/"))[:60]
        output = DOWNLOAD_DIR / f"{slug or 'video'}.mp4"

    # ffmpeg
    ffmpeg_bin = _find_ffmpeg(args.ffmpeg)
    if not ffmpeg_bin:
        print("  ⚠ 未找到 ffmpeg，将尝试 curl-cffi 分段下载（仅支持非加密流）")

    use_tls_bypass = not args.no_tls_bypass

    print(f"目标: {target_url}")
    print(f"画质: {args.quality}  输出: {output}")
    print(f"TLS 旁路: {'启用 (curl-cffi)' if use_tls_bypass else '禁用'}")

    if not args.browser:
        # 非浏览器模式：直接下载（URL 已知）
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        ok = download_url(target_url, output, target_url,
                          args.quality, ffmpeg_bin)
        return 0 if ok else 1

    # 浏览器模式
    ok = asyncio.run(_browser_capture(
        target_url=target_url,
        timeout=args.browser_timeout,
        headless=args.headless,
        profile=args.profile,
        keep_browser=args.keep_browser,
        quality=args.quality,
        output=output,
        ffmpeg_bin=ffmpeg_bin,
        use_tls_bypass=use_tls_bypass,
    ))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
磁力链视频下载工具
- 解析磁力链接 (magnet:?xt=urn:btih:...)
- 多线程/多片段并发下载 (libtorrent 内置)
- 自动识别视频文件并在下载初期预览
- 实时显示下载进度、速度、ETA
"""

import os
import sys
import re
import time
import threading
import subprocess
import shutil
import signal
import argparse
from pathlib import Path
from urllib.parse import unquote_plus
from dataclasses import dataclass, field
from typing import Optional, List

try:
    import libtorrent as lt
except ImportError:
    print("错误: 未找到 libtorrent，请运行: pip install libtorrent", file=sys.stderr)
    sys.exit(1)

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


# ══════════════════════════════════════════════
# 配置项 —— 修改此处即可调整所有默认行为
# ══════════════════════════════════════════════
@dataclass
class Config:
    # 下载目录
    save_path:        str   = './downloads'

    # ── 连接 / 速度 ──
    connections:      int   = 300    # 最大并发 peer 连接数
    upload_limit_kb:  int   = 50     # 上传限速（KB/s），0 = 不限速
    download_limit_kb:int   = 0      # 下载限速（KB/s），0 = 不限速

    # ── 元数据 ──
    meta_timeout_sec: int   = 90     # 等待种子元数据的最长秒数

    # ── 预览 ──
    preview:          bool  = True   # 是否在下载初期自动预览视频
    preview_sec:      int   = 5      # 预览时长（秒）
    min_preview_mb:   float = 8.0    # 触发预览所需的最少已下载量（MB）


# 全局默认配置实例（argparse 从此处读取默认值）
DEFAULT = Config()

# 支持的视频格式
VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.ts', '.m2ts', '.rmvb', '.rm'}

# 颜色输出
class C:
    RESET  = '\033[0m'
    GREEN  = '\033[92m'
    YELLOW = '\033[93m'
    CYAN   = '\033[96m'
    RED    = '\033[91m'
    BOLD   = '\033[1m'
    DIM    = '\033[2m'

def cprint(color: str, msg: str):
    print(f"{color}{msg}{C.RESET}")


# ──────────────────────────────────────────
# 磁力链解析
# ──────────────────────────────────────────

@dataclass
class MagnetInfo:
    raw:        str
    info_hash:  str
    name:       str
    trackers:   List[str] = field(default_factory=list)


def parse_magnet(uri: str) -> MagnetInfo:
    """解析磁力链接，提取 info_hash / 名称 / tracker 列表。"""
    uri = uri.strip()
    if not uri.startswith('magnet:?'):
        raise ValueError("不是有效的磁力链接（需以 magnet:? 开头）")

    query  = uri[8:]
    params: dict[str, str] = {}
    trackers: list[str]    = []

    for part in query.split('&'):
        if '=' not in part:
            continue
        k, v = part.split('=', 1)
        v = unquote_plus(v)
        if k == 'tr':
            trackers.append(v)
        elif k not in params:
            params[k] = v

    xt = params.get('xt', '')
    # 支持 hex (40字节) 或 base32 (32字节)
    m = re.search(r'urn:btih:([A-Fa-f0-9]{40}|[A-Za-z2-7]{32})', xt, re.I)
    if not m:
        raise ValueError("磁力链接中未找到有效的 info hash")

    ih = m.group(1)
    if len(ih) == 32:          # base32 → hex
        import base64
        ih = base64.b32decode(ih.upper()).hex()

    return MagnetInfo(
        raw       = uri,
        info_hash = ih.lower(),
        name      = params.get('dn', ih),
        trackers  = trackers,
    )


def print_magnet_info(info: MagnetInfo):
    cprint(C.BOLD + C.CYAN, "\n══════════════ 磁力链解析结果 ══════════════")
    print(f"  名称      : {info.name}")
    print(f"  Info Hash : {info.info_hash}")
    print(f"  Trackers  : {len(info.trackers)} 个")
    for t in info.trackers[:5]:
        print(f"              {C.DIM}{t}{C.RESET}")
    if len(info.trackers) > 5:
        print(f"              ... 另有 {len(info.trackers)-5} 个")
    cprint(C.BOLD + C.CYAN, "════════════════════════════════════════════\n")


# ──────────────────────────────────────────
# 视频预览
# ──────────────────────────────────────────

PLAYERS = [
    ('ffplay', lambda f, d: ['ffplay', '-autoexit', '-t', str(d), '-loglevel', 'error', f]),
    ('mpv',    lambda f, d: ['mpv', f'--length={d}', '--quiet', '--no-terminal', f]),
    ('vlc',    lambda f, d: ['vlc', '--play-and-exit', f'--run-time={d}', '--intf', 'dummy', f]),
    ('mplayer',lambda f, d: ['mplayer', '-endpos', str(d), '-really-quiet', f]),
]


def find_player() -> Optional[tuple]:
    for name, cmd_fn in PLAYERS:
        if shutil.which(name):
            return name, cmd_fn
    return None


def preview_video(filepath: str, duration: int = 5):
    """用系统可用播放器预览视频前几秒，在独立线程调用。"""
    player = find_player()
    if not player:
        cprint(C.YELLOW, "未找到视频播放器 (ffplay/mpv/vlc/mplayer)，跳过预览。")
        return

    name, cmd_fn = player
    cprint(C.GREEN, f"\n▶  使用 {name} 预览 {duration}s: {os.path.basename(filepath)}")
    try:
        proc = subprocess.Popen(
            cmd_fn(filepath, duration),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=duration + 15)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception as e:
        cprint(C.YELLOW, f"预览失败: {e}")


# ──────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────

def fmt_size(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_speed(n: int) -> str:
    return fmt_size(n) + '/s'


def fmt_eta(seconds: float) -> str:
    if seconds <= 0:
        return '--'
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


STATE_LABEL = {
    lt.torrent_status.checking_files:      "检查文件",
    lt.torrent_status.downloading_metadata:"元数据下载",
    lt.torrent_status.downloading:         "下载中",
    lt.torrent_status.finished:            "完成",
    lt.torrent_status.seeding:             "做种",
    lt.torrent_status.allocating:          "分配空间",
    lt.torrent_status.checking_resume_data:"检查续传",
}


def scan_videos(path: str) -> List[str]:
    """扫描目录中的视频文件，按大小降序返回。"""
    results = []
    for ext in VIDEO_EXTS:
        results.extend(Path(path).rglob(f'*{ext}'))
    return sorted([str(p) for p in results], key=lambda x: os.path.getsize(x) if os.path.exists(x) else 0, reverse=True)


# ──────────────────────────────────────────
# 下载核心
# ──────────────────────────────────────────

class MagnetDownloader:

    def __init__(
        self,
        magnet_info: MagnetInfo,
        cfg:         Config = None,
    ):
        self.info         = magnet_info
        self.cfg          = cfg or Config()
        self.save_path    = os.path.abspath(self.cfg.save_path)
        self.do_preview   = self.cfg.preview
        self.preview_sec  = self.cfg.preview_sec
        self.connections  = self.cfg.connections
        self.upload_limit = self.cfg.upload_limit_kb * 1024
        self.dl_limit     = self.cfg.download_limit_kb * 1024

        self._session:  Optional[lt.session]         = None
        self._handle:   Optional[lt.torrent_handle]  = None
        self._stop      = threading.Event()
        self._previewed = False

    # ── 会话初始化 ──

    def _make_session(self) -> lt.session:
        sp = lt.session_params()
        settings = {
            # 连接数
            'connections_limit':       self.connections,
            # 上传/下载限速
            'upload_rate_limit':       self.upload_limit,
            'download_rate_limit':     self.dl_limit,
            # 积极的 tracker 探测
            'active_downloads':        10,
            'active_seeds':            5,
            'active_limit':            20,
            # 启用 DHT / UPnP / LSD
            'enable_dht':              True,
            'enable_lsd':              True,
            'enable_upnp':             True,
            'enable_natpmp':           True,
            # 顺序下载有助于边下边播
            'strict_end_game_mode':    True,
            # 用户代理
            'user_agent':              'libtorrent/2.0',
        }
        sp.settings = lt.default_settings()
        for k, v in settings.items():
            try:
                sp.settings[k] = v
            except Exception:
                pass

        sess = lt.session(sp)
        # 添加公共 DHT bootstrap 节点
        for host, port in [
            ("router.bittorrent.com",   6881),
            ("router.utorrent.com",     6881),
            ("dht.transmissionbt.com",  6881),
            ("dht.aelitis.com",         6881),
        ]:
            sess.add_dht_router(host, port)
        return sess

    def _add_torrent(self) -> lt.torrent_handle:
        params = lt.parse_magnet_uri(self.info.raw)
        params.save_path     = self.save_path
        params.storage_mode  = lt.storage_mode_t.storage_mode_sparse
        # 顺序下载，前部分片优先——有利于边下边播
        params.flags        |= lt.torrent_flags.sequential_download
        return self._session.add_torrent(params)

    # ── 元数据等待 ──

    def _wait_metadata(self):
        timeout = self.cfg.meta_timeout_sec
        cprint(C.YELLOW, f"正在获取种子元数据（最长等待 {timeout}s）...")
        deadline = time.time() + timeout
        spin = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏']
        i = 0
        while not self._handle.has_metadata():
            if self._stop.is_set():
                raise InterruptedError("用户中断")
            if time.time() > deadline:
                raise TimeoutError("获取种子元数据超时，请检查磁力链接或网络")
            sys.stdout.write(f"\r  {spin[i % len(spin)]} 等待中... {int(deadline - time.time())}s  ")
            sys.stdout.flush()
            i += 1
            time.sleep(0.2)
        print()

    # ── 文件信息展示 ──

    def _show_torrent_info(self):
        ti  = self._handle.get_torrent_info()
        fs  = ti.files()
        cprint(C.BOLD + C.GREEN, "\n══════════════ 种子信息 ══════════════")
        print(f"  名称   : {ti.name()}")
        print(f"  总大小 : {fmt_size(ti.total_size())}")
        print(f"  文件数 : {ti.num_files()}")
        cprint(C.BOLD + C.GREEN, "  包含视频:")
        has_video = False
        for i in range(ti.num_files()):
            fname = fs.file_path(i)
            fsize = fs.file_size(i)
            if Path(fname).suffix.lower() in VIDEO_EXTS:
                has_video = True
                print(f"    {C.CYAN}{fname}{C.RESET}  ({fmt_size(fsize)})")
        if not has_video:
            cprint(C.YELLOW, "    （未检测到常见视频文件，将下载全部内容）")
        cprint(C.BOLD + C.GREEN, "══════════════════════════════════════\n")

    # ── 预览触发 ──

    def _maybe_preview(self, total_done: int):
        if self._previewed or not self.do_preview:
            return
        if total_done < self.cfg.min_preview_mb * 1024 * 1024:
            return
        videos = scan_videos(self.save_path)
        if not videos:
            return
        # 取最大视频文件
        target = videos[0]
        if not os.path.exists(target):
            return
        self._previewed = True
        threading.Thread(
            target=preview_video,
            args=(target, self.preview_sec),
            daemon=True,
            name="preview",
        ).start()

    # ── 进度条 ──

    def _progress_bar(self, pct: float, width: int = 30) -> str:
        filled = int(width * pct / 100)
        bar    = '█' * filled + '░' * (width - filled)
        return f"[{bar}] {pct:5.1f}%"

    # ── 主下载循环 ──

    def download(self):
        os.makedirs(self.save_path, exist_ok=True)
        cprint(C.BOLD, f"下载目录: {self.save_path}")

        self._session = self._make_session()
        self._handle  = self._add_torrent()

        self._wait_metadata()
        self._show_torrent_info()

        cprint(C.YELLOW, "开始下载...")

        while True:
            if self._stop.is_set():
                cprint(C.YELLOW, "\n已停止下载。")
                break

            st      = self._handle.status()
            state   = STATE_LABEL.get(st.state, str(st.state))
            pct     = st.progress * 100
            dl_rate = st.download_rate
            ul_rate = st.upload_rate
            done    = st.total_done
            wanted  = st.total_wanted
            peers   = st.num_peers
            seeds   = st.num_seeds

            eta = fmt_eta((wanted - done) / dl_rate) if dl_rate > 0 and wanted > done else '--'

            bar  = self._progress_bar(pct)
            line = (
                f"\r{bar}  "
                f"{C.CYAN}{state}{C.RESET}  "
                f"↓{C.GREEN}{fmt_speed(dl_rate)}{C.RESET} "
                f"↑{fmt_speed(ul_rate)}  "
                f"{fmt_size(done)}/{fmt_size(wanted)}  "
                f"P:{peers} S:{seeds}  "
                f"ETA:{eta}   "
            )
            sys.stdout.write(line)
            sys.stdout.flush()

            # 触发预览
            self._maybe_preview(done)

            # 完成条件
            if st.state in (lt.torrent_status.finished, lt.torrent_status.seeding) or pct >= 100:
                print()
                cprint(C.BOLD + C.GREEN, "\n✓ 下载完成！")
                self._print_result()
                break

            time.sleep(1)

    def _print_result(self):
        videos = scan_videos(self.save_path)
        if videos:
            cprint(C.BOLD, "\n视频文件列表:")
            for v in videos:
                size = os.path.getsize(v) if os.path.exists(v) else 0
                print(f"  {C.GREEN}{v}{C.RESET}  ({fmt_size(size)})")
        else:
            print(f"\n文件保存于: {self.save_path}")

    def stop(self):
        self._stop.set()
        if self._session and self._handle:
            try:
                self._session.remove_torrent(self._handle)
            except Exception:
                pass


# ──────────────────────────────────────────
# CLI 入口
# ──────────────────────────────────────────

def main():
    # ──────────────────────────────────────────────────────────
    # 磁力链列表 —— 在此处添加要下载的磁力链接，支持多条
    # ──────────────────────────────────────────────────────────
    MAGNET_LINKS: List[str] = [
        # 示例（取消注释并替换为真实磁力链接）：
        # "magnet:?xt=urn:btih:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&dn=Movie1",
        # "magnet:?xt=urn:btih:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB&dn=Movie2",
    ]

    d = DEFAULT  # 简写，方便在 help 字符串中引用
    parser = argparse.ArgumentParser(
        prog        = 'magnet_downloader',
        description = '磁力链视频下载工具（libtorrent 多线程分片）',
        epilog      = (
            "示例:\n"
            "  python magnet_downloader.py                          # 下载代码中 MAGNET_LINKS 里的链接\n"
            '  python magnet_downloader.py "magnet:?xt=urn:btih:XXXX..."  # 临时指定单条链接\n'
            '  python magnet_downloader.py "magnet:?xt=..." -o ~/Videos\n'
            "\n默认值均来自文件顶部的 Config 配置项，可直接修改 DEFAULT 实例。\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('magnets',
                        nargs='*',
                        metavar='MAGNET',
                        help='磁力链接（可选，若不传则使用代码中的 MAGNET_LINKS）')
    parser.add_argument('-o', '--output',
                        default=d.save_path,
                        dest='save_path',
                        metavar='DIR',
                        help=f'保存目录（默认: {d.save_path}）')
    parser.add_argument('--no-preview',
                        action='store_true',
                        help='禁用视频预览')
    parser.add_argument('--preview-sec',
                        type=int, default=d.preview_sec, metavar='N',
                        help=f'预览时长（秒，默认 {d.preview_sec}）')
    parser.add_argument('--connections',
                        type=int, default=d.connections, metavar='N',
                        help=f'最大并发连接数（默认 {d.connections}）')
    parser.add_argument('--upload-limit',
                        type=int, default=d.upload_limit_kb, metavar='KB',
                        help=f'上传限速 KB/s（默认 {d.upload_limit_kb}，0=不限）')
    parser.add_argument('--download-limit',
                        type=int, default=d.download_limit_kb, metavar='KB',
                        help=f'下载限速 KB/s（默认 {d.download_limit_kb}，0=不限）')
    parser.add_argument('--meta-timeout',
                        type=int, default=d.meta_timeout_sec, metavar='SEC',
                        help=f'元数据等待超时（秒，默认 {d.meta_timeout_sec}）')
    args = parser.parse_args()

    # CLI 传入的链接追加到列表（去重）
    for uri in args.magnets:
        if uri not in MAGNET_LINKS:
            MAGNET_LINKS.append(uri)

    if not MAGNET_LINKS:
        cprint(C.RED, "错误: 没有磁力链接可下载。")
        cprint(C.YELLOW, "  方式一：在代码 main() 的 MAGNET_LINKS 列表中添加链接")
        cprint(C.YELLOW, '  方式二：python magnet_downloader.py "magnet:?xt=urn:btih:XXXX..."')
        sys.exit(1)

    # 构造本次运行的 Config（CLI 参数覆盖默认值）
    cfg = Config(
        save_path         = args.save_path,
        connections       = args.connections,
        upload_limit_kb   = args.upload_limit,
        download_limit_kb = args.download_limit,
        meta_timeout_sec  = args.meta_timeout,
        preview           = not args.no_preview,
        preview_sec       = args.preview_sec,
        min_preview_mb    = DEFAULT.min_preview_mb,
    )

    cprint(C.BOLD, f"\n共 {len(MAGNET_LINKS)} 条磁力链接待下载")

    for idx, uri in enumerate(MAGNET_LINKS, 1):
        cprint(C.BOLD + C.CYAN, f"\n[{idx}/{len(MAGNET_LINKS)}] 开始处理")

        # 解析磁力链
        try:
            info = parse_magnet(uri)
        except ValueError as e:
            cprint(C.RED, f"解析失败，跳过: {e}")
            continue

        print_magnet_info(info)

        downloader = MagnetDownloader(magnet_info=info, cfg=cfg)

        def _sigint(sig, frame):
            print()
            cprint(C.YELLOW, "收到中断信号，正在停止...")
            downloader.stop()

        signal.signal(signal.SIGINT, _sigint)

        try:
            downloader.download()
        except TimeoutError as e:
            cprint(C.RED, f"\n超时: {e}")
        except InterruptedError:
            cprint(C.YELLOW, "已取消。")
            break
        except Exception as e:
            cprint(C.RED, f"\n错误: {e}")
            raise


if __name__ == '__main__':
    main()

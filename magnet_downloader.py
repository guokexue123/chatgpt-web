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
import urllib.request
import urllib.error
import warnings as _warnings
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
    meta_timeout_sec: int   = 180    # 等待种子元数据的最长秒数（无 tracker 时需要更长时间）

    # ── 网络 ──
    listen_port:      int   = 6881   # 本地监听端口（需在路由器/防火墙开放此端口入站）

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
    warnings:   List[str] = field(default_factory=list)


# 已知的专有平台 biz 标识 → 提示文字
_BIZ_WARNINGS: dict = {
    'ktr': '迅雷专属资源（biz=ktr）：该资源主要依赖迅雷私有 P2P 网络，标准 BT 客户端只能靠 DHT 找节点，速度可能极慢甚至无法下载。建议改用迅雷客户端。',
    'xl':  '迅雷专属资源（biz=xl）：同上，建议使用迅雷客户端。',
}


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

    warns = []
    biz = params.get('biz', '').lower()
    if biz in _BIZ_WARNINGS:
        warns.append(_BIZ_WARNINGS[biz])
    if not trackers:
        warns.append('磁力链中无 tracker（tr= 参数），仅靠 DHT 寻找节点，速度较慢且可能超时。')

    return MagnetInfo(
        raw       = uri,
        info_hash = ih.lower(),
        name      = params.get('dn', ih),
        trackers  = trackers,
        warnings  = warns,
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
    for w in info.warnings:
        cprint(C.YELLOW, f"\n  ⚠  {w}")
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
# Torrent 元数据预取（HTTP 方式，跳过 DHT 等待）
# ──────────────────────────────────────────
# 原理：这些公共网站会爬取/缓存已知 info_hash 对应的 .torrent 文件。
# 如果命中缓存，可在 <1s 内拿到完整元数据，完全跳过漫长的 DHT 阶段。

_TORRENT_FETCH_SOURCES = [
    # 格式: (url_template, response类型)  {h} = 大写 hex hash
    "https://itorrents.org/torrent/{h}.torrent",
    "https://torrage.info/torrent.php?h={h}",
    "https://torcache.net/torrent/{h}.torrent",
]


def fetch_torrent_bytes(info_hash: str, timeout: int = 8) -> Optional[bytes]:
    """
    用 info_hash 从公共 torrent 缓存站下载 .torrent 文件字节。
    成功返回 bytes，失败返回 None（静默）。
    """
    h = info_hash.upper()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': '*/*',
    }
    for tpl in _TORRENT_FETCH_SOURCES:
        url = tpl.format(h=h)
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if len(data) > 200 and data[:2] == b'd8':  # bencode dict
                    return data
                if len(data) > 200 and data[0:1] == b'd':  # bencode
                    return data
        except Exception:
            continue
    return None


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

        self._session:          Optional[lt.session]         = None
        self._handle:           Optional[lt.torrent_handle]  = None
        self._stop              = threading.Event()
        self._previewed         = False
        self._meta_prefetched   = False   # HTTP 预取是否成功
        self._dht_nodes         = 0       # 后台异步更新

    # ── 会话初始化 ──

    def _make_session(self) -> lt.session:
        sp = lt.session_params()
        sp.settings = lt.default_settings()

        opts = {
            # ── 连接数 / 限速 ──
            'connections_limit':            self.connections,
            'upload_rate_limit':            self.upload_limit,
            'download_rate_limit':          self.dl_limit,
            # ── 激进的 peer 发现 ──
            'active_downloads':             20,
            'active_seeds':                 5,
            'active_limit':                 30,
            'active_tracker_limit':         20,
            'active_dht_limit':             80,
            'active_lsd_limit':             60,
            # ── 监听端口（固定端口便于路由器开放入站）──
            'listen_interfaces':            f'0.0.0.0:{self.cfg.listen_port}',
            # ── DHT / UPnP / LSD / NAT-PMP ──
            'enable_dht':                   True,
            'enable_lsd':                   True,
            'enable_upnp':                  True,
            'enable_natpmp':                True,
            # ── Peer 交换 & 连接策略 ──
            'peer_connect_timeout':         5,
            'request_timeout':              10,
            'max_allowed_in_request_queue': 2000,
            'max_out_request_queue':        500,
            'whole_pieces_threshold':       20,
            'strict_end_game_mode':         True,
            # ── Tracker 宣告间隔缩短（更快找到 peers）──
            'min_announce_interval':        30,
            'tracker_backoff':              0,
            'announce_to_all_trackers':     True,  # 同时向所有 tracker 宣告
            'announce_to_all_tiers':        True,
            # ── 用户代理 ──
            'user_agent':                   'qBittorrent/4.6.2',
        }
        for k, v in opts.items():
            try:
                sp.settings[k] = v
            except Exception:
                pass

        # DHT bootstrap 节点通过 settings 注入（libtorrent 2.0 新 API，替代废弃的 add_dht_router）
        dht_nodes = ','.join([
            "router.bittorrent.com:6881",
            "router.utorrent.com:6881",
            "dht.transmissionbt.com:6881",
            "dht.aelitis.com:6881",
            "router.bitcomet.com:6881",
            "dht.libtorrent.org:25401",
        ])
        try:
            sp.settings['dht_bootstrap_nodes'] = dht_nodes
        except Exception:
            pass

        return lt.session(sp)

    # 大量公共 tracker，注入每个种子以增加 peer 来源
    _PUBLIC_TRACKERS: List[str] = [
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://open.tracker.cl:1337/announce",
        "udp://tracker.openbittorrent.com:6969/announce",
        "udp://opentracker.i2p.rocks:6969/announce",
        "udp://tracker.internetwarriors.net:1337/announce",
        "udp://tracker.leechers-paradise.org:6969/announce",
        "udp://tracker.coppersurfer.tk:6969/announce",
        "udp://tracker.zer0day.to:1337/announce",
        "udp://tracker.pirateparty.gr:6969/announce",
        "udp://exodus.desync.com:6969/announce",
        "udp://tracker.tiny-vps.com:6969/announce",
        "udp://retracker.lanta-net.ru:2710/announce",
        "udp://open.stealth.si:80/announce",
        "udp://tracker.torrent.eu.org:451/announce",
        "udp://tracker.moeking.me:6969/announce",
        "https://tracker.gbitt.info/announce",
        "https://tracker.tamersunion.org:443/announce",
        "https://opentracker.i2p.rocks:443/announce",
    ]

    def _add_torrent(self) -> lt.torrent_handle:
        params = lt.add_torrent_params()
        params.save_path    = self.save_path
        params.storage_mode = lt.storage_mode_t.storage_mode_sparse

        # ── 优先尝试 HTTP 预取 .torrent（可跳过 DHT 等待，秒级获取元数据）──
        cprint(C.YELLOW, "尝试从公共缓存站预取种子元数据（可绕过 DHT 加速启动）...")
        torrent_bytes = fetch_torrent_bytes(self.info.info_hash)
        if torrent_bytes:
            try:
                ti = lt.torrent_info(torrent_bytes)
                params.ti = ti
                cprint(C.GREEN, f"  ✓ 预取成功！跳过 DHT 等待，直接使用缓存元数据。")
                self._meta_prefetched = True
            except Exception as e:
                cprint(C.YELLOW, f"  预取的数据解析失败（{e}），回退到 DHT 模式。")
                torrent_bytes = None

        if not torrent_bytes:
            # 回退：从磁力链解析参数
            mp = lt.parse_magnet_uri(self.info.raw)
            params.info_hashes = mp.info_hashes
            params.trackers    = mp.trackers
            params.name        = mp.name
            self._meta_prefetched = False

        # 注入公共 tracker（合并去重）
        existing = set(params.trackers)
        for t in self._PUBLIC_TRACKERS:
            if t not in existing:
                params.trackers.append(t)

        # 随机分片：peer 少时比顺序分片更容易凑够数据
        return self._session.add_torrent(params)

    # ── 元数据等待 ──

    def _wait_metadata(self):
        if self._meta_prefetched:
            # HTTP 预取已拿到元数据，无需 DHT 等待
            return

        timeout = self.cfg.meta_timeout_sec
        cprint(C.YELLOW, f"正在通过 DHT 获取种子元数据（最长等待 {timeout}s）...")
        deadline = time.time() + timeout
        spin = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏']
        i = 0
        while not self._handle.status().has_metadata:
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
        # torrent_file() 替代废弃的 get_torrent_info()
        ti = self._handle.torrent_file()
        if ti is None:
            cprint(C.YELLOW, "元数据尚未就绪，跳过文件列表展示。")
            return
        fs = ti.files()
        cprint(C.BOLD + C.GREEN, "\n══════════════ 种子信息 ══════════════")
        print(f"  名称   : {ti.name()}")
        print(f"  总大小 : {fmt_size(ti.total_size())}")
        print(f"  文件数 : {ti.num_files()}")
        cprint(C.BOLD + C.GREEN, "  包含文件（视频）:")
        has_video = False
        for i in range(ti.num_files()):
            fname = fs.file_path(i)
            fsize = fs.file_size(i)
            if Path(fname).suffix.lower() in VIDEO_EXTS:
                has_video = True
                print(f"    {C.CYAN}{fname}{C.RESET}  ({fmt_size(fsize)})")
        if not has_video:
            cprint(C.YELLOW, "    （未检测到常见视频文件，将下载全部内容）")

        # 磁力链不包含 HTTP 视频 URL，视频数据分散在 P2P 节点中
        # 但下载完成后本地路径即为视频位置
        cprint(C.DIM + C.BOLD + C.GREEN, "\n  ℹ  磁力链不含 HTTP URL，视频以分片形式存储于 P2P 网络")
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

    def _dht_stats_updater(self):
        """后台线程：每 5s 通过 post_dht_stats() 更新 DHT 节点数（替代废弃的 session.status()）。"""
        while not self._stop.is_set():
            try:
                self._session.post_dht_stats()
                time.sleep(0.2)
                for a in self._session.pop_alerts():
                    if hasattr(a, 'routing_table'):
                        self._dht_nodes = sum(b.num_nodes for b in a.routing_table)
                        break
            except Exception:
                pass
            self._stop.wait(5)

    def download(self):
        os.makedirs(self.save_path, exist_ok=True)
        cprint(C.BOLD, f"下载目录: {self.save_path}")

        self._session = self._make_session()
        self._handle  = self._add_torrent()

        # 后台更新 DHT 节点数
        threading.Thread(target=self._dht_stats_updater, daemon=True, name="dht-stats").start()

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

            # DHT 节点数 — session.status() 在 2.0 中废弃，改用 post_dht_stats() + alert
            dht_nodes = self._dht_nodes

            bar  = self._progress_bar(pct)
            line = (
                f"\r{bar}  "
                f"{C.CYAN}{state}{C.RESET}  "
                f"↓{C.GREEN}{fmt_speed(dl_rate)}{C.RESET} "
                f"↑{fmt_speed(ul_rate)}  "
                f"{fmt_size(done)}/{fmt_size(wanted)}  "
                f"P:{peers} S:{seeds} DHT:{dht_nodes}  "
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

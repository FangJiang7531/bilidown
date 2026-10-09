#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BiliDown v0.2 —— 共享核心模块

职责：
  * 路径与工具链定位（ffmpeg / ffprobe / cookies / 配置文件）
  * 视频信息探测：标题、UP 主、时长、分P、**真实可用的最高清晰度**
  * 下载：基于 yt-dlp Python API，带进度 / 取消 / 日志回调
  * 附加内容：弹幕（XML）、封面、字幕

GUI (`bili_down_gui.pyw`) 与 CLI (`命令/bili_cli.py`) 共用本模块，
因此画质选择逻辑在两条链路上完全一致。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

APP_VERSION = "0.2.0"
APP_NAME = "BiliDown"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REFERER = "https://www.bilibili.com/"

# 无 ffmpeg 时也能工作，但合并 / 转码会失败
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ══════════════════════════════════════════════════════════════════════
# 路径
# ══════════════════════════════════════════════════════════════════════
def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """可写的主目录：exe 所在目录（打包后）或源码目录（开发时）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """只读资源目录：打包后是 _internal，开发时同 app_dir。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return app_dir()


def tools_dir() -> Path:
    return app_dir() / "tools"


def downloads_dir() -> Path:
    return app_dir() / "downloads"


def _first_existing(*paths: Optional[Path]) -> Optional[Path]:
    for p in paths:
        try:
            if p and Path(p).exists():
                return Path(p)
        except OSError:
            continue
    return None


def find_ffmpeg() -> Optional[str]:
    """ffmpeg 定位：exe 同级 tools → 打包资源 → 源码 tools → 系统 PATH。"""
    cand = _first_existing(
        tools_dir() / "ffmpeg.exe",
        tools_dir() / "ffmpeg",
        resource_dir() / "tools" / "ffmpeg.exe",
    )
    if cand:
        return str(cand)
    return shutil.which("ffmpeg")


def find_ffprobe() -> Optional[str]:
    cand = _first_existing(
        tools_dir() / "ffprobe.exe",
        tools_dir() / "ffprobe",
        resource_dir() / "tools" / "ffprobe.exe",
    )
    if cand:
        return str(cand)
    return shutil.which("ffprobe")


def find_ffmpeg_dir() -> Optional[str]:
    """返回给 yt-dlp 的 --ffmpeg-location：包含 ffmpeg 与 ffprobe 的目录。"""
    ff = find_ffmpeg()
    if not ff:
        return None
    d = Path(ff).parent
    # 目录里最好同时有 ffprobe，否则 yt-dlp 会退回到 PATH 查找
    if (d / "ffprobe.exe").exists() or (d / "ffprobe").exists():
        return str(d)
    return str(d)


def find_cookies() -> Optional[Path]:
    """在 app 目录中寻找 bilibili cookies 文件。"""
    base = app_dir()
    try:
        names = sorted(p.name for p in base.iterdir() if p.is_file())
    except OSError:
        return None
    def _ok(name: str) -> bool:
        # 转换出来的中间文件不能再被当成用户提供的 cookies
        return not name.lower().endswith(".netscape.txt")

    # 精确优先
    for name in names:
        low = name.lower()
        if _ok(name) and "bilibili" in low and low.endswith(".txt") and "cookie" in low:
            return base / name
    for name in names:
        low = name.lower()
        if _ok(name) and low.endswith("_cookies.txt"):
            return base / name
    # 用户按教程存成 cookies.txt 也能直接识别
    for name in names:
        low = name.lower()
        if _ok(name) and low in ("cookies.txt", "cookie.txt"):
            return base / name
    return None


def ytdlp_version() -> str:
    try:
        import yt_dlp
        return getattr(yt_dlp.version, "__version__", "?")
    except Exception:
        return ""


def has_builtin_ytdlp() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════
DEFAULT_CONFIG: Dict[str, Any] = {
    "outdir": "",
    "quality": "max",              # max / 具体 tier key / audio-m4a / audio-mp3
    "container": "mp4",
    "write_danmaku": True,
    "write_thumbnail": False,
    "write_subs": False,
    "audio_only": False,
    "playlist_all": True,
    "filename_template": "%(title).120B.%(ext)s",
    "cookies": "",
    "proxy": "",
    "open_when_done": False,
    "window": "",
    "concurrent_fragments": 4,
}


def config_path() -> Path:
    return app_dir() / "config.json"


def _config_fallback_path() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / APP_NAME / "config.json"


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    for p in (config_path(), _config_fallback_path()):
        try:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
                break
        except Exception:
            continue
    if not cfg.get("outdir"):
        cfg["outdir"] = str(downloads_dir())
    if not cfg.get("cookies"):
        ck = find_cookies()
        if ck:
            cfg["cookies"] = str(ck)
    return cfg


def save_config(cfg: Dict[str, Any]) -> Optional[str]:
    data = {k: cfg.get(k, v) for k, v in DEFAULT_CONFIG.items()}
    for p in (config_path(), _config_fallback_path()):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, p)
            return str(p)
        except Exception:
            continue
    return None


# ══════════════════════════════════════════════════════════════════════
# URL 规整
# ══════════════════════════════════════════════════════════════════════
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{8,12})")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)
_B23_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+")
_PAGE_RE = re.compile(r"[?&]p=(\d+)")


def normalize_url(url: str) -> str:
    """把各种粘贴形式统一成 yt-dlp 可用的 URL。"""
    url = (url or "").strip().strip("<>\"'")
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        m = _BV_RE.search(url) or _AV_RE.search(url)
        if m:
            return f"https://www.bilibili.com/video/{m.group(0)}"
    return url


def is_bilibili_url(url: str) -> bool:
    low = (url or "").lower()
    return "bilibili.com" in low or "b23.tv" in low or bool(_BV_RE.search(url or ""))


def extract_page(url: str) -> Optional[int]:
    m = _PAGE_RE.search(url or "")
    return int(m.group(1)) if m else None


# ══════════════════════════════════════════════════════════════════════
# 画质模型
# ══════════════════════════════════════════════════════════════════════
# B 站清晰度代号（qn）→ 友好名
QN_NAMES = {
    127: "8K 超清",
    126: "杜比视界",
    125: "HDR 真彩",
    120: "4K 超清",
    116: "1080P60 高帧率",
    112: "1080P+ 高码率",
    100: "智能修复",
    80: "1080P 高清",
    74: "720P60 高帧率",
    64: "720P 高清",
    32: "480P 清晰",
    16: "360P 流畅",
    6: "240P 极速",
}

_HEIGHT_NAME = {
    4320: "8K 超清",
    2160: "4K 超清",
    1440: "2K 超清",
    1080: "1080P 高清",
    720: "720P 高清",
    480: "480P 清晰",
    360: "360P 流畅",
    240: "240P 极速",
}


@dataclass
class QualityTier:
    """一个可下载的清晰度档位（同一高度只保留最好的那一档）。"""
    key: str                # 唯一标识，用于在 UI 中回传
    height: int
    fps: int
    label: str              # "2160p60 · 4K 超清"
    short: str              # "4K"
    selector: str           # 传给 yt-dlp -f 的选择器
    vcodec: str = ""
    tbr: float = 0.0
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Part:
    index: int
    title: str
    url: str
    cid: str = ""
    duration: float = 0.0


@dataclass
class VideoInfo:
    ok: bool = False
    error: str = ""
    url: str = ""
    title: str = ""
    uploader: str = ""
    duration: float = 0.0
    thumbnail: str = ""
    is_playlist: bool = False
    parts: List[Part] = field(default_factory=list)
    tiers: List[QualityTier] = field(default_factory=list)
    audio_tiers: List[QualityTier] = field(default_factory=list)
    logged_in: bool = False
    has_vip: bool = False
    max_label: str = ""
    engine: str = ""
    cookies_ignored: bool = False

    def summary(self) -> str:
        bits = []
        if self.uploader:
            bits.append(self.uploader)
        if self.duration:
            bits.append(fmt_duration(self.duration))
        if self.is_playlist and len(self.parts) > 1:
            bits.append(f"{len(self.parts)} 个分P")
        if self.max_label:
            bits.append(f"最高可用 {self.max_label}")
        return " · ".join(bits)


def fmt_duration(sec: float) -> str:
    try:
        sec = int(round(float(sec)))
    except Exception:
        return ""
    if sec <= 0:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _short_name(height: int) -> str:
    return {4320: "8K", 2160: "4K", 1440: "2K", 1080: "1080P",
            720: "720P", 480: "480P", 360: "360P", 240: "240P"}.get(height, f"{height}P")


def _tier_label(height: int, fps: int, note: str, qn: Any) -> str:
    base = f"{height}p"
    if fps and fps >= 48:
        base += f"{int(fps)}"
    friendly = ""
    if isinstance(qn, int) and qn in QN_NAMES:
        friendly = QN_NAMES[qn]
    if not friendly:
        friendly = _HEIGHT_NAME.get(height, "")
    note = (note or "").strip()
    if note and note not in (friendly, base):
        # B 站 format_note 形如 "1080P 高清"，优先使用
        if any(ch.isalpha() for ch in note) or "P" in note.upper():
            friendly = note
    if friendly and friendly not in base:
        return f"{base} · {friendly}"
    return base


def _selector_for(height: int, fps: int) -> str:
    """为档位生成精确的 -f 选择器。

    先精确匹配高度+帧率，再退化为「不超过该高度」，最后兜底 best。
    """
    primary = f"bestvideo[height={height}]"
    if fps:
        primary += f"[fps={fps}]"
    primary += "+bestaudio/best"
    ladder = [
        primary,
        f"bestvideo[height={height}]+bestaudio/best",
        f"bestvideo[height<={height}]+bestaudio/best",
        "best",
    ]
    return "/".join(ladder)


def build_tiers(info: Dict[str, Any]) -> tuple[List[QualityTier], List[QualityTier]]:
    """从 yt-dlp info 字典中提取真实可用的清晰度档位。

    返回 (视频档位, 音频档位)，均按从高到低排序。
    关键点：只保留「确实存在」的档位，因此 UI 里的“最高清晰度”
    永远等于这部视频此刻真实能拿到的最高清晰度。
    """
    formats = info.get("formats") or []
    by_height: Dict[int, Dict[str, Any]] = {}

    for f in formats:
        try:
            vcodec = f.get("vcodec") or "none"
            acodec = f.get("acodec") or "none"
            height = int(f.get("height") or 0)
        except Exception:
            continue
        if vcodec == "none" or height <= 0:
            continue
        # 同一个高度保留「帧率高 → 码率高 → 体积大」的那一条
        fps = int(round(float(f.get("fps") or 0)))
        score = (
            fps,
            float(f.get("tbr") or 0),
            float(f.get("filesize") or f.get("filesize_approx") or 0),
        )
        cur = by_height.get(height)
        if cur is None or score > cur["_score"]:
            by_height[height] = {"fmt": f, "fps": fps, "_score": score}

    video_tiers: List[QualityTier] = []
    for height in sorted(by_height, reverse=True):
        entry = by_height[height]
        f = entry["fmt"]
        fps = entry["fps"]
        note = f.get("format_note") or ""
        qn = f.get("quality")
        label = _tier_label(height, fps, note, qn)
        video_tiers.append(QualityTier(
            key=f"v{height}p{fps}" if fps and fps >= 48 else f"v{height}p",
            height=height,
            fps=fps,
            label=label,
            short=_short_name(height),
            selector=_selector_for(height, fps),
            vcodec=f.get("vcodec") or "",
            tbr=float(f.get("tbr") or 0),
            note=note,
        ))

    # ── 音频 ──────────────────────────────────────────────────────────
    # 按「编码家族」去重，每族只保留码率最高的一条，避免下拉框出现
    # 三条一模一样的 “仅音频 · mp4a”。
    audio_seen: Dict[str, Dict[str, Any]] = {}
    for f in formats:
        try:
            vcodec = f.get("vcodec") or "none"
            acodec = (f.get("acodec") or "none").lower()
        except Exception:
            continue
        if acodec == "none" or vcodec != "none":
            continue
        abr = float(f.get("abr") or f.get("tbr") or 0)
        family = ("flac" if "flac" in acodec else
                  "eac3" if ("ec-3" in acodec or "eac3" in acodec) else
                  "aac" if "mp4a" in acodec or "aac" in acodec else acodec)
        cur = audio_seen.get(family)
        if cur is None or abr > float(cur["_abr"]):
            audio_seen[family] = {"fmt": f, "_abr": abr, "_codec": acodec}

    audio_tiers: List[QualityTier] = []
    for family, entry in sorted(audio_seen.items(),
                                key=lambda kv: kv[1]["_abr"], reverse=True):
        f = entry["fmt"]
        abr = float(entry["_abr"])
        codec = entry["_codec"]
        pretty = {"flac": "FLAC 无损",
                  "eac3": "杜比全景声",
                  "aac": "AAC"}.get(family, codec)
        if abr:
            pretty = f"{pretty} {int(abr)} kbps"
        audio_tiers.append(QualityTier(
            key=f"a-{family}-{int(abr)}",
            height=0,
            fps=0,
            label=f"仅音频 · {pretty}",
            short="音频",
            selector=f"{f.get('format_id')}/bestaudio/best",
            vcodec=codec,
            tbr=abr,
        ))
    return video_tiers, audio_tiers


# 静态兜底档位：无网络 / 未探测时使用。
# 注意 “max” 档位用 -S 强制按 分辨率→帧率→码率 排序，确保拿到真实最高。
_SAFE_SORT = "res,fps,hdr:12,br,size"


def fallback_tiers() -> List[QualityTier]:
    tiers = [QualityTier(key="max", height=0, fps=0,
                         label="最高清晰度", short="MAX",
                         selector="bestvideo+bestaudio/best")]
    for height, name in ((2160, "4K 超清"), (1080, "1080P 高清"),
                         (720, "720P 高清"), (480, "480P 清晰"), (360, "360P 流畅")):
        tiers.append(QualityTier(
            key=f"v{height}p", height=height, fps=0,
            label=f"{'≤' if False else ''}{height}p · {name}",
            short=_short_name(height),
            selector=_selector_for(height, 0),
        ))
    return tiers


# ══════════════════════════════════════════════════════════════════════
# yt-dlp 选项构造
# ══════════════════════════════════════════════════════════════════════
class _Logger:
    """把 yt-dlp 的消息转成回调。"""

    def __init__(self, sink: Callable[[str, str], None]):
        self._sink = sink

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug] "):
            return
        if "[download] Destination" in msg or "[Merger]" in msg:
            self._sink(msg, "dim")

    def info(self, msg: str) -> None:
        self.debug(msg)

    def warning(self, msg: str) -> None:
        self._sink(str(msg), "warn")

    def error(self, msg: str) -> None:
        self._sink(str(msg), "err")


def base_ydl_opts(
    *,
    cookies: Optional[str] = None,
    proxy: str = "",
    on_log: Optional[Callable[[str, str], None]] = None,
    ffmpeg_dir: Optional[str] = None,
    concurrent_fragments: int = 4,
) -> Dict[str, Any]:
    sink = on_log or (lambda m, t=None: None)

    # yt-dlp 的 progress_hook 自带 print；我们用 quiet + noprogress 关掉，
    # 进度完全由 GUI 自己的回调渲染，这样才不会抖。
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "consoletitle": False,
        "noplaylist": False,
        "logger": _Logger(sink),
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "retry_sleep_functions": {"http": lambda n: min(2 ** n, 20)},
        "file_access_retries": 3,
        "concurrent_fragment_downloads": max(1, int(concurrent_fragments or 1)),
        "socket_timeout": 30,
        "nocheckcertificate": True,
        "continuedl": True,
        "overwrites": False,
        "windowsfilenames": True,
        # 注意：不要设置 trim_file_name。yt-dlp 是拿【完整路径】去比这个长度
        # 的，输出目录一长就会把文件名从中间截断
        # （实测 ...\cli_final_57cd.../Rick Astley.mp4 → Rick Astl.mp4）。
        # 文件名长度已经由模板里的 %(title).120B 控制，这里保持默认 0。
        "trim_file_name": 0,
        "ignoreerrors": False,
        "http_headers": {"User-Agent": USER_AGENT, "Referer": REFERER},
        "format_sort": _SAFE_SORT.split(","),
    }
    # 手动从 F12 复制的 Cookie 头不是 Netscape 格式，yt-dlp 读不了，
    # 这里换成自动转换出来的中间文件
    cookiefile = ensure_netscape_cookiefile(cookies)
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if proxy.strip():
        opts["proxy"] = proxy.strip()
    if ffmpeg_dir:
        opts["ffmpeg_location"] = ffmpeg_dir
    return opts


# ══════════════════════════════════════════════════════════════════════
# 探测
# ══════════════════════════════════════════════════════════════════════
class ProbeError(RuntimeError):
    pass


def _import_ytdlp():
    try:
        import yt_dlp
        return yt_dlp
    except Exception as exc:  # pragma: no cover
        raise ProbeError(f"yt-dlp 模块不可用：{exc}") from exc


def probe(
    url: str,
    *,
    cookies: Optional[str] = None,
    proxy: str = "",
    on_log: Optional[Callable[[str, str], None]] = None,
    ffmpeg_dir: Optional[str] = None,
    deep: bool = True,
) -> VideoInfo:
    """探测视频信息与真实可用清晰度。

    deep=True 时会额外对第一个分P 做完整解析以获取 formats 列表
    （B 站合集的第一层只给出标题，没有清晰度信息）。
    """
    yt_dlp = _import_ytdlp()
    url = normalize_url(url)
    info = VideoInfo(url=url)

    # 失效的 cookies 会把画质从 1080P 压到 480P，宁可匿名
    use_cookies = effective_cookies(cookies, proxy)
    info.cookies_ignored = bool(cookies) and not use_cookies

    flat_opts = base_ydl_opts(cookies=use_cookies, proxy=proxy, on_log=on_log,
                              ffmpeg_dir=ffmpeg_dir)
    flat_opts.update({
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": 200,
    })

    try:
        with yt_dlp.YoutubeDL(flat_opts) as ydl:
            data = ydl.extract_info(url, download=False)
    except Exception as exc:
        msg = _friendly_error(exc)
        info.error = msg
        return info

    if not data:
        info.error = "未能解析该链接"
        return info

    entries = data.get("entries")
    first: Optional[Dict[str, Any]] = None
    if entries:
        info.is_playlist = True
        idx = 0
        for e in entries:
            if not e:
                continue
            idx += 1
            info.parts.append(Part(
                index=idx,
                title=e.get("title") or f"P{idx}",
                url=e.get("url") or e.get("webpage_url") or url,
                cid=str(e.get("cid") or ""),
                duration=float(e.get("duration") or 0),
            ))
        if info.parts:
            first = entries[0] or None
            info.title = data.get("title") or info.parts[0].title
            info.uploader = data.get("uploader") or data.get("channel") or ""
            info.thumbnail = data.get("thumbnail") or ""
        if not info.parts:
            info.error = "该合集为空"
            return info
    else:
        first = data
        info.title = data.get("title") or ""
        info.uploader = data.get("uploader") or data.get("channel") or ""
        info.duration = float(data.get("duration") or 0)
        info.thumbnail = data.get("thumbnail") or ""
        info.parts.append(Part(index=1, title=info.title or "P1", url=url,
                               cid=str(data.get("cid") or ""),
                               duration=info.duration))

    detail = first if not deep else _deep_entry(first, url, yt_dlp, flat_opts)
    if detail is None:
        detail = first or {}

    tiers, audio = build_tiers(detail)
    info.tiers = tiers
    info.audio_tiers = audio
    if not info.duration:
        info.duration = float(detail.get("duration") or 0)
    if not info.thumbnail:
        info.thumbnail = detail.get("thumbnail") or ""
    if not info.title:
        info.title = detail.get("title") or ""
    if not info.uploader:
        info.uploader = detail.get("uploader") or detail.get("channel") or ""

    # 这里只做「推断」：B 站未登录最高 480P，所以能解析到 ≥720P 就说明
    # Cookies 生效了。真实登录态由 check_login() 独立确认，不要在这里断言。
    notes = " ".join((t.note or "") for t in tiers)
    heights = [t.height for t in tiers]
    info.logged_in = any(h >= 720 for h in heights)
    info.has_vip = any(h >= 2160 for h in heights) or "8K" in notes or "4K" in notes
    info.max_label = tiers[0].label if tiers else "未知"
    info.engine = _engine_name()
    info.ok = bool(tiers)
    if not tiers:
        info.error = "未能获取清晰度列表（可能是付费/地区限制内容）"
    return info


def _deep_entry(first: Optional[Dict[str, Any]], url: str, yt_dlp,
                flat_opts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """对单个分P 做完整解析，拿到 formats。"""
    if not first:
        return None
    if first.get("formats"):
        return first
    target = first.get("webpage_url") or first.get("url") or url
    if not target:
        return None
    opts = dict(flat_opts)
    opts.pop("extract_flat", None)
    opts["extract_flat"] = False
    opts["playlist_items"] = "1"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(target, download=False)
    except Exception:
        return first
    entries = (data or {}).get("entries")
    if entries:
        for e in entries:
            if e:
                return e
    return data or first


def _engine_name() -> str:
    """下载引擎说明。

    程序始终使用内置的 yt-dlp 模块（in-process），这样才能拿到真正的
    progress hook 和即时取消；不依赖机器上是否装了 yt-dlp。
    """
    ver = ytdlp_version()
    return f"内置 yt-dlp {ver}" if ver else "未找到 yt-dlp"


# ══════════════════════════════════════════════════════════════════════
# 下载
# ══════════════════════════════════════════════════════════════════════
try:  # yt-dlp 自带取消异常，用于在 progress hook 里中断
    from yt_dlp.utils import DownloadCancelled as _DownloadCancelled  # type: ignore
except Exception:  # pragma: no cover
    class _DownloadCancelled(Exception):
        pass


class CancelToken:
    def __init__(self) -> None:
        self._ev = threading.Event()

    def cancel(self) -> None:
        self._ev.set()

    def reset(self) -> None:
        self._ev.clear()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()


@dataclass
class DownloadPlan:
    outdir: str
    selector: str = "bestvideo+bestaudio/best"
    container: str = "mp4"
    quality_label: str = ""
    filename_template: str = "%(title).120B.%(ext)s"
    cookies: str = ""
    proxy: str = ""
    write_danmaku: bool = False
    write_thumbnail: bool = False
    write_subs: bool = False
    audio_only: bool = False
    audio_format: str = "m4a"          # m4a / mp3
    playlist_all: bool = True
    playlist_items: str = ""           # "1,3,5" 或 "1-5"


@dataclass
class TaskProgress:
    percent: float = 0.0
    speed: float = 0.0
    eta: float = 0.0
    downloaded: int = 0
    total: int = 0
    stage: str = ""                    # 下载中 / 合并中 / 转码中
    index: int = 0
    count: int = 1
    title: str = ""


class DownloadResult:
    def __init__(self) -> None:
        self.ok = False
        self.cancelled = False
        self.error = ""
        self.files: List[str] = []
        self.titles: List[str] = []


class _Aggregator:
    """把 yt-dlp 每个流各自的进度合成一个平滑的总进度。"""

    def __init__(self) -> None:
        self._streams: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()
        self._pct = 0.0

    def update(self, d: Dict[str, Any]) -> Dict[str, float]:
        key = str(d.get("format_id") or d.get("filename") or "?")
        total = float(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
        done = float(d.get("downloaded_bytes") or 0)
        with self._lock:
            if d.get("status") == "finished":
                self._streams[key] = {"done": total or done, "total": total or done}
            else:
                prev = self._streams.get(key, {})
                # 总量估算会变化，取较大的以保证单流进度不回退
                t = max(total, prev.get("total", 0))
                self._streams[key] = {"done": done, "total": t}
            done_sum = sum(s["done"] for s in self._streams.values())
            total_sum = sum(s["total"] for s in self._streams.values())
            pct = (done_sum / total_sum * 100.0) if total_sum > 0 else 0.0
            # 单调递增：B 站是「先下视频流、再下音频流」，音频流出现时
            # 分母突然变大，原始比例会让进度条从 100% 掉回 60% 多。
            # 这里取历史最大值并封顶 99%，剩下的 1% 留给合并/完成事件。
            self._pct = max(self._pct, min(pct, 99.0))
            shown = self._pct
        return {"percent": shown, "downloaded": done_sum, "total": total_sum}


def download(
    url: str,
    plan: DownloadPlan,
    *,
    on_log: Optional[Callable[[str, str], None]] = None,
    on_progress: Optional[Callable[[TaskProgress], None]] = None,
    cancel: Optional[CancelToken] = None,
    index: int = 0,
    count: int = 1,
) -> DownloadResult:
    """执行一次下载（可含播放列表）。所有回调都在同一线程内触发。"""
    result = DownloadResult()
    log = on_log or (lambda m, t=None: None)
    prog = on_progress or (lambda p: None)
    cancel = cancel or CancelToken()

    url = normalize_url(url)
    outdir = Path(plan.outdir or downloads_dir())
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.error = f"无法创建保存目录：{exc}"
        return result

    agg = _Aggregator()
    state = {"stage": "解析中", "title": ""}

    def _hook(d: Dict[str, Any]) -> None:
        if cancel.cancelled:
            raise _DownloadCancelled("用户取消")
        info = d.get("info_dict") or {}
        title = info.get("title") or ""
        if title and title != state["title"]:
            state["title"] = title
        if d.get("status") == "downloading":
            stats = agg.update(d)
            p = TaskProgress(
                percent=stats["percent"],
                speed=float(d.get("speed") or 0),
                eta=float(d.get("eta") or 0),
                downloaded=int(stats["downloaded"]),
                total=int(stats["total"]),
                stage="下载中",
                index=index, count=count, title=state["title"],
            )
            prog(p)
        elif d.get("status") == "finished":
            agg.update(d)
            state["stage"] = "合并中"
            prog(TaskProgress(percent=99.0, stage="合并中",
                              index=index, count=count, title=state["title"]))

    def _pp_hook(d: Dict[str, Any]) -> None:
        if cancel.cancelled:
            raise _DownloadCancelled("用户取消")
        name = d.get("postprocessor") or ""
        status = d.get("status")
        if status == "started":
            label = {"Merger": "合并音视频", "FFmpegMerger": "合并音视频",
                     "FFmpegExtractAudio": "提取音频",
                     "FFmpegVideoConvertor": "转码",
                     "FFmpegMetadata": "写入元数据"}.get(name, name or "后处理")
            state["stage"] = label
            log(f"[{label}] …", "dim")
            prog(TaskProgress(percent=99.0, stage=label,
                              index=index, count=count, title=state["title"]))

    # ── 组装 yt-dlp 选项 ──────────────────────────────────────────────
    # 同样先验证登录态：失效的 cookies 会让 B 站把画质压到 480P
    use_cookies = effective_cookies(plan.cookies, plan.proxy)
    if plan.cookies and not use_cookies:
        log("Cookies 已失效，本次不带 Cookies 请求（不带反而画质更高）", "warn")

    yt_dlp = _import_ytdlp()
    opts = base_ydl_opts(cookies=use_cookies, proxy=plan.proxy, on_log=log,
                         ffmpeg_dir=find_ffmpeg_dir())
    opts.update({
        "outtmpl": {"default": str(outdir / plan.filename_template)},
        "progress_hooks": [_hook],
        "postprocessor_hooks": [_pp_hook],
    })
    if plan.container:
        opts["merge_output_format"] = plan.container
    if plan.write_thumbnail:
        opts["writethumbnail"] = True
    if plan.write_subs:
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = ["all"]
        opts["subtitlesformat"] = "srt/best"
    if plan.playlist_items:
        opts["playlist_items"] = plan.playlist_items
    elif not plan.playlist_all:
        opts["noplaylist"] = True

    if plan.audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": plan.audio_format or "m4a",
            "preferredquality": "0",
        }]
        opts.pop("merge_output_format", None)
    else:
        opts["format"] = plan.selector or "bestvideo+bestaudio/best"

    if plan.proxy.strip():
        opts["proxy"] = plan.proxy.strip()

    log(f"画质：{plan.quality_label or plan.selector}", "accent")
    log(f"保存到：{outdir}", "dim")

    started = time.time()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=True)
    except _DownloadCancelled:
        result.cancelled = True
        result.error = "已取消"
        log("已停止下载", "warn")
        return result
    except Exception as exc:
        if cancel.cancelled:
            result.cancelled = True
            result.error = "已取消"
            log("已停止下载", "warn")
        else:
            result.error = _friendly_error(exc)
            log(result.error, "err")
        return result

    result.ok = True

    # 弹幕需要 cid，而 yt-dlp 不提供，只能另行查询分P 接口
    pages: List[Dict[str, Any]] = []
    if plan.write_danmaku:
        bvid = extract_bvid(url) or (data.get("id") if isinstance(data, dict) else "") or ""
        # 这里用原始 cookies：cid 接口带 cookies 才稳定，和播放地址的策略不同
        pages = fetch_video_pages(str(bvid), plan.cookies, plan.proxy, on_log=log)

    for idx, entry in enumerate(_iter_entries(data)):
        entry_files: List[str] = []
        for rd in (entry.get("requested_downloads") or []):
            fp = rd.get("filepath")
            if fp and Path(fp).exists():
                entry_files.append(fp)
                result.files.append(fp)
        t = entry.get("title")
        if t:
            result.titles.append(t)

        if plan.write_danmaku and idx < len(pages):
            cid = str(pages[idx].get("cid") or "")
            if not cid:
                continue
            if entry_files:
                # 与视频文件同名同目录，方便播放器自动关联
                target = Path(entry_files[0]).with_suffix(".danmaku.xml")
            else:
                target = _danmaku_target(entry, outdir, plan.filename_template)
            got = fetch_danmaku(cid, target, plan.cookies, plan.proxy, log)
            if got:
                result.files.append(str(got))

    # 没有 requested_downloads 时按修改时间兜底找文件
    if not result.files:
        result.files = _recent_files(outdir, started)

    prog(TaskProgress(percent=100.0, stage="完成", index=index, count=count,
                      title=state["title"]))
    return result


def _iter_entries(data: Any) -> Iterable[Dict[str, Any]]:
    if not data:
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    if entries:
        return [e for e in entries if e]
    return [data]


def _recent_files(outdir: Path, since: float, window: float = 7200) -> List[str]:
    out: List[str] = []
    try:
        for p in outdir.iterdir():
            if not p.is_file():
                continue
            try:
                if p.stat().st_mtime >= since - 5:
                    out.append(str(p))
            except OSError:
                continue
    except OSError:
        pass
    return out


def _danmaku_target(entry: Dict[str, Any], outdir: Path,
                    template: str) -> Path:
    """弹幕文件名与视频保持一致。"""
    title = _sanitize(entry.get("title") or "danmaku")
    return outdir / f"{title[:120]}.danmaku.xml"


def _sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    return name.strip(" .") or "video"


# ══════════════════════════════════════════════════════════════════════
# 弹幕
# ══════════════════════════════════════════════════════════════════════
def _split_cookie_pairs(s: str) -> Dict[str, str]:
    """把 "a=1; b=2; c=3" 拆成字典。"""
    out: Dict[str, str] = {}
    for item in (s or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out[k] = v
    return out


def _extract_raw_cookie(text: str) -> str:
    """从手动复制的内容里抠出 Cookie 值。

    兼容三种粘贴方式：
      1. 只复制了值          ``SESSDATA=xxx; bili_jct=yyy``
      2. 连前缀一起复制      ``cookie: SESSDATA=xxx; ...``
      3. 整段 Copy as cURL   ``curl '...' -H 'cookie: SESSDATA=xxx; ...'``
    """
    for line in (text or "").splitlines():
        m = re.search(r"cookie\s*:\s*(.+)$", line, re.IGNORECASE)
        if not m:
            continue
        val = m.group(1).strip()
        # curl 里 Cookie 值被引号包着，截到引号为止
        for q in ("'", '"'):
            if q in val:
                val = val.split(q)[0]
        return val.strip()
    return " ".join((text or "").split())


def parse_cookie_text(text: str) -> Dict[str, str]:
    """解析 cookie 文本，同时兼容 Netscape 文件与手动复制的 Cookie 头。

    手动从 F12 复制出来的是 ``a=b; c=d`` 这种 Cookie 头，不是 Netscape
    格式；但 DevTools 里看到的是浏览器**真实发出**的请求头，HttpOnly 的
    SESSDATA 也在里面，所以这条路完全可行，程序负责把它转成 Netscape。
    """
    netscape_lines = 0
    netscape: Dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and "." in parts[0]:
            netscape_lines += 1
            if "bilibili" in parts[0].lower():
                netscape[parts[5].strip()] = parts[6].strip()
    if netscape_lines:
        return netscape
    return _split_cookie_pairs(_extract_raw_cookie(text))


def cookie_file_format(cookies_file: Optional[str]) -> str:
    """判断 cookies 文件是哪种格式：netscape / raw / unknown / none。"""
    if not cookies_file:
        return "none"
    p = Path(cookies_file)
    if not p.exists():
        return "none"
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "unknown"
    for line in text.splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 7 and "." in parts[0]:
            return "netscape"
    return "raw" if _split_cookie_pairs(_extract_raw_cookie(text)) else "unknown"


_NETSCAPE_HEADER = (
    "# Netscape HTTP Cookie File\n"
    "# 本文件由 BiliDown 从手动复制的 Cookie 头自动转换生成\n"
    "# 只是给 yt-dlp 读取用的中间文件，原文件未被修改\n\n"
)


def ensure_netscape_cookiefile(cookies_file: Optional[str]) -> Optional[str]:
    """保证交给 yt-dlp 的 cookiefile 一定是 Netscape 格式。

    从 F12 复制的是 ``SESSDATA=xxx; bili_jct=yyy``，yt-dlp 的
    MozillaCookieJar 读不了（会直接报 LoadError 然后一个 cookie 都不带），
    所以这里就地转成一个 ``.netscape.txt`` 中间文件，原文件保持不动。
    """
    if not cookies_file:
        return None
    p = Path(cookies_file)
    if not p.exists():
        return None

    fmt = cookie_file_format(str(p))
    if fmt == "netscape":
        return str(p)
    if fmt != "raw":
        return None

    out = p.with_name(p.stem + ".netscape.txt")
    # 原文件没变就复用上次的转换结果
    try:
        if out.exists() and out.stat().st_mtime >= p.stat().st_mtime:
            return str(out)
    except OSError:
        pass

    pairs = parse_cookie_text(p.read_text(encoding="utf-8", errors="ignore"))
    if not pairs:
        return None
    body = "".join(
        f".bilibili.com\tTRUE\t/\tFALSE\t0\t{k}\t{v}\n" for k, v in pairs.items()
    )
    try:
        out.write_text(_NETSCAPE_HEADER + body, encoding="utf-8")
    except OSError:
        return None
    return str(out)


def cookie_header(cookies_file: Optional[str]) -> str:
    """把 cookies 文件（Netscape 或手动复制的 Cookie 头）转成 Cookie: 头。"""
    if not cookies_file:
        return ""
    try:
        text = Path(cookies_file).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    pairs = parse_cookie_text(text)
    if not pairs:
        return ""
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


@dataclass
class LoginState:
    """Cookies 是否仍然有效——直接决定能拿到多高的清晰度。"""
    checked: bool = False
    logged_in: bool = False
    uname: str = ""
    vip: bool = False
    message: str = "未检测"

    @property
    def badge(self) -> str:
        if not self.checked:
            return "未检测"
        if not self.logged_in:
            return "未登录 / Cookies 已失效"
        return f"已登录：{self.uname}" + ("（大会员）" if self.vip else "（普通用户）")


def check_login(cookies_file: Optional[str], proxy: str = "",
                timeout: int = 15) -> LoginState:
    """调用 B 站 nav 接口验证 Cookies 是否有效。

    B 站未登录时最高只有 480P，登录后 1080P，大会员才有 4K/8K。
    把这一步显式做出来，用户才能明白“最高清晰度”为什么变低了。
    """
    import urllib.request

    st = LoginState()
    if not cookies_file or not Path(cookies_file).exists():
        st.checked = True
        st.message = "未导入 Cookies（最高仅 480P）"
        return st

    ck = cookie_header(cookies_file)
    if not ck:
        st.checked = True
        st.message = "Cookies 文件为空或格式不正确"
        return st

    handlers = []
    if proxy.strip():
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        "https://api.bilibili.com/x/web-interface/nav",
        headers={"User-Agent": USER_AGENT, "Referer": REFERER, "Cookie": ck},
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        st.checked = True
        st.message = f"登录状态检测失败：{exc}"
        return st

    data = payload.get("data") or {}
    st.checked = True
    st.logged_in = bool(data.get("isLogin"))
    st.uname = data.get("uname") or ""
    st.vip = bool(data.get("vipStatus"))
    if st.logged_in:
        st.message = st.badge
    else:
        code = payload.get("code")
        st.message = ("Cookies 已过期，请重新导出（当前未登录，最高仅 480P）"
                      if code == -101 else
                      f"未登录（接口返回 code={code}），最高仅 480P")
    return st


_login_cache: Dict[tuple, "LoginState"] = {}


def effective_cookies(cookies_file: Optional[str], proxy: str = "") -> Optional[str]:
    """返回「真正应该发给 B 站」的 cookies 路径。

    这是一个很反直觉但实测成立的坑：B 站对「带着失效 SESSDATA」的请求，
    比完全匿名还要严格。

        同一个视频 BV1GJ411x7h7：
            带过期 cookies  → 最高 480P   [480, 360]
            不带任何 cookies → 最高 1080P  [1080, 720, 480, 360]

    也就是说，一个过期的 cookies 文件不仅没帮助，还会把本来能拿到的
    1080P 压到 480P —— 用户看到的“最高清晰度”因此名不副实。
    所以这里先验证登录态，一旦失效就干脆不发 cookies。
    """
    if not cookies_file:
        return None
    p = Path(cookies_file)
    try:
        if not p.exists():
            return None
        mtime = p.stat().st_mtime
    except OSError:
        return None

    key = (str(p), mtime, proxy or "")
    st = _login_cache.get(key)
    if st is None:
        st = check_login(str(p), proxy)
        _login_cache[key] = st
    return str(p) if st.logged_in else None


def clear_login_cache() -> None:
    _login_cache.clear()


def extract_bvid(url: str) -> str:
    m = _BV_RE.search(url or "")
    return m.group(1) if m else ""


def _http_get_text(url: str, cookies_file: Optional[str] = None, proxy: str = "",
                   timeout: int = 20, on_error: Optional[Callable[[str], None]] = None) -> Optional[str]:
    import urllib.request

    handlers = []
    if proxy.strip():
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    headers = {"User-Agent": USER_AGENT, "Referer": REFERER,
               "Accept-Language": "zh-CN,zh;q=0.9"}
    ck = cookie_header(cookies_file)
    if ck:
        headers["Cookie"] = ck
    try:
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception as exc:
        if on_error:
            on_error(f"{type(exc).__name__}: {exc}")
        return None


def _api_get(url: str, cookies_file: Optional[str], proxy: str, timeout: int = 20,
             on_error: Optional[Callable[[str], None]] = None) -> Optional[dict]:
    text = _http_get_text(url, cookies_file, proxy, timeout, on_error)
    if text is None:
        return None
    try:
        return json.loads(text)
    except Exception as exc:
        if on_error:
            on_error(f"返回内容不是合法 JSON：{exc}")
        return None


def fetch_video_pages(bvid: str, cookies_file: Optional[str] = None, proxy: str = "",
                      on_log: Optional[Callable[[str, str], None]] = None) -> List[Dict[str, Any]]:
    """取视频分P 列表（含 cid）。

    yt-dlp 的 info 字典里没有 cid，而 B 站弹幕接口需要它，所以必须另外查
    web-interface/view。返回的 pages 顺序与 yt-dlp 的 entries 顺序一致。

    这条链路比较脆弱（接口会要 buvid、会风控），所以做了三级回退：
    ① 带 cookies 调接口 → ② 不带 cookies 调接口 → ③ 抓视频页 HTML 里的 cid。
    任何一级成功即可，避免「视频下好了但弹幕没下到」这种半成品结果。
    """
    if not bvid:
        return []

    errors: List[str] = []
    api = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"

    for label, ck in (("带 Cookies", cookies_file), ("匿名", None)):
        if label == "匿名" and not cookies_file:
            continue
        payload = _api_get(api, ck, proxy, on_error=errors.append)
        if payload and payload.get("code") == 0:
            data = payload.get("data") or {}
            pages = data.get("pages") or []
            if not pages and data.get("cid"):
                pages = [{"page": 1, "cid": data.get("cid"),
                          "part": data.get("title") or "",
                          "duration": data.get("duration") or 0}]
            if pages:
                return pages
        elif payload:
            errors.append(f"{label}接口返回 code={payload.get('code')} "
                          f"{payload.get('message') or ''}".strip())

    # ③ 直接抓页面 HTML
    html = _http_get_text(f"https://www.bilibili.com/video/{bvid}",
                          cookies_file, proxy, on_error=errors.append)
    if html:
        cids = re.findall(r'"cid"\s*:\s*(\d+)', html)
        if cids:
            seen, pages = set(), []
            for c in cids:
                if c in seen:
                    continue
                seen.add(c)
                pages.append({"page": len(pages) + 1, "cid": c, "part": "", "duration": 0})
            return pages

    if on_log:
        detail = "；".join(errors[:3]) if errors else "接口无有效返回"
        on_log(f"未能获取 cid（{detail}），跳过弹幕下载（不影响视频）", "warn")
    return []


def fetch_danmaku(cid: str, out_path: Path, cookies_file: Optional[str] = None,
                  proxy: str = "", log: Optional[Callable[[str, str], None]] = None) -> Optional[Path]:
    """下载 B 站弹幕 XML（comment.bilibili.com 返回 deflate 压缩数据）。"""
    import urllib.request
    import urllib.error

    log = log or (lambda m, t=None: None)
    if not cid:
        return None
    url = f"https://comment.bilibili.com/{cid}.xml"
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": REFERER,
        "Accept-Encoding": "gzip, deflate",
    }
    ck = cookie_header(cookies_file)
    if ck:
        headers["Cookie"] = ck

    handlers = []
    if proxy.strip():
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)

    try:
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=30) as resp:
            raw = resp.read()
    except Exception as exc:
        log(f"弹幕下载失败：{exc}", "warn")
        return None

    data = raw
    if raw[:2] not in (b"<?", b"\xef\xbb"):
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS, 47):
            try:
                data = zlib.decompress(raw, wbits)
                break
            except Exception:
                continue
    if not data.lstrip().startswith(b"<"):
        log("弹幕内容异常，已跳过", "warn")
        return None

    try:
        out_path.write_bytes(data)
        log(f"弹幕已保存：{out_path.name}", "ok")
        return out_path
    except OSError as exc:
        log(f"弹幕写入失败：{exc}", "warn")
        return None


# ══════════════════════════════════════════════════════════════════════
# 错误信息汉化
# ══════════════════════════════════════════════════════════════════════
_ERROR_RULES: Sequence[tuple[str, str]] = (
    ("HTTP Error 404", "视频不存在或已被删除（404）"),
    ("HTTP Error 403", "访问被拒绝（403）：可能需要登录 Cookies，或该内容有地区限制"),
    ("HTTP Error 412", "被 B 站风控拦截（412）：请稍后重试，或更新 yt-dlp"),
    ("HTTP Error 429", "请求过于频繁（429）：请稍后再试"),
    ("Unsupported URL", "不支持的链接，请检查是否为 B 站视频地址"),
    ("Sign in to confirm", "需要登录才能访问：请在界面中导入 Cookies"),
    ("login", "需要登录：请导入 B 站 Cookies"),
    ("This video is only available for registered users",
     "该视频仅限登录用户观看：请导入 Cookies"),
    ("Premium", "该清晰度需要大会员：请使用已开通大会员的账号 Cookies"),
    ("requested format is not available",
     "所选清晰度当前不可用，已自动回退到可用的最高清晰度"),
    ("ffmpeg not found", "未找到 ffmpeg，无法合并音视频"),
    ("ffprobe", "未找到 ffprobe，请确认 tools 目录完整"),
    ("Unable to download webpage", "无法访问页面：请检查网络连接"),
    ("timed out", "网络超时：请检查网络或代理设置"),
    ("certificate", "证书校验失败：已启用跳过校验，如仍失败请检查代理"),
    ("Permission denied", "没有写入权限：请更换保存目录"),
    ("No space left", "磁盘空间不足"),
    ("already been downloaded", "文件已存在，已跳过（如需重新下载请先删除原文件）"),
    ("has already been recorded in the archive", "该视频已在下载记录中，已跳过"),
)


def _friendly_error(exc: Any) -> str:
    raw = str(exc)
    low = raw.lower()
    for needle, nice in _ERROR_RULES:
        if needle.lower() in low:
            return nice
    raw = re.sub(r"^ERROR:\s*", "", raw).strip()
    raw = re.sub(r"\s+", " ", raw)
    return raw[:300] if raw else "下载失败"


# ══════════════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════════════
def human_size(n: float) -> str:
    try:
        n = float(n)
    except Exception:
        return "-"
    if n <= 0:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return "-"


def human_speed(n: float) -> str:
    if not n or n <= 0:
        return ""
    return human_size(n) + "/s"


def human_eta(sec: float) -> str:
    if not sec or sec <= 0:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def open_folder(path: str) -> bool:
    try:
        if is_frozen() or os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


def select_tier(tiers: List[QualityTier], key: str) -> Optional[QualityTier]:
    for t in tiers:
        if t.key == key:
            return t
    return None

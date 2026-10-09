#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BiliDown v0.2 统一入口。

* 不带参数                → 启动图形界面
* --selftest --out F      → 环境自检（打包后无控制台，结果写入 F）
* --probe URL --out F     → 探测清晰度，结果写入 F
* --download URL --out F  → 直接下载，结果写入 F

打包后是 windowed 程序，`sys.stdout` 为 None，因此所有诊断输出都落到 --out
指定的文件里，这样才能在自动化脚本中验证成品是否真的可用。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# windowed 模式下没有标准输出，argparse / print 都会炸
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bili_core as C  # noqa: E402


# ══════════════════════════════════════════════════════════════════════
def _emit(payload: dict, out_path: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out_path:
        try:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text(text, encoding="utf-8")
        except OSError:
            pass
    try:
        print(text)
    except Exception:
        pass


def _tool_version(exe: str | None) -> str:
    if not exe:
        return ""
    try:
        p = subprocess.run([exe, "-version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20,
                           creationflags=C.CREATE_NO_WINDOW)
        first = (p.stdout or "").splitlines()
        return first[0][:120] if first else ""
    except Exception as exc:
        return f"<失败: {exc}>"


def _ytdlp_version() -> str:
    try:
        import yt_dlp
        return getattr(yt_dlp.version, "__version__", "?")
    except Exception as exc:
        return f"<不可用: {exc}>"


# ══════════════════════════════════════════════════════════════════════
def _cookiefile_check(cookiefile: str | None) -> dict:
    """确认转换后的 cookiefile 真的能被 yt-dlp 的 cookie jar 读进去。

    只判断文件格式是不够的：格式对但 jar 读不进去，下载时一样是匿名。
    """
    if not cookiefile:
        return {"loaded": False, "reason": "没有可用的 cookiefile"}
    try:
        from yt_dlp.cookies import YoutubeDLCookieJar
        jar = YoutubeDLCookieJar(cookiefile)
        jar.load(ignore_discard=True, ignore_expires=True)
        names = sorted({c.name for c in jar})
        return {"loaded": True, "count": len(names),
                "has_sessdata": "SESSDATA" in names,
                "sample": names[:6]}
    except Exception as exc:
        return {"loaded": False, "error": f"{type(exc).__name__}: {exc}"}


def cmd_selftest(args) -> int:
    ffmpeg = C.find_ffmpeg()
    ffprobe = C.find_ffprobe()
    # 显式 --cookies 优先，便于验证「手动 F12 复制的 Cookie 头」这条路
    cookies = Path(args.cookies) if getattr(args, "cookies", None) else C.find_cookies()
    login = C.check_login(str(cookies) if cookies else None)

    # 有效 cookies：失效的 cookies 会反向压低画质，必须确认实际用了哪一份
    eff = C.effective_cookies(str(cookies) if cookies else None)

    api_msgs: list[str] = []
    pages = C.fetch_video_pages("BV1GJ411x7h7", str(cookies) if cookies else None,
                                on_log=lambda m, t=None: api_msgs.append(m))

    payload = {
        "app": C.APP_NAME,
        "version": C.APP_VERSION,
        "frozen": C.is_frozen(),
        "executable": sys.executable,
        "app_dir": str(C.app_dir()),
        "resource_dir": str(C.resource_dir()),
        "python": sys.version.split()[0],
        "yt_dlp_module": _ytdlp_version(),
        "engine": C._engine_name(),
        "ffmpeg": ffmpeg,
        "ffmpeg_version": _tool_version(ffmpeg),
        "ffprobe": ffprobe,
        "ffmpeg_dir": C.find_ffmpeg_dir(),
        "cookies": str(cookies) if cookies else None,
        "cookie_format": C.cookie_file_format(str(cookies) if cookies else None),
        "cookiefile_for_ytdlp": C.ensure_netscape_cookiefile(
            str(cookies) if cookies else None),
        "cookiefile_check": _cookiefile_check(
            C.ensure_netscape_cookiefile(str(cookies) if cookies else None)),
        "cookies_effective": eff,
        "cookies_ignored": bool(cookies) and not eff,
        "api_check": {"pages": len(pages),
                      "first_cid": (pages[0].get("cid") if pages else None),
                      "messages": api_msgs},
        "login": {
            "checked": login.checked,
            "logged_in": login.logged_in,
            "uname": login.uname,
            "vip": login.vip,
            "message": login.message,
        },
        "writable_app_dir": os.access(str(C.app_dir()), os.W_OK),
        "fallback_tiers": [t.key for t in C.fallback_tiers()],
        "downloads_dir": str(C.downloads_dir()),
    }

    if getattr(args, "url", None):
        info = C.probe(args.url, cookies=str(cookies) if cookies else None)
        payload["probe"] = {
            "ok": info.ok,
            "error": info.error,
            "title": info.title,
            "uploader": info.uploader,
            "duration": info.duration,
            "max_label": info.max_label,
            "max_height": info.tiers[0].height if info.tiers else 0,
            "max_fps": info.tiers[0].fps if info.tiers else 0,
            "max_selector": info.tiers[0].selector if info.tiers else "",
            "tiers": [{"key": t.key, "height": t.height, "fps": t.fps, "label": t.label}
                      for t in info.tiers],
        }

    payload["checks"] = {
        "engine_ok": C.has_builtin_ytdlp(),
        "ffmpeg_ok": bool(ffmpeg),
        "ffprobe_ok": bool(ffprobe),
        "bundled": C.is_frozen(),
    }
    # 弹幕接口需要登录态才通，没配 cookies 时只作为信息上报，不计入 ok ——
    # 否则一个不带凭证的全新克隆会被误判为"自检失败"
    payload["danmaku_api_ok"] = len(pages) > 0
    payload["ok"] = all(payload["checks"].values())

    _emit(payload, args.out)
    return 0 if payload["ok"] else 1


def cmd_probe(args) -> int:
    found = C.find_cookies()
    info = C.probe(args.url,
                   cookies=args.cookies or (str(found) if found else None),
                   proxy=args.proxy or "")
    payload = {
        "ok": info.ok,
        "error": info.error,
        "url": info.url,
        "title": info.title,
        "uploader": info.uploader,
        "duration": info.duration,
        "is_playlist": info.is_playlist,
        "parts": len(info.parts),
        "max_label": info.max_label,
        "max_height": info.tiers[0].height if info.tiers else 0,
        "max_fps": info.tiers[0].fps if info.tiers else 0,
        # 这两项是「最高清晰度是否真的对应最高」的核心证据
        "max_selector": info.tiers[0].selector if info.tiers else "",
        "tiers": [{"key": t.key, "height": t.height, "fps": t.fps,
                   "label": t.label, "selector": t.selector} for t in info.tiers],
    }
    _emit(payload, args.out)
    return 0 if info.ok else 1


def cmd_download(args) -> int:
    # 不显式指定时必须回退到程序目录里的 cookies，否则 B 站弹幕/分P 接口
    # 会直接返回 412（风控），拿不到 cid。
    found = C.find_cookies()
    cookies = args.cookies or (str(found) if found else "")
    plan = C.DownloadPlan(
        outdir=args.outdir or str(C.downloads_dir()),
        selector=args.selector or "bestvideo+bestaudio/best",
        container=args.container or "mp4",
        quality_label=args.selector or "测试",
        cookies=cookies,
        proxy=args.proxy or "",
        write_danmaku=bool(args.danmaku),
        playlist_items=args.items or "",
        playlist_all=not bool(args.items),
    )
    lines: list[str] = []
    t0 = time.time()
    result = C.download(args.url, plan, on_log=lambda m, t=None: lines.append(f"[{t or '-'}] {m}"))
    payload = {
        "ok": result.ok,
        "cancelled": result.cancelled,
        "error": result.error,
        "seconds": round(time.time() - t0, 1),
        "files": result.files,
        "file_sizes": {f: (Path(f).stat().st_size if Path(f).exists() else 0)
                       for f in result.files},
        "log": lines[-60:],
    }
    _emit(payload, args.out)
    return 0 if result.ok else 1


# ══════════════════════════════════════════════════════════════════════
def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        from bili_gui import main as gui_main
        gui_main()
        return 0

    import argparse
    parser = argparse.ArgumentParser(prog="BiliDown", add_help=True)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("url", nargs="?")
    parser.add_argument("--out")
    parser.add_argument("--outdir")
    parser.add_argument("--selector")
    parser.add_argument("--container", default="mp4")
    parser.add_argument("--cookies")
    parser.add_argument("--proxy")
    parser.add_argument("--items")
    parser.add_argument("--danmaku", action="store_true")
    args = parser.parse_args(argv)

    if args.version:
        _emit({"version": C.APP_VERSION}, args.out)
        return 0
    if args.selftest:
        return cmd_selftest(args)
    if args.probe:
        if not args.url:
            _emit({"ok": False, "error": "缺少 URL"}, args.out)
            return 2
        return cmd_probe(args)
    if args.download:
        if not args.url:
            _emit({"ok": False, "error": "缺少 URL"}, args.out)
            return 2
        return cmd_download(args)

    from bili_gui import main as gui_main
    gui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())

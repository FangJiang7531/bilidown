#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BiliDown v0.2 —— 命令行版

与 GUI 共用 bili_core，所以「最高清晰度」的判定逻辑完全一致：
先探测真实可用档位，再精确选择，而不是盲目丢一个 best 给 yt-dlp。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bili_core as C  # noqa: E402

# Windows 控制台默认 GBK，中文会乱码
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


def _banner() -> str:
    return f"{C.APP_NAME} v{C.APP_VERSION} — B 站视频下载器（命令行）"


def _print_tiers(info: C.VideoInfo) -> None:
    print(f"  标题      : {info.title}")
    if info.uploader:
        print(f"  UP 主     : {info.uploader}")
    if info.duration:
        print(f"  时长      : {C.fmt_duration(info.duration)}")
    if info.is_playlist and len(info.parts) > 1:
        print(f"  分P 数量  : {len(info.parts)}")
    print(f"  最高可用  : {info.max_label}")
    print("  可用清晰度:")
    for i, t in enumerate(info.tiers):
        mark = " <== 最高清晰度" if i == 0 else ""
        print(f"    [{i + 1:>2}] {t.label:<28} {t.height}p{t.fps if t.fps >= 48 else ''}{mark}")


def cmd_login(args) -> int:
    ck = args.cookies or (str(C.find_cookies()) if C.find_cookies() else None)
    st = C.check_login(ck, args.proxy or "")
    print(_banner())
    print(f"Cookies 文件 : {ck or '未找到'}")
    print(f"登录状态     : {st.badge}")
    print(f"说明         : {st.message}")
    if not st.logged_in:
        print("提示：未登录时 B 站最高只提供 480P，登录后可到 1080P，大会员才有 4K/8K。")
    return 0 if st.logged_in else 1


def cmd_info(args) -> int:
    ck = args.cookies or (str(C.find_cookies()) if C.find_cookies() else None)
    print(_banner())
    print(f"解析: {C.normalize_url(args.url)}")
    info = C.probe(args.url, cookies=ck, proxy=args.proxy or "")
    if not info.ok:
        print(f"解析失败: {info.error}")
        return 1
    _print_tiers(info)
    return 0


def cmd_download(args) -> int:
    ck = args.cookies or (str(C.find_cookies()) if C.find_cookies() else None)
    print(_banner())

    # ── 先探测，把「最高清晰度」落实成真实存在的档位 ──────────────────
    selector = "bestvideo+bestaudio/best"
    label = "最高清晰度"
    info = None
    if not args.no_probe:
        print(f"解析中: {C.normalize_url(args.url)}")
        info = C.probe(args.url, cookies=ck, proxy=args.proxy or "")
        if info.ok:
            _print_tiers(info)
        else:
            print(f"（探测失败，改用通用选择器：{info.error}）")

    if info and info.ok:
        tiers = info.tiers
        if args.quality in ("max", "best", ""):
            tier = tiers[0]
        elif args.quality.isdigit():
            idx = int(args.quality)
            tier = tiers[idx - 1] if 1 <= idx <= len(tiers) else tiers[0]
        else:
            want = args.quality.lower().replace("p", "")
            tier = next((t for t in tiers if str(t.height) == want.split("f")[0]), None)
            if tier is None:
                # 没有恰好等于的，取不超过该高度的最高档
                try:
                    lim = int(want.split("f")[0])
                except ValueError:
                    lim = 0
                tier = next((t for t in tiers if t.height <= lim), tiers[-1])
        selector, label = tier.selector, tier.label
        if tier.height and tier.height < (info.tiers[0].height):
            print(f"注意：选择的是 {label}，本次最高可用为 {info.max_label}")

    container = args.format
    audio_only = container in ("m4a", "mp3")
    if audio_only:
        container = "mp4"

    plan = C.DownloadPlan(
        outdir=args.output or str(C.downloads_dir()),
        selector=selector,
        container=container,
        quality_label=label,
        cookies=ck or "",
        proxy=args.proxy or "",
        write_danmaku=args.danmaku,
        write_thumbnail=args.cover,
        write_subs=args.subs,
        audio_only=audio_only,
        audio_format=args.format if audio_only else "m4a",
        playlist_all=not bool(args.items),
        playlist_items=args.items or "",
    )
    print(f"画质: {label}")
    print(f"格式: {args.format}{'（仅音频）' if audio_only else ''}")
    print(f"保存: {plan.outdir}")
    print("-" * 64)

    _last["pct"] = -10
    result = C.download(
        args.url, plan,
        on_log=_log_line,
        on_progress=_progress_printer,
    )
    _clear_line()
    print("-" * 64)
    if result.cancelled:
        print("已取消")
        return 130
    if result.ok:
        for f in result.files:
            print(f"完成: {f}")
        return 0
    print(f"失败: {result.error}")
    return 1


_last = {"pct": -10}
_TTY = sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else False


def _clear_line() -> None:
    if _TTY:
        sys.stdout.write("\r" + " " * 78 + "\r")
        sys.stdout.flush()


def _log_line(msg: str, tag=None) -> None:
    _clear_line()
    print(f"  {msg}")


def _progress_printer(p: C.TaskProgress) -> None:
    """终端里按 5% 步进刷新，避免刷屏。

    进度行用 \\r 原地重绘，写日志前先擦掉它，否则两者会叠在一起。
    """
    pct = int(p.percent)
    if pct // 5 != _last["pct"] // 5 or p.stage != "下载中":
        _last["pct"] = pct
        speed = C.human_speed(p.speed)
        eta = C.human_eta(p.eta)
        extra = "  ".join(x for x in (speed, f"ETA {eta}" if eta else "") if x)
        if not _TTY:
            return
        sys.stdout.write(f"\r  [{pct:>3}%] {p.stage:<8} {extra:<28}")
        sys.stdout.flush()


def main() -> int:
    p = argparse.ArgumentParser(
        prog="bili",
        description="BiliDown v0.2 — B 站视频下载器（命令行）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  bili info BV1xx411c7mD          查看可用清晰度\n"
            "  bili BV1xx411c7mD               下载最高清晰度\n"
            "  bili BV1xx411c7mD -q 1080p      指定清晰度\n"
            "  bili BV1xx411c7mD -q 2          按列表序号选择\n"
            "  bili BV1xx411c7mD -f mp3        仅下载音频\n"
            "  bili BV1xx411c7mD -i 1,3        只下载第 1、3 个分P\n"
            "  bili login                      检查 Cookies 登录状态\n"
        ),
    )
    sub = {"info", "login"}
    argv = sys.argv[1:]
    if argv and argv[0] in sub:
        p.add_argument("cmd", choices=sorted(sub))
        p.add_argument("url", nargs="?")
    else:
        p.add_argument("url", nargs="?")

    p.add_argument("-o", "--output", help="保存目录")
    p.add_argument("-q", "--quality", default="max",
                   help="清晰度：max / 2160p / 1080p / 720p / 480p / 360p / 列表序号")
    p.add_argument("-f", "--format", default="mp4",
                   choices=["mp4", "mkv", "webm", "m4a", "mp3"], help="输出格式")
    p.add_argument("-i", "--items", help="分P 选择，例如 1,3,5 或 1-5")
    p.add_argument("--cookies", help="Cookies 文件（Netscape 格式）")
    p.add_argument("--proxy", help="代理，例如 http://127.0.0.1:7890")
    p.add_argument("--danmaku", action="store_true", help="同时下载弹幕 XML")
    p.add_argument("--cover", action="store_true", help="同时下载封面")
    p.add_argument("--subs", action="store_true", help="同时下载字幕（需登录）")
    p.add_argument("--no-probe", action="store_true",
                   help="跳过清晰度探测，直接用通用 best 选择器（更快）")
    args = p.parse_args()

    if getattr(args, "cmd", None) == "login":
        return cmd_login(args)
    if getattr(args, "cmd", None) == "info":
        if not args.url:
            p.error("info 需要提供 URL")
        return cmd_info(args)

    if not args.url:
        p.print_help()
        return 2
    return cmd_download(args)


if __name__ == "__main__":
    sys.exit(main())

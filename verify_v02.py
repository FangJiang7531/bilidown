#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成品验收脚本：验证 成品/v0.2 是否真的能在「干净环境」下工作。

    python verify_v02.py

做三件事：
  1. 自检  —— 引擎 / ffmpeg / ffprobe 是否全部来自成品目录本身
  2. 探测  —— 真实 B 站链接，校验「最高清晰度」是否等于解析出的第一档
  3. 下载  —— 真实下载一个低清晰度文件，并用 ffprobe 确认产物可播放

为了模拟「别的电脑」，运行 exe 时会清洗环境变量：PATH 只留 system32、
清空 PYTHONHOME / PYTHONPATH，确保它没有偷偷依赖本机的 Python 或 PATH 里的 ffmpeg。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG = ROOT / "成品" / "v0.2"
EXE = PKG / "BiliDown.exe"
TEST_URL = "BV1GJ411x7h7"          # 【官方 MV】Never Gonna Give You Up

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    return ok


def clean_env() -> dict:
    """尽量模拟一台没装 Python / ffmpeg 的电脑。"""
    env = dict(os.environ)
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    env["PATH"] = f"{sysroot}\\system32;{sysroot}"
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONIOENCODING", None)
    return env


def run_exe(args: list[str], timeout: int = 300) -> tuple[int, dict | None, str]:
    """运行成品 exe（窗口程序，无 stdout），结果从 --out 的 JSON 读取。"""
    tmp = Path(tempfile.gettempdir()) / f"bilidown_verify_{time.time_ns()}.json"
    cmd = [str(EXE)] + args + ["--out", str(tmp)]
    t0 = time.time()
    proc = subprocess.run(cmd, env=clean_env(), timeout=timeout,
                          capture_output=True)
    elapsed = time.time() - t0
    data = None
    if tmp.exists():
        try:
            data = json.loads(tmp.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"    (JSON 解析失败: {exc})")
        finally:
            tmp.unlink(missing_ok=True)
    return proc.returncode, data, f"{elapsed:.1f}s"


def make_raw_cookie_file(src: Path, dst: Path) -> int:
    """把 Netscape cookies 文件转成『F12 手动复制』那种 Cookie 头文本。

    这里刻意不复用 bili_core 的解析器 —— 测试输入要独立于被测代码，
    否则解析器一起写错时两边会一起"通过"。
    """
    pairs = []
    for line in src.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 7 and "." in parts[0] and "bilibili" in parts[0].lower():
            pairs.append(f"{parts[5]}={parts[6]}")
    dst.write_text("cookie: " + "; ".join(pairs), encoding="utf-8")
    return len(pairs)


def main() -> int:
    print("=" * 64)
    print("  BiliDown 成品验收")
    print("=" * 64)

    if not EXE.exists():
        print(f"找不到成品：{EXE}")
        return 2
    print(f"成品目录: {PKG}")
    total = sum(f.stat().st_size for f in PKG.rglob("*") if f.is_file())
    print(f"体积    : {total / 1024 / 1024:.1f} MB\n")

    # ── 1. 自检 ───────────────────────────────────────────────────────
    print("[1/4] 自检（清洗 PATH，模拟纯净电脑）")
    rc, st, took = run_exe(["--selftest"], timeout=180)
    if not check("exe 能启动并返回结果", st is not None, took):
        print("  → 成品无法运行，后续检查中止")
        return 1

    check("以冻结模式运行", bool(st.get("frozen")), f"exe={st.get('executable')}")
    check("内置 yt-dlp 可用", bool(st.get("checks", {}).get("engine_ok")),
          f"版本 {st.get('yt_dlp_module')}")
    check("ffmpeg 来自成品目录", bool(st.get("checks", {}).get("ffmpeg_ok")) and
          str(PKG) in str(st.get("ffmpeg") or ""), str(st.get("ffmpeg")))
    check("ffprobe 来自成品目录", bool(st.get("checks", {}).get("ffprobe_ok")) and
          str(PKG) in str(st.get("ffprobe") or ""), str(st.get("ffprobe")))
    check("ffmpeg 可执行", "ffmpeg version" in str(st.get("ffmpeg_version")),
          str(st.get("ffmpeg_version"))[:70])
    login = st.get("login") or {}
    check("登录状态检测可用", bool(login.get("checked")), login.get("message"))

    # 回归：失效的 cookies 会反向把画质从 1080P 压到 480P，必须被自动停用
    ignored = bool(st.get("cookies_ignored"))
    check("失效 Cookies 已自动停用（否则画质会被压低）", ignored,
          f"effective={st.get('cookies_effective') or '不使用'}")

    api = st.get("api_check") or {}
    check("弹幕 cid 接口可用（B 站风控 412 回归）",
          int(api.get("pages") or 0) > 0,
          f"pages={api.get('pages')} cid={api.get('first_cid')} "
          f"{str(api.get('messages'))[:60]}")
    check("成品目录可写（config.json）", bool(st.get("writable_app_dir")))
    check("整体自检通过", bool(st.get("ok")))

    # ── 2. F12 手动复制的 Cookie 头 ───────────────────────────────────
    # 用户按 README 4.3 从开发者工具复制的是 "a=b; c=d" 这种 Cookie 头，
    # 不是 Netscape 格式；程序必须能识别、并转成 yt-dlp 读得进去的文件。
    print("\n[2/4] F12 手动复制的 Cookie 头（README 4.3 的路线）")
    src_ck = PKG / "www.bilibili.com_cookies.txt"
    if src_ck.exists():
        tmpdir = Path(tempfile.gettempdir()) / f"bilidown_rawck_{time.time_ns()}"
        tmpdir.mkdir(parents=True, exist_ok=True)
        raw_ck = tmpdir / "cookies.txt"
        n = make_raw_cookie_file(src_ck, raw_ck)
        check("能造出模拟的 F12 复制内容", n > 0, f"{n} 项")

        rc, raw_st, took = run_exe(
            ["--selftest", "--cookies", str(raw_ck)], timeout=180)
        if check("带手动 Cookie 头时自检可运行", raw_st is not None, took):
            check("识别为 raw（手动复制）格式",
                  raw_st.get("cookie_format") == "raw",
                  str(raw_st.get("cookie_format")))
            conv = str(raw_st.get("cookiefile_for_ytdlp") or "")
            check("自动转换出 Netscape 中间文件",
                  conv.endswith(".netscape.txt") and Path(conv).exists(),
                  Path(conv).name if conv else "(无)")
            jc = raw_st.get("cookiefile_check") or {}
            check("yt-dlp 能加载转换后的 cookiefile",
                  bool(jc.get("loaded")), f"{jc.get('count')} 个 cookie",
                  )
            check("转换结果里带着 SESSDATA（登录态没丢）",
                  bool(jc.get("has_sessdata")),
                  str(jc.get("sample") or jc.get("error") or "")[:70])
        shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        check("找得到用于测试的 cookies 文件", False, str(src_ck))

    # ── 3. 清晰度探测 ─────────────────────────────────────────────────
    print("\n[3/4] 真实链接清晰度探测")
    rc, pr, took = run_exe(["--probe", TEST_URL], timeout=240)
    if not check("探测成功", bool(pr and pr.get("ok")), took):
        if pr:
            print(f"    error: {pr.get('error')}")
        return 1

    tiers = pr.get("tiers") or []
    check("返回了清晰度列表", len(tiers) > 0,
          " / ".join(f"{t['height']}p" for t in tiers))
    if tiers:
        top = tiers[0]
        check("列表按从高到低排序",
              all(tiers[i]["height"] >= tiers[i + 1]["height"]
                  for i in range(len(tiers) - 1)))
        check("「最高清晰度」= 列表第一档",
              pr.get("max_height") == top["height"] and pr.get("max_fps") == top["fps"],
              f"{pr.get('max_label')}")
        sel = str(pr.get("max_selector") or "")
        check("选择器精确锁定该高度（非模糊 best）",
              f"height={top['height']}" in sel, sel[:72])
        print(f"    标题: {pr.get('title')}")
        print(f"    最高可用: {pr.get('max_label')}")

    # ── 3. 真实下载 ───────────────────────────────────────────────────
    print("\n[4/4] 真实下载（最低清晰度 + 弹幕）")
    outdir = Path(tempfile.gettempdir()) / f"bilidown_pkg_test_{time.time_ns()}"
    outdir.mkdir(parents=True, exist_ok=True)
    sel = "bestvideo[height<=360]+bestaudio/best" 
    rc, dl, took = run_exe(
        ["--download", TEST_URL, "--outdir", str(outdir),
         "--selector", sel, "--danmaku"], timeout=900)
    check("下载流程返回结果", dl is not None, took)
    if dl:
        check("下载成功", bool(dl.get("ok")), str(dl.get("error"))[:90])
        files = dl.get("files") or []
        check("产出了文件", len(files) > 0, f"{len(files)} 个")
        media = [f for f in files if f.lower().endswith((".mp4", ".mkv", ".webm", ".m4a", ".mp3"))]
        check("包含音视频文件", len(media) > 0)
        if media:
            size = Path(media[0]).stat().st_size
            check("文件非空", size > 100 * 1024, f"{size / 1024 / 1024:.2f} MB")
            # 用成品自带的 ffprobe 验证产物真的可播放
            ffprobe = PKG / "tools" / "ffprobe.exe"
            if ffprobe.exists():
                p = subprocess.run(
                    [str(ffprobe), "-v", "error", "-show_entries",
                     "stream=codec_type,codec_name,height",
                     "-of", "json", media[0]],
                    capture_output=True, text=True)
                try:
                    streams = json.loads(p.stdout).get("streams", [])
                except Exception:
                    streams = []
                kinds = {s.get("codec_type") for s in streams}
                check("ffprobe 能解析产物（音视频轨齐全）",
                      {"video", "audio"} <= kinds,
                      ", ".join(f"{s.get('codec_type')}:{s.get('codec_name')}"
                                for s in streams))
        danmaku = [f for f in files if f.endswith(".danmaku.xml")]
        check("弹幕已下载", len(danmaku) > 0)

    # ── 汇总 ──────────────────────────────────────────────────────────
    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    print(f"  结果: {passed} 通过 / {failed} 失败  （共 {len(results)} 项）")
    for name, ok, _ in results:
        if not ok:
            print(f"    ✗ {name}")
    print("=" * 64)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

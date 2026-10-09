#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BiliDown v0.2 一键打包脚本。

    python build_v02.py

流程：生成图标 → PyInstaller(onedir) → 组装 成品/v0.2 → 打印体积清单。

成品目录结构（整包拷到别的电脑即可用，无需 Python / ffmpeg / yt-dlp）：

    成品/v0.2/
      BiliDown.exe
      _internal/          PyInstaller 运行库（内含 Python 与 yt-dlp）
      tools/ffmpeg.exe    exe 同级，便于用户直接替换升级
      tools/ffprobe.exe
      downloads/
      www.bilibili.com_cookies.txt
      使用说明.txt
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist_v02"
BUILD = ROOT / "build_v02"


def _final_dir() -> Path:
    """成品输出目录。

    如果已经存在 成品*/v0.2（用户可能把「成品」改成更好懂的名字），
    就沿用同一个位置，免得重新打包后凭空多出一个新文件夹。
    """
    for parent in sorted(p for p in ROOT.glob("成品*") if p.is_dir()):
        if (parent / "v0.2").exists():
            return parent / "v0.2"
    return ROOT / "成品" / "v0.2"


FINAL = _final_dir()

README_TXT = """\
╔══════════════════════════════════════════════════════════════╗
║            BiliDown v0.2  —— 使用说明                         ║
╚══════════════════════════════════════════════════════════════╝

【怎么用】
  1. 双击 BiliDown.exe
  2. 粘贴 B 站视频链接（BV 号 / av 号 / b23.tv 短链 / 合集链接）
  3. 界面会自动解析，并在“清晰度”下拉框里列出这部视频
     【真实可用】的全部档位
  4. 保持默认的“最高清晰度”，点“开始下载”

  本文件夹已经内置 Python 运行时、yt-dlp、ffmpeg，
  换一台没有装任何环境的电脑，直接复制整个文件夹就能用。

──────────────────────────────────────────────────────────────
【关于“最高清晰度”】
  B 站是按登录状态分级放行的，不同状态下能拿到的最高画质完全不同：

      未登录            →  最高 480P
      已登录（普通）     →  最高 1080P
      已登录（大会员）   →  最高 4K / 8K / HDR / 杜比视界

  界面上的“登录状态”会实时告诉你当前处于哪一档。
  如果显示“Cookies 已失效”，导入新的 Cookies 后即可解锁更高画质。

  下拉框里的“最高清晰度”永远等于本次解析结果的第一档，
  所以它不会名不副实 —— 显示 4K 就是真的 4K，只有 480P 也会如实写明。

──────────────────────────────────────────────────────────────
【Cookies 怎么导出】—— 决定你能下到多高的画质，务必看

  · 不带 Cookies（匿名）        →  最高 480P
  · 已登录（普通用户）           →  最高 1080P
  · 已登录（大会员）             →  最高 4K / 8K / HDR

  ── 方法一：F12 手动复制（推荐，不用装任何扩展）────────────

    1. 打开并登录 https://www.bilibili.com
       先确认右上角是自己的头像 —— 没登录的话复制出来的是游客
       cookies，一点用都没有
    2. 按 F12 打开开发者工具（或右键页面 →“检查”）
    3. 切到 “Network / 网络” 标签
    4. 按 F5 刷新页面，等左侧请求列表刷出来
    5. 在列表里点最上面那个请求（一般是 www.bilibili.com 这个
       文档请求；点别的 bilibili 请求也行）
    6. 右侧选 “Headers / 标头”，往下滚到
       “Request Headers / 请求标头”
    7. 找到以 cookie: 开头的那一行。它的值非常长（几百到几千
       字符），这是正常的。复制冒号后面的那一整串 —— 从
       SESSDATA= 或 buvid3= 开始一直复制到行尾
    8. 新建记事本文件粘贴进去，然后“文件 → 另存为”：
         · 文件名   ：cookies.txt
         · 保存类型 ：所有文件 (*.*)   ← 不选会变成 cookies.txt.txt
         · 编码     ：UTF-8
    9. 回到本程序，点“导入 Cookies”，选中刚保存的 cookies.txt

    更省事的替代做法：第 5 步改成在请求上
    “右键 → Copy → Copy as cURL”，把整段粘贴进记事本保存也行，
    程序会自动从里面把 Cookie: 那部分抠出来。

    怎么确认复制对了：粘贴的内容里必须能看到 SESSDATA= 。
    如果没有，说明复制的不是已登录状态下的 B 站请求，重新复制。

  ── 方法二：用浏览器扩展导出 ──────────────────────────────

    1. 扩展商店搜索并安装 “Get cookies.txt LOCALLY”
       （Chrome 应用商店 / Edge 加载项商店 / Firefox 附加组件）
    2. 打开并登录 https://www.bilibili.com
    3. 点扩展图标 → Export 导出
    4. 回到本程序点“导入 Cookies”
    备选：Cookie-Editor —— 导出时必须手动选 “Netscape” 格式。

  ── 导入成功的标志 ────────────────────────────────────────

    · 界面“登录状态”显示：已登录：你的用户名（大会员）
    · 日志出现：登录状态：已登录：你的用户名（大会员）
    · 点“解析清晰度”后，下拉框第一项出现 1080P 或更高

  ── 支持的格式（大多数失败都出在这）──────────────────────

    两种都收，程序会自动识别：
      A. 手动复制的 Cookie 头：SESSDATA=xxx; bili_jct=yyy; ...
         （F12 复制出来的就是这种）
      B. Netscape 格式：7 列制表符分隔的纯文本（扩展导出的那种）

    F12 复制出来的是 A，而 yt-dlp 只认 B。程序会在同目录生成一个
    xxx.netscape.txt 中间文件（原文件不动），日志会写
    “识别为手动复制的 Cookie 头（N 项），已自动转换供 yt-dlp 使用”。
    这是正常现象，不是报错。也支持直接粘贴整段 Copy as cURL。

    下面这些拿不到有效 cookies：
      ✗ 控制台敲 document.cookie —— SESSDATA 是 HttpOnly，JS 读不到
        （注意这跟上面的 F12 网络面板不是一回事，网络面板能看到
         浏览器真实发出的请求头，所以 HttpOnly 也在里面）
      ✗ 各种“导出为 JSON”的插件输出
      ⚠ F12 → Application → Cookies 那张表理论上也能用，但得一条条
        手抄拼成 “名字=值; 名字=值”，又慢又容易错，不如直接复制整行

  ── 有效期与排查 ──────────────────────────────────────────

    · SESSDATA 一般 1~3 个月过期，掉登录了重新复制一份即可
    · 显示“未登录 / Cookies 已失效” = 过期了，重新弄一份
    · 复制的内容里没有 SESSDATA= → 复制到的不是已登录的请求
    · 保存后文件名变成 cookies.txt.txt → 另存为时没改“保存类型”
    · 日志提示“Cookies 已失效”但照样能下 1080P 是正常的 ——
      程序会故意不带失效 Cookies 请求，因为带失效 Cookies 反而
      会被 B 站压到 480P，不带才是 1080P
    · 点“清除”只是不再使用，文件不会删除，想删请手动删

  安全提醒：Cookies 等同于账号登录凭证，不要发给别人、不要传到网上。

──────────────────────────────────────────────────────────────
【文件夹结构】
  BiliDown.exe        主程序，双击运行
  _internal\\          运行库（Python、yt-dlp 等），请勿删除
  tools\\ffmpeg.exe    音视频合并用，已内置
  tools\\ffprobe.exe
  downloads\\          默认下载目录
  config.json         你的设置（首次运行后自动生成）
  使用说明.txt         本文件

──────────────────────────────────────────────────────────────
【常见问题】
  Q: 提示“所选清晰度当前不可用”？
  A: 该清晰度需要登录或大会员。导入有效 Cookies 即可。

  Q: 解析失败 / 忽然下载不了？
  A: B 站接口经常变动，通常是内置的 yt-dlp 需要升级。
     在源码目录执行一次：python build_v02.py
     它会自动升级 yt-dlp 并重新打包，把新的 成品\\v0.2 覆盖本文件夹即可。

  Q: 杀毒软件报毒？
  A: PyInstaller 打包的程序经常被杀软误报，添加信任即可。

  Q: 弹幕文件是什么？
  A: 与视频同名的 .danmaku.xml，B 站播放器、DanDanPlay 等可直接加载。

──────────────────────────────────────────────────────────────
【免责声明】
  仅供个人学习与离线观看使用，请勿传播下载内容。
"""


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def run(cmd: list[str], **kw) -> int:
    log(" ".join(str(c) for c in cmd))
    return subprocess.call(cmd, cwd=str(ROOT), **kw)


def step_update_ytdlp(skip: bool) -> None:
    """打包前把 yt-dlp 升到最新。

    B 站接口变动频繁，内置的 yt-dlp 版本直接决定程序还能不能用，
    所以默认每次打包都顺手升级一次。离线环境可用 --no-update 跳过。
    """
    if skip:
        log("跳过 yt-dlp 升级（--no-update）")
        return
    log("升级 yt-dlp 到最新版 …")
    rc = run([sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check",
              "yt-dlp"], stdout=subprocess.DEVNULL)
    if rc != 0:
        log("  ! 升级失败（可能无网络），继续使用当前已安装版本")

    probe = subprocess.run(
        [sys.executable, "-c", "import yt_dlp;print(yt_dlp.version.__version__)"],
        capture_output=True, text=True)
    if probe.returncode == 0:
        log(f"  当前 yt-dlp 版本：{probe.stdout.strip()}")


def step_icon() -> None:
    icon = ROOT / "app.ico"
    if icon.exists():
        log(f"图标已存在：{icon.name}")
        return
    log("生成图标 …")
    run([sys.executable, str(ROOT / "tools" / "make_icon.py")])


def step_pyinstaller() -> None:
    log("清理旧构建 …")
    for d in (DIST, BUILD):
        shutil.rmtree(d, ignore_errors=True)
    log("PyInstaller 打包中（首次约需 1-3 分钟）…")
    t0 = time.time()
    rc = run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        str(ROOT / "BiliDown.spec"),
    ])
    if rc != 0:
        raise SystemExit(f"PyInstaller 失败，退出码 {rc}")
    log(f"打包完成，用时 {time.time() - t0:.1f}s")


def step_assemble() -> None:
    src = DIST / "BiliDown"
    if not (src / "BiliDown.exe").exists():
        raise SystemExit(f"找不到产物：{src / 'BiliDown.exe'}")

    log(f"组装成品 → {FINAL}")
    shutil.rmtree(FINAL, ignore_errors=True)
    FINAL.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, FINAL)

    # exe 同级的 tools/：ffmpeg 与 ffprobe
    tools = FINAL / "tools"
    tools.mkdir(exist_ok=True)
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        s = ROOT / "tools" / name
        if s.exists():
            shutil.copy2(s, tools / name)
            log(f"  + tools/{name}  ({s.stat().st_size / 1e6:.0f} MB)")
        else:
            log(f"  ! 缺少 tools/{name}")

    (FINAL / "downloads").mkdir(exist_ok=True)

    ck = ROOT / "www.bilibili.com_cookies.txt"
    if ck.exists():
        shutil.copy2(ck, FINAL / ck.name)
        log(f"  + {ck.name}")

    (FINAL / "使用说明.txt").write_text(README_TXT, encoding="utf-8")
    log("  + 使用说明.txt")

    preview = ROOT / "界面预览.png"
    if preview.exists():
        shutil.copy2(preview, FINAL / preview.name)
        log(f"  + {preview.name}")


def step_report() -> None:
    total = 0
    count = 0
    for p in FINAL.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
            count += 1
    log("─" * 60)
    log(f"成品目录：{FINAL}")
    log(f"文件数  ：{count}")
    log(f"总大小  ：{total / 1024 / 1024:.1f} MB")
    log(f"主程序  ：{FINAL / 'BiliDown.exe'}")
    for name in ("BiliDown.exe", "tools/ffmpeg.exe", "tools/ffprobe.exe",
                 "使用说明.txt"):
        p = FINAL / name
        if p.exists():
            log(f"  {name:<24} {p.stat().st_size / 1e6:>8.1f} MB")
    internal = FINAL / "_internal"
    if internal.exists():
        size = sum(f.stat().st_size for f in internal.rglob("*") if f.is_file())
        n = sum(1 for f in internal.rglob("*") if f.is_file())
        log(f"  {'_internal/':<24} {size / 1e6:>8.1f} MB  ({n} 个文件)")


def main() -> int:
    log(f"项目根目录：{ROOT}")
    step_update_ytdlp("--no-update" in sys.argv)
    step_icon()
    step_pyinstaller()
    step_assemble()
    step_report()
    log("完成 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())

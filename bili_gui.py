#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BiliDown v0.2 —— B 站视频下载器（GUI）

v0.2 重点：
  1. 「最高清晰度」先探测再选择，永远等于这部视频此刻真实能拿到的最高档；
     并且明确显示是否登录 —— 未登录时 B 站最高只有 480P。
  2. UI 平滑：进度条插值动画、日志批量刷新、鼠标滚轮不再被打劫。
  3. 体验：自动解析、分P 选择、弹幕/封面/字幕、批量下载、设置持久化。
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from dataclasses import replace
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bili_core as C  # noqa: E402

APP_TITLE = f"{C.APP_NAME} v{C.APP_VERSION}"


# ══════════════════════════════════════════════════════════════════════
# 主题
# ══════════════════════════════════════════════════════════════════════
class T:
    BG        = "#0b0f18"
    BG_CARD   = "#131a2b"
    BG_INPUT  = "#192236"
    BG_HOVER  = "#223049"
    BORDER    = "#212c44"
    BORDER_F  = "#00a1d6"
    TEXT      = "#e6eaf0"
    TEXT_SEC  = "#8b95a8"
    TEXT_DIM  = "#5a6478"
    ACCENT    = "#00a1d6"
    ACCENT_H  = "#00bdf0"
    ACCENT_D  = "#0088b8"
    SUCCESS   = "#22c55e"
    ERROR     = "#ef4444"
    ERROR_H   = "#ff5c5c"
    WARN      = "#f59e0b"
    PROG_BG   = "#1a2234"

    @staticmethod
    def fonts():
        fam = set(tkfont.families())
        ui = next((f for f in ("Microsoft YaHei UI", "Segoe UI", "Microsoft YaHei")
                   if f in fam), "TkDefaultFont")
        mono = next((f for f in ("Cascadia Mono", "Cascadia Code", "Consolas",
                                 "Courier New") if f in fam), "TkFixedFont")
        return {
            "title": (ui, 17, "bold"),
            "label": (ui, 10),
            "label_b": (ui, 10, "bold"),
            "small": (ui, 9),
            "small_b": (ui, 9, "bold"),
            "input": (ui, 10),
            "btn": (ui, 10, "bold"),
            "log": (mono, 9),
        }


def make_button(parent, text, command, *, kind="ghost", font=None, width=None):
    """带悬停反馈的扁平按钮。"""
    palette = {
        "primary": (T.ACCENT, "white", T.ACCENT_H),
        "danger":  (T.ERROR, "white", T.ERROR_H),
        "ghost":   (T.BG_INPUT, T.TEXT_SEC, T.BG_HOVER),
        "subtle":  (T.BG_CARD, T.TEXT_DIM, T.BG_INPUT),
    }[kind]
    bg, fg, hover = palette
    btn = tk.Button(parent, text=text, command=command, font=font,
                    bg=bg, fg=fg, relief=tk.FLAT, bd=0,
                    activebackground=hover, activeforeground=fg,
                    cursor="hand2", highlightthickness=0)
    if width:
        btn.configure(width=width)

    def _enter(_e):
        if str(btn["state"]) != tk.DISABLED:
            btn.configure(bg=hover)

    def _leave(_e):
        if str(btn["state"]) != tk.DISABLED:
            btn.configure(bg=bg)

    btn.bind("<Enter>", _enter)
    btn.bind("<Leave>", _leave)
    btn._base_bg = bg          # 供 disabled 状态恢复用
    return btn


class Card(tk.Frame):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=T.BG_CARD,
                         highlightbackground=T.BORDER,
                         highlightthickness=1, bd=0, **kw)


# ══════════════════════════════════════════════════════════════════════
# 主程序
# ══════════════════════════════════════════════════════════════════════
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = C.load_config()
        self.F = T.fonts()

        self.ffmpeg = C.find_ffmpeg()
        self.ffprobe = C.find_ffprobe()

        # 运行时状态
        self.downloading = False
        self.probing = False
        self.cancel = C.CancelToken()
        self.tiers: list = []
        self.audio_tiers: list = []
        self.tier_map: dict = {}
        self.parts: list = []
        self.quality_key = self.cfg.get("quality", "max")
        self.login: C.LoginState = C.LoginState()
        self._probe_job = None
        self._last_probe_url = ""
        self._pending_tasks = None

        # 线程 → UI 队列
        self._logq: queue.Queue = queue.Queue()
        self._progq: queue.Queue = queue.Queue()
        self._probeq: queue.Queue = queue.Queue()
        self._doneq: queue.Queue = queue.Queue()
        self._loginq: queue.Queue = queue.Queue()

        # 进度动画
        self._prog_shown = 0.0
        self._prog_target = 0.0

        self._build_ui()
        self._tick()
        self._log_init()
        self.root.after(200, self._validate_login_async)

    # ── UI ────────────────────────────────────────────────────────────
    def _build_ui(self):
        r = self.root
        r.title(APP_TITLE)
        r.configure(bg=T.BG)
        geom = self.cfg.get("window") or "800x880"
        r.geometry(self._fit_to_screen(r, geom))
        r.minsize(660, 620)

        self._setup_style()

        main = tk.Frame(r, bg=T.BG)
        main.pack(fill=tk.BOTH, expand=True, padx=22, pady=(16, 14))

        self._build_header(main)
        self._build_url_card(main)
        self._build_options_card(main)
        self._build_cookie_card(main)
        self._build_actions(main)
        self._build_progress(main)
        self._build_log(main)

        r.protocol("WM_DELETE_WINDOW", self._on_close)
        r.bind("<Control-v>", lambda e: self._paste())
        r.bind("<Control-V>", lambda e: self._paste())

    @staticmethod
    def _fit_to_screen(root, geom: str) -> str:
        """把窗口尺寸限制在屏幕内。

        默认 880 高在 1080P、125% 缩放的笔记本上会超出屏幕，
        导致日志卡片被裁掉，所以这里统一夹一次。
        """
        import re as _re
        m = _re.match(r"^(\d+)x(\d+)", str(geom).strip() or "")
        if not m:
            return "800x880"
        try:
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
        except Exception:
            return f"{m.group(1)}x{m.group(2)}"
        w = max(640, min(int(m.group(1)), sw - 60))
        h = max(560, min(int(m.group(2)), sh - 110))
        return f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}"
    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("Q.TCombobox",
                        fieldbackground=T.BG_INPUT, background=T.BG_INPUT,
                        foreground=T.TEXT, arrowcolor=T.TEXT_SEC,
                        bordercolor=T.BORDER, lightcolor=T.BG_INPUT,
                        darkcolor=T.BG_INPUT, selectbackground=T.BG_INPUT,
                        selectforeground=T.TEXT, padding=5, relief=tk.FLAT)
        style.map("Q.TCombobox",
                  fieldbackground=[("readonly", T.BG_INPUT), ("disabled", T.BG_CARD)],
                  foreground=[("readonly", T.TEXT), ("disabled", T.TEXT_DIM)],
                  arrowcolor=[("disabled", T.TEXT_DIM)],
                  bordercolor=[("focus", T.BORDER_F)])

        style.configure("Q.TCheckbutton",
                        background=T.BG_CARD, foreground=T.TEXT_SEC,
                        focuscolor=T.BG_CARD, padding=2)
        style.map("Q.TCheckbutton",
                  background=[("active", T.BG_CARD)],
                  foreground=[("active", T.TEXT), ("selected", T.TEXT)],
                  indicatorcolor=[("selected", T.ACCENT), ("!selected", T.BG_INPUT)])

        style.configure("P.Horizontal.TProgressbar",
                        troughcolor=T.PROG_BG, background=T.ACCENT,
                        bordercolor=T.PROG_BG, lightcolor=T.ACCENT,
                        darkcolor=T.ACCENT, thickness=13)

        # ttk 样式按名字前缀继承：V.Scrollbar 会继承到没有 layout 的
        # Scrollbar 而渲染成空白，必须写成 Q.Vertical.TScrollbar。
        style.configure("Q.Vertical.TScrollbar", background=T.BG_INPUT,
                        troughcolor=T.BG_CARD, bordercolor=T.BG_CARD,
                        arrowcolor=T.TEXT_DIM, gripcount=0, relief=tk.FLAT)
        style.map("Q.Vertical.TScrollbar", background=[("active", T.BG_HOVER)])

        # Combobox 下拉列表（原生 Listbox）的配色
        self.root.option_add("*TCombobox*Listbox.background", T.BG_INPUT)
        self.root.option_add("*TCombobox*Listbox.foreground", T.TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", T.ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "white")
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)

    def _build_header(self, main):
        hdr = tk.Frame(main, bg=T.BG)
        hdr.pack(fill=tk.X, pady=(0, 12))

        tk.Label(hdr, text="BiliDown", font=self.F["title"],
                 bg=T.BG, fg=T.ACCENT).pack(side=tk.LEFT)
        tk.Label(hdr, text=f"v{C.APP_VERSION}", font=self.F["small_b"],
                 bg=T.BG, fg=T.TEXT_DIM).pack(side=tk.LEFT, padx=(6, 0), pady=(7, 0))
        tk.Label(hdr, text="B 站视频下载器", font=self.F["small"],
                 bg=T.BG, fg=T.TEXT_SEC).pack(side=tk.LEFT, padx=(10, 0), pady=(7, 0))

        dots = tk.Frame(hdr, bg=T.BG)
        dots.pack(side=tk.RIGHT, pady=(6, 0))
        self.ff_dot = self._status_dot(dots, "ffmpeg", bool(self.ffmpeg))
        self.engine_dot = self._status_dot(
            dots, "yt-dlp", C.has_builtin_ytdlp())

    def _status_dot(self, parent, name, ok):
        lbl = tk.Label(parent, text=f"● {name}", font=self.F["small"],
                       bg=T.BG, fg=T.SUCCESS if ok else T.ERROR)
        lbl.pack(side=tk.LEFT, padx=(10, 0))
        return lbl

    def _build_url_card(self, main):
        card = Card(main)
        card.pack(fill=tk.X, pady=(0, 10))
        inner = tk.Frame(card, bg=T.BG_CARD)
        inner.pack(fill=tk.X, padx=16, pady=13)

        head = tk.Frame(inner, bg=T.BG_CARD)
        head.pack(fill=tk.X)
        tk.Label(head, text="视频链接", font=self.F["label_b"],
                 bg=T.BG_CARD, fg=T.TEXT).pack(side=tk.LEFT)
        tk.Label(head, text="支持 BV / av / b23.tv / 合集 / 分P 链接",
                 font=self.F["small"], bg=T.BG_CARD, fg=T.TEXT_DIM).pack(side=tk.LEFT, padx=(10, 0))
        self.parse_hint = tk.Label(head, text="", font=self.F["small"],
                                   bg=T.BG_CARD, fg=T.TEXT_DIM)
        self.parse_hint.pack(side=tk.RIGHT)

        row = tk.Frame(inner, bg=T.BG_CARD)
        row.pack(fill=tk.X, pady=(9, 0))
        self.url_var = tk.StringVar()
        self.url_entry = tk.Entry(
            row, textvariable=self.url_var, font=self.F["input"],
            bg=T.BG_INPUT, fg=T.TEXT, insertbackground=T.TEXT,
            relief=tk.FLAT, highlightthickness=1, bd=0,
            highlightcolor=T.BORDER_F, highlightbackground=T.BORDER,
            disabledbackground=T.BG_CARD)
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=8)
        self.url_entry.bind("<Return>", lambda e: self._start_download())
        self.url_var.trace_add("write", self._on_url_changed)

        btn_paste = make_button(row, "粘贴", self._paste, font=self.F["small"])
        btn_paste.pack(side=tk.LEFT, padx=(8, 0), ipady=5, ipadx=10)

        self.btn_parse = make_button(row, "解析清晰度", self._probe_now,
                                     kind="ghost", font=self.F["small"])
        self.btn_parse.pack(side=tk.LEFT, padx=(6, 0), ipady=5, ipadx=10)

        # 视频信息面板
        self.info_frame = tk.Frame(inner, bg=T.BG_INPUT)
        self.info_frame.pack(fill=tk.X, pady=(9, 0))
        self.info_title = tk.Label(
            self.info_frame, text="", font=self.F["label_b"], bg=T.BG_INPUT,
            fg=T.TEXT, anchor="w", justify=tk.LEFT, wraplength=640)
        self.info_title.pack(fill=tk.X, padx=10, pady=(7, 0))
        self.info_meta = tk.Label(
            self.info_frame, text="", font=self.F["small"], bg=T.BG_INPUT,
            fg=T.TEXT_SEC, anchor="w", justify=tk.LEFT, wraplength=640)
        self.info_meta.pack(fill=tk.X, padx=10, pady=(2, 7))
        self.info_frame.pack_forget()

    def _build_options_card(self, main):
        card = Card(main)
        card.pack(fill=tk.X, pady=(0, 10))
        inner = tk.Frame(card, bg=T.BG_CARD)
        inner.pack(fill=tk.X, padx=16, pady=13)

        # 第 1 行：清晰度 + 分P
        r1 = tk.Frame(inner, bg=T.BG_CARD)
        r1.pack(fill=tk.X)
        r1.columnconfigure(1, weight=3)
        r1.columnconfigure(4, weight=1)

        tk.Label(r1, text="清晰度", font=self.F["label"],
                 bg=T.BG_CARD, fg=T.TEXT).grid(row=0, column=0, sticky=tk.W)

        self.quality_var = tk.StringVar()
        self.quality_combo = ttk.Combobox(
            r1, textvariable=self.quality_var, state="readonly",
            font=self.F["input"], style="Q.TCombobox", values=["最高清晰度"])
        self.quality_combo.grid(row=0, column=1, sticky=tk.EW, padx=(10, 0))
        self.quality_combo.bind("<<ComboboxSelected>>", self._on_quality)

        self.badge = tk.Label(r1, text="MAX", font=self.F["small_b"],
                              bg=T.ACCENT, fg="white", padx=7, pady=1)
        self.badge.grid(row=0, column=2, padx=(8, 14))

        self.parts_label = tk.Label(r1, text="分P", font=self.F["label"],
                                    bg=T.BG_CARD, fg=T.TEXT)
        self.parts_var = tk.StringVar(value="全部")
        self.parts_combo = ttk.Combobox(
            r1, textvariable=self.parts_var, state="readonly",
            font=self.F["input"], style="Q.TCombobox", values=["全部"])
        # 初始隐藏，探测到多分P 才显示

        # 第 2 行：格式 + 保存目录
        r2 = tk.Frame(inner, bg=T.BG_CARD)
        r2.pack(fill=tk.X, pady=(11, 0))
        r2.columnconfigure(1, weight=0)
        r2.columnconfigure(3, weight=1)

        tk.Label(r2, text="输出格式", font=self.F["label"],
                 bg=T.BG_CARD, fg=T.TEXT).grid(row=0, column=0, sticky=tk.W)
        self.format_var = tk.StringVar(value=self.cfg.get("container", "mp4"))
        self.format_combo = ttk.Combobox(
            r2, textvariable=self.format_var, state="readonly",
            font=self.F["input"], style="Q.TCombobox", width=14,
            values=["mp4", "mkv", "webm", "m4a（仅音频）", "mp3（仅音频）"])
        self.format_combo.grid(row=0, column=1, sticky=tk.W, padx=(10, 18))
        self.format_combo.bind("<<ComboboxSelected>>", lambda e: None)

        tk.Label(r2, text="保存到", font=self.F["label"],
                 bg=T.BG_CARD, fg=T.TEXT).grid(row=0, column=2, sticky=tk.W)
        dirbox = tk.Frame(r2, bg=T.BG_CARD)
        dirbox.grid(row=0, column=3, sticky=tk.EW, padx=(10, 0))
        self.dir_var = tk.StringVar(value=self.cfg.get("outdir") or str(C.downloads_dir()))
        tk.Entry(dirbox, textvariable=self.dir_var, font=self.F["input"],
                 bg=T.BG_INPUT, fg=T.TEXT, insertbackground=T.TEXT,
                 relief=tk.FLAT, highlightthickness=1, bd=0,
                 highlightcolor=T.BORDER_F,
                 highlightbackground=T.BORDER).pack(side=tk.LEFT, fill=tk.X,
                                                    expand=True, ipady=6)
        make_button(dirbox, "浏览", self._browse_dir, font=self.F["small"]
                    ).pack(side=tk.LEFT, padx=(6, 0), ipady=3, ipadx=8)
        make_button(dirbox, "打开", self._open_dir, font=self.F["small"]
                    ).pack(side=tk.LEFT, padx=(4, 0), ipady=3, ipadx=8)

        # 第 3 行：附加内容
        r3 = tk.Frame(inner, bg=T.BG_CARD)
        r3.pack(fill=tk.X, pady=(11, 0))
        tk.Label(r3, text="同时下载", font=self.F["label"],
                 bg=T.BG_CARD, fg=T.TEXT).pack(side=tk.LEFT)

        self.danmaku_var = tk.BooleanVar(value=bool(self.cfg.get("write_danmaku", True)))
        self.thumb_var = tk.BooleanVar(value=bool(self.cfg.get("write_thumbnail", False)))
        self.subs_var = tk.BooleanVar(value=bool(self.cfg.get("write_subs", False)))
        self.open_var = tk.BooleanVar(value=bool(self.cfg.get("open_when_done", False)))
        for text, var in (("弹幕", self.danmaku_var), ("封面", self.thumb_var),
                          ("字幕", self.subs_var)):
            ttk.Checkbutton(r3, text=text, variable=var, style="Q.TCheckbutton"
                            ).pack(side=tk.LEFT, padx=(14, 0))

        ttk.Checkbutton(r3, text="完成后打开文件夹", variable=self.open_var,
                        style="Q.TCheckbutton").pack(side=tk.RIGHT)

    def _build_cookie_card(self, main):
        card = Card(main)
        card.pack(fill=tk.X, pady=(0, 10))
        inner = tk.Frame(card, bg=T.BG_CARD)
        inner.pack(fill=tk.X, padx=16, pady=11)

        tk.Label(inner, text="登录状态", font=self.F["label"],
                 bg=T.BG_CARD, fg=T.TEXT).pack(side=tk.LEFT)
        self.cookie_var = tk.StringVar(value="检测中…")
        self.cookie_lbl = tk.Label(inner, textvariable=self.cookie_var,
                                   font=self.F["small"], bg=T.BG_CARD, fg=T.TEXT_SEC)
        self.cookie_lbl.pack(side=tk.LEFT, padx=(10, 0))

        make_button(inner, "导入 Cookies", self._import_cookies, kind="primary",
                    font=self.F["small"]).pack(side=tk.RIGHT, ipady=4, ipadx=10)
        make_button(inner, "重新检测", self._validate_login_async, font=self.F["small"]
                    ).pack(side=tk.RIGHT, padx=(0, 6), ipady=4, ipadx=8)
        make_button(inner, "清除", self._clear_cookies, font=self.F["small"]
                    ).pack(side=tk.RIGHT, padx=(0, 6), ipady=4, ipadx=8)

    def _build_actions(self, main):
        bar = tk.Frame(main, bg=T.BG)
        bar.pack(fill=tk.X, pady=(2, 10))

        self.dl_btn = make_button(bar, "⬇  开始下载", self._start_download,
                                  kind="primary", font=self.F["btn"])
        self.dl_btn.pack(side=tk.LEFT, ipady=9, ipadx=26)

        self.stop_btn = make_button(bar, "停止", self._stop, kind="danger",
                                    font=self.F["btn"])
        self.stop_btn.pack(side=tk.LEFT, padx=(10, 0), ipady=9, ipadx=16)
        self.stop_btn.configure(state=tk.DISABLED, bg=T.BG_CARD, fg=T.TEXT_DIM)

        make_button(bar, "批量下载", self._open_batch, font=self.F["small"]
                    ).pack(side=tk.LEFT, padx=(10, 0), ipady=6, ipadx=12)
        make_button(bar, "设置", self._open_settings, font=self.F["small"]
                    ).pack(side=tk.RIGHT, ipady=6, ipadx=12)

    def _build_progress(self, main):
        wrap = tk.Frame(main, bg=T.BG)
        wrap.pack(fill=tk.X, pady=(0, 10))

        self.progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(wrap, variable=self.progress_var, maximum=100,
                        style="P.Horizontal.TProgressbar").pack(fill=tk.X)

        row = tk.Frame(wrap, bg=T.BG)
        row.pack(fill=tk.X, pady=(5, 0))
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(row, textvariable=self.status_var, font=self.F["small"],
                 bg=T.BG, fg=T.TEXT_SEC, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.rate_var = tk.StringVar(value="")
        tk.Label(row, textvariable=self.rate_var, font=self.F["small"],
                 bg=T.BG, fg=T.ACCENT).pack(side=tk.RIGHT)

    def _build_log(self, main):
        card = Card(main)
        card.pack(fill=tk.BOTH, expand=True)
        head = tk.Frame(card, bg=T.BG_CARD)
        head.pack(fill=tk.X, padx=14, pady=(9, 0))
        tk.Label(head, text="日志", font=self.F["label_b"],
                 bg=T.BG_CARD, fg=T.TEXT_SEC).pack(side=tk.LEFT)
        make_button(head, "清空", self._clear_log, kind="subtle",
                    font=self.F["small"]).pack(side=tk.RIGHT, ipady=1, ipadx=8)

        body = tk.Frame(card, bg=T.BG_CARD)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 10))

        self.log = tk.Text(body, font=self.F["log"], bg="#0d1220", fg="#c9d1d9",
                           insertbackground=T.TEXT, relief=tk.FLAT, bd=0,
                           state=tk.DISABLED, wrap=tk.WORD, height=6,
                           padx=10, pady=6, highlightthickness=0)
        sb = ttk.Scrollbar(body, orient=tk.VERTICAL, style="Q.Vertical.TScrollbar",
                           command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        for tag, color in (("ok", T.SUCCESS), ("err", T.ERROR), ("warn", T.WARN),
                           ("accent", T.ACCENT), ("dim", T.TEXT_DIM)):
            self.log.tag_configure(tag, foreground=color)

    # ── 队列驱动的 UI 刷新（避免逐行 after 造成的卡顿）─────────────────
    def _tick(self):
        # 1. 日志
        lines = []
        try:
            while len(lines) < 300:
                lines.append(self._logq.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.log.configure(state=tk.NORMAL)
            for msg, tag in lines:
                self.log.insert(tk.END, msg + "\n", tag or ())
            # 控制日志长度，避免长时间运行后变卡
            total = int(self.log.index("end-1c").split(".")[0])
            if total > 1200:
                self.log.delete("1.0", f"{total - 900}.0")
            self.log.see(tk.END)
            self.log.configure(state=tk.DISABLED)

        # 2. 进度（只取最新一帧）
        latest = None
        try:
            while True:
                latest = self._progq.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            self._apply_progress(latest)

        # 3. 探测结果
        try:
            while True:
                info = self._probeq.get_nowait()
                self._apply_probe(info)
        except queue.Empty:
            pass

        # 4. 登录状态（同样由队列驱动，避免跨线程调 Tk）
        try:
            while True:
                self._apply_login(self._loginq.get_nowait())
        except queue.Empty:
            pass

        # 5. 结束
        try:
            while True:
                self._doneq.get_nowait()
                self._finish_download()
        except queue.Empty:
            pass

        # 5. 进度条插值动画
        delta = self._prog_target - self._prog_shown
        if abs(delta) < 0.08:
            self._prog_shown = self._prog_target
        else:
            self._prog_shown += delta * 0.22
        self.progress_var.set(self._prog_shown)

        self.root.after(33, self._tick)

    def _post_log(self, msg, tag=None):
        self._logq.put((msg, tag))

    def _log(self, msg, tag=None):
        """主线程直接写日志。"""
        self._logq.put((msg, tag))

    def _clear_log(self):
        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.configure(state=tk.DISABLED)

    def _apply_progress(self, p: C.TaskProgress):
        self._prog_target = max(0.0, min(100.0, float(p.percent)))
        stage = p.stage or "下载中"
        prefix = f"[{p.index}/{p.count}] " if p.count > 1 else ""
        title = (p.title or "")[:48]
        bits = [f"{prefix}{stage}"]
        if title:
            bits.append(title)
        if p.total:
            bits.append(f"{C.human_size(p.downloaded)} / {C.human_size(p.total)}")
        self.status_var.set("  ·  ".join(bits))

        rate = []
        if p.speed:
            rate.append(C.human_speed(p.speed))
        if p.eta:
            rate.append("剩余 " + C.human_eta(p.eta))
        self.rate_var.set("   ".join(rate))

    # ── 启动日志 ──────────────────────────────────────────────────────
    def _log_init(self):
        self._log(f"{APP_TITLE} 已启动", "accent")
        self._log(f"yt-dlp 引擎：{C._engine_name()}", "ok" if C.has_builtin_ytdlp() else "err")
        if self.ffmpeg:
            self._log(f"ffmpeg：{self.ffmpeg}", "ok")
        else:
            self._log("ffmpeg：未找到（无法合并音视频）", "err")
        if not self.ffprobe:
            self._log("ffprobe：未找到（部分后处理会失败）", "warn")
        self._log(f"保存目录：{self.dir_var.get()}", "dim")

    # ── 登录状态 ──────────────────────────────────────────────────────
    def _validate_login_async(self):
        ck = self.cfg.get("cookies") or ""
        proxy = self.cfg.get("proxy") or ""
        self.cookie_var.set("检测中…")
        self.cookie_lbl.configure(fg=T.TEXT_DIM)

        def work():
            st = C.check_login(ck, proxy)
            self._loginq.put(st)

        threading.Thread(target=work, daemon=True).start()

    def _apply_login(self, st: C.LoginState):
        self.login = st
        self.cookie_var.set(st.badge if st.checked else st.message)
        color = T.SUCCESS if st.logged_in else (T.WARN if st.checked else T.TEXT_DIM)
        self.cookie_lbl.configure(fg=color)
        if not st.logged_in:
            self._log("⚠ " + st.message, "warn")
            self._log("  未登录时 B 站最高只给 480P；“最高清晰度”会如实显示实际可用档位。", "dim")
        else:
            self._log(f"登录状态：{st.message}", "ok")

    def _import_cookies(self):
        path = filedialog.askopenfilename(
            title="选择 B 站 Cookies 文件（Netscape 格式的 .txt）",
            initialdir=str(C.app_dir()),
            filetypes=[("Cookies 文本", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        src = Path(path)
        dest = C.app_dir() / "www.bilibili.com_cookies.txt"
        try:
            dest.write_bytes(src.read_bytes())
        except OSError as exc:
            # 目录不可写时退回 APPDATA
            fallback = Path(os.environ.get("APPDATA", os.path.expanduser("~"))) / C.APP_NAME
            fallback.mkdir(parents=True, exist_ok=True)
            dest = fallback / "www.bilibili.com_cookies.txt"
            try:
                dest.write_bytes(src.read_bytes())
            except OSError:
                messagebox.showerror("导入失败", f"无法写入 Cookies：{exc}")
                return
        self.cfg["cookies"] = str(dest)
        C.save_config(self.cfg)
        self._log(f"Cookies 已导入：{src.name} → {dest}", "ok")

        # 手动从 F12 复制的 Cookie 头不是 Netscape 格式，这里说清楚，
        # 否则用户会以为导入失败了
        fmt = C.cookie_file_format(str(dest))
        if fmt == "raw":
            pairs = C.parse_cookie_text(dest.read_text(encoding="utf-8", errors="ignore"))
            self._log(f"识别为手动复制的 Cookie 头（{len(pairs)} 项），已自动转换供 yt-dlp 使用", "warn")
            if "SESSDATA" not in pairs:
                self._log("⚠ 里面没有 SESSDATA —— 说明复制到的不是已登录的 B 站请求，请重新复制", "err")
        elif fmt == "netscape":
            self._log("识别为 Netscape 格式 cookies 文件", "dim")
        elif fmt == "unknown":
            self._log("⚠ 没从文件里解析出任何 cookie，请检查复制内容是否完整", "err")

        self._validate_login_async()

    def _clear_cookies(self):
        self.cfg["cookies"] = ""
        C.save_config(self.cfg)
        self.cookie_var.set("未导入（最高 480P）")
        self.cookie_lbl.configure(fg=T.TEXT_DIM)
        self.login = C.LoginState(checked=True)
        self._log("已停用 Cookies（文件未删除，可重新导入）", "warn")

    # ── URL / 探测 ────────────────────────────────────────────────────
    def _paste(self):
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        self.url_var.set(text)

    def _on_url_changed(self, *_a):
        if self._probe_job:
            try:
                self.root.after_cancel(self._probe_job)
            except Exception:
                pass
        url = self.url_var.get().strip()
        if not C.is_bilibili_url(url):
            self.parse_hint.configure(text="")
            return
        self.parse_hint.configure(text="等待解析…", fg=T.TEXT_DIM)
        self._probe_job = self.root.after(650, self._probe_now)

    def _probe_now(self):
        self._probe_job = None
        url = C.normalize_url(self.url_var.get())
        if not url:
            return
        if not C.is_bilibili_url(url):
            self.parse_hint.configure(text="不是 B 站链接", fg=T.WARN)
            return
        if self.probing:
            return
        self.probing = True
        self.parse_hint.configure(text="解析中…", fg=T.ACCENT)

        ck = self.cfg.get("cookies") or ""
        proxy = self.cfg.get("proxy") or ""

        def work():
            try:
                info = C.probe(url, cookies=ck or None, proxy=proxy,
                               ffmpeg_dir=C.find_ffmpeg_dir())
            except Exception as exc:
                info = C.VideoInfo(url=url, error=str(exc))
            self._probeq.put(info)

        threading.Thread(target=work, daemon=True).start()

    def _apply_probe(self, info: C.VideoInfo):
        self.probing = False
        if not info.ok:
            self.parse_hint.configure(text="解析失败", fg=T.ERROR)
            self._log(f"解析失败：{info.error or '未知错误'}", "err")
            self.info_frame.pack_forget()
            return

        self._last_probe_url = info.url
        self.parts = info.parts
        self.parse_hint.configure(text="解析完成", fg=T.SUCCESS)

        self.info_title.configure(text=info.title or "(无标题)")
        self.info_meta.configure(text=info.summary())
        self.info_frame.pack(fill=tk.X, pady=(9, 0))

        self._apply_tiers(info.tiers, info.audio_tiers)
        self._apply_parts(info)
        self._log(f"解析成功：{info.title}", "ok")
        self._log(f"最高可用清晰度：{info.max_label}", "accent")
        if info.tiers and info.tiers[0].height <= 480 and not self.login.logged_in:
            self._log("⚠ 当前最高只有 480P，因为未登录；导入有效 Cookies 后可解锁 1080P / 4K / 8K", "warn")

    def _apply_tiers(self, tiers, audio_tiers):
        self.tiers = tiers or []
        self.audio_tiers = audio_tiers or []
        labels, mapping = [], {}

        if self.tiers:
            top = self.tiers[0]
            top_entry = replace(top, key="max", label=f"最高清晰度 · {top.label}")
            labels.append(top_entry.label)
            mapping[top_entry.label] = top_entry
            for t in self.tiers:
                if t.label not in mapping:
                    labels.append(t.label)
                    mapping[t.label] = t
        else:
            for t in C.fallback_tiers():
                lbl = "最高清晰度" if t.key == "max" else t.label
                labels.append(lbl)
                mapping[lbl] = t

        self.tier_map = mapping
        self.quality_combo.configure(values=labels)

        # 尽量保持用户原来的选择
        target = None
        for lbl, t in mapping.items():
            if t.key == self.quality_key:
                target = lbl
                break
        if target is None:
            target = labels[0]
        self.quality_var.set(target)
        self._on_quality()

    def _apply_parts(self, info: C.VideoInfo):
        if info.is_playlist and len(info.parts) > 1:
            values = ["全部"] + [f"P{p.index}  {p.title[:52]}" for p in info.parts]
            self.parts_combo.configure(values=values)
            self.parts_var.set("全部")
            self.parts_label.grid(row=0, column=3, sticky=tk.W)
            self.parts_combo.grid(row=0, column=4, sticky=tk.EW, padx=(10, 0))
        else:
            self.parts_label.grid_remove()
            self.parts_combo.grid_remove()
            self.parts_var.set("全部")

    def _on_quality(self, _e=None):
        label = self.quality_var.get()
        tier = self.tier_map.get(label)
        short, color = "—", T.BG_HOVER
        if tier is None:
            short = "MAX"
        elif tier.key == "max" and self.tiers:
            short = self.tiers[0].short
            color = T.ACCENT
        elif tier.height >= 2160:
            short, color = tier.short, "#a855f7"
        elif tier.height >= 1080:
            short, color = tier.short, T.ACCENT
        elif tier.height >= 720:
            short, color = tier.short, "#2563eb"
        elif tier.height > 0:
            short, color = tier.short, T.WARN
        else:
            short, color = "音频", T.SUCCESS
        self.badge.configure(text=short, bg=color)
        if tier is not None:
            self.quality_key = tier.key

    # ── 下载 ──────────────────────────────────────────────────────────
    def _current_plan(self) -> C.DownloadPlan:
        label = self.quality_var.get()
        tier = self.tier_map.get(label)
        selector = tier.selector if tier else "bestvideo+bestaudio/best"
        quality_label = label or "最高清晰度"

        fmt = self.format_var.get()
        audio_only = fmt in ("m4a（仅音频）", "mp3（仅音频）")
        container = "mp4" if audio_only else fmt
        audio_format = {"m4a（仅音频）": "m4a", "mp3（仅音频）": "mp3"}.get(fmt, "m4a")

        parts_sel = self.parts_var.get()
        playlist_items = ""
        if self.parts and parts_sel.startswith("P"):
            try:
                idx = int(parts_sel.split(" ", 1)[0][1:])
                playlist_items = str(idx)
            except Exception:
                playlist_items = ""

        return C.DownloadPlan(
            outdir=self.dir_var.get().strip() or str(C.downloads_dir()),
            selector=selector,
            container=container,
            quality_label=quality_label,
            filename_template=self.cfg.get("filename_template") or "%(title).120B.%(ext)s",
            cookies=self.cfg.get("cookies") or "",
            proxy=self.cfg.get("proxy") or "",
            write_danmaku=bool(self.danmaku_var.get()),
            write_thumbnail=bool(self.thumb_var.get()),
            write_subs=bool(self.subs_var.get()),
            audio_only=audio_only,
            audio_format=audio_format,
            playlist_all=(parts_sel == "全部"),
            playlist_items=playlist_items,
        )

    def _start_download(self, tasks=None):
        if self.downloading:
            return
        if not tasks:
            url = C.normalize_url(self.url_var.get())
            if not url:
                messagebox.showwarning("提示", "请先输入视频链接")
                return
            if not C.is_bilibili_url(url):
                if not messagebox.askyesno("确认", "这似乎不是 B 站链接，仍要尝试下载吗？"):
                    return
            tasks = [(url, self._current_plan())]

        if not C.has_builtin_ytdlp():
            messagebox.showerror("错误", "未找到可用的 yt-dlp 引擎")
            return

        self._save_cfg_quiet()
        self.downloading = True
        self.cancel.reset()
        self._prog_target = 0.0
        self._prog_shown = 0.0
        self.progress_var.set(0)
        self.rate_var.set("")
        self.status_var.set("准备下载…")
        self.dl_btn.configure(state=tk.DISABLED, bg=T.BG_CARD, fg=T.TEXT_DIM)
        self.stop_btn.configure(state=tk.NORMAL, bg=T.ERROR, fg="white")
        self.url_entry.configure(state=tk.DISABLED)

        threading.Thread(target=self._run_tasks, args=(tasks,), daemon=True).start()

    def _run_tasks(self, tasks):
        log = lambda m, t=None: self._post_log(m, t)          # noqa: E731
        prog = lambda p: self._progq.put(p)                    # noqa: E731
        ok_count = 0
        try:
            total = len(tasks)
            for i, (url, plan) in enumerate(tasks, 1):
                if self.cancel.cancelled:
                    break
                log(f"{'─' * 46}", "dim")
                log(f"({i}/{total}) 开始下载 {url}", "accent")
                result = C.download(url, plan, on_log=log, on_progress=prog,
                                    cancel=self.cancel, index=i, count=total)
                if result.cancelled:
                    log("任务已取消", "warn")
                    break
                if result.ok:
                    ok_count += 1
                    for f in result.files:
                        log(f"✅ 完成：{Path(f).name}", "ok")
                    if not result.files:
                        log("✅ 下载完成（未取得文件名，请查看保存目录）", "ok")
                else:
                    log(f"❌ 失败：{result.error}", "err")
        except Exception:
            log("内部错误：\n" + traceback.format_exc(), "err")
        finally:
            self._doneq.put((ok_count, len(tasks)))

    def _finish_download(self):
        self.downloading = False
        self.dl_btn.configure(state=tk.NORMAL, bg=T.ACCENT, fg="white")
        self.stop_btn.configure(state=tk.DISABLED, bg=T.BG_CARD, fg=T.TEXT_DIM)
        self.url_entry.configure(state=tk.NORMAL)
        self.status_var.set("就绪")
        self.rate_var.set("")
        self._prog_target = 100.0 if self._prog_shown > 1 else 0.0
        if self.open_var.get():
            self._open_dir()

    def _stop(self):
        if self.downloading:
            self.cancel.cancel()
            self._log("正在停止…（当前分片结束后生效）", "warn")
            self.status_var.set("正在停止…")

    # ── 杂项 ──────────────────────────────────────────────────────────
    def _browse_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or str(C.app_dir()))
        if d:
            self.dir_var.set(d)

    def _open_dir(self):
        d = self.dir_var.get().strip()
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if not C.open_folder(d):
            messagebox.showinfo("提示", f"无法自动打开，请手动前往：\n{d}")

    def _save_cfg_quiet(self):
        self.cfg.update({
            "outdir": self.dir_var.get().strip(),
            "quality": self.quality_key,
            "container": self.format_var.get(),
            "write_danmaku": bool(self.danmaku_var.get()),
            "write_thumbnail": bool(self.thumb_var.get()),
            "write_subs": bool(self.subs_var.get()),
            "open_when_done": bool(self.open_var.get()),
        })
        C.save_config(self.cfg)

    def _open_settings(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("设置")
        dlg.configure(bg=T.BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)

        body = tk.Frame(dlg, bg=T.BG)
        body.pack(padx=20, pady=16, fill=tk.BOTH, expand=True)

        vars_ = {
            "filename_template": tk.StringVar(value=self.cfg.get("filename_template", "")),
            "proxy": tk.StringVar(value=self.cfg.get("proxy", "")),
            "concurrent_fragments": tk.StringVar(
                value=str(self.cfg.get("concurrent_fragments", 4))),
        }

        def field(row, label, var, hint=""):
            tk.Label(body, text=label, font=self.F["label"], bg=T.BG, fg=T.TEXT
                     ).grid(row=row, column=0, sticky=tk.W, pady=(0, 3))
            tk.Entry(body, textvariable=var, font=self.F["input"], width=46,
                     bg=T.BG_INPUT, fg=T.TEXT, insertbackground=T.TEXT,
                     relief=tk.FLAT, highlightthickness=1, bd=0,
                     highlightcolor=T.BORDER_F, highlightbackground=T.BORDER
                     ).grid(row=row + 1, column=0, sticky=tk.EW, ipady=5, pady=(0, 2))
            if hint:
                tk.Label(body, text=hint, font=self.F["small"], bg=T.BG,
                         fg=T.TEXT_DIM, justify=tk.LEFT, wraplength=430
                         ).grid(row=row + 2, column=0, sticky=tk.W, pady=(0, 10))

        field(0, "文件名模板", vars_["filename_template"],
              "可用变量：%(title)s  %(id)s  %(uploader)s  %(upload_date)s  %(ext)s")
        field(3, "代理（可选）", vars_["proxy"], "例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080")
        field(6, "并发分片数", vars_["concurrent_fragments"], "默认 4，网络不稳定时调小到 1-2")

        btns = tk.Frame(body, bg=T.BG)
        btns.grid(row=9, column=0, sticky=tk.E, pady=(4, 0))

        def save():
            self.cfg["filename_template"] = vars_["filename_template"].get().strip() or "%(title).120B.%(ext)s"
            self.cfg["proxy"] = vars_["proxy"].get().strip()
            try:
                self.cfg["concurrent_fragments"] = max(1, min(16, int(vars_["concurrent_fragments"].get())))
            except ValueError:
                self.cfg["concurrent_fragments"] = 4
            C.save_config(self.cfg)
            self._log("设置已保存", "ok")
            dlg.destroy()

        make_button(btns, "保存", save, kind="primary", font=self.F["btn"]
                    ).pack(side=tk.RIGHT, ipady=5, ipadx=18)
        make_button(btns, "取消", dlg.destroy, font=self.F["label"]
                    ).pack(side=tk.RIGHT, padx=(0, 8), ipady=5, ipadx=14)
        dlg.grab_set()

    def _open_batch(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("批量下载")
        dlg.configure(bg=T.BG)
        dlg.transient(self.root)
        dlg.geometry("560x430")

        tk.Label(dlg, text="每行一个链接（BV / av / b23.tv / 合集）", font=self.F["label"],
                 bg=T.BG, fg=T.TEXT).pack(anchor=tk.W, padx=18, pady=(14, 6))
        tk.Label(dlg, text="批量下载时将使用主界面当前的清晰度与格式设置。",
                 font=self.F["small"], bg=T.BG, fg=T.TEXT_DIM).pack(anchor=tk.W, padx=18)

        txt = tk.Text(dlg, font=self.F["log"], bg=T.BG_INPUT, fg=T.TEXT,
                      insertbackground=T.TEXT, relief=tk.FLAT, bd=0, height=13,
                      padx=8, pady=6, highlightthickness=1,
                      highlightbackground=T.BORDER, highlightcolor=T.BORDER_F)
        txt.pack(fill=tk.BOTH, expand=True, padx=18, pady=(8, 10))

        bar = tk.Frame(dlg, bg=T.BG)
        bar.pack(fill=tk.X, padx=18, pady=(0, 14))

        def go():
            urls = [u.strip() for u in txt.get("1.0", tk.END).splitlines()]
            urls = [C.normalize_url(u) for u in urls if C.is_bilibili_url(u)]
            if not urls:
                messagebox.showwarning("提示", "没有识别到有效的 B 站链接", parent=dlg)
                return
            dlg.destroy()
            plan = self._current_plan()
            self._start_download([(u, replace(plan)) for u in urls])

        make_button(bar, "开始下载", go, kind="primary", font=self.F["btn"]
                    ).pack(side=tk.RIGHT, ipady=6, ipadx=20)
        make_button(bar, "取消", dlg.destroy, font=self.F["label"]
                    ).pack(side=tk.RIGHT, padx=(0, 8), ipady=6, ipadx=14)
        dlg.grab_set()

    def _on_close(self):
        if self.downloading:
            if not messagebox.askyesno("确认", "正在下载中，确定要退出吗？"):
                return
            self.cancel.cancel()
        try:
            self.cfg["window"] = self.root.geometry()
            self.cfg["outdir"] = self.dir_var.get().strip()
            C.save_config(self.cfg)
        except Exception:
            pass
        self.root.destroy()


# ══════════════════════════════════════════════════════════════════════
def main():
    # 必须在创建 Tk 之前声明 DPI 感知，否则界面会被系统二次缩放而发虚
    if os.name == "nt":
        try:
            from ctypes import windll
            try:
                windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    root = tk.Tk()
    # 窗口图标
    for name in ("app.ico", "BiliDown.ico"):
        icon = C.resource_dir() / name
        if icon.exists():
            try:
                root.iconbitmap(str(icon))
                break
            except Exception:
                pass

    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

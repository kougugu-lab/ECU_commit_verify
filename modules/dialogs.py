#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dialogs.py - 詳細設定ダイアログ (SettingsDialog) & ラズパイ本体日時設定 (SystemDateTimeDialog)
common_ui_requirements.md および inspection_app 準拠。
整然としたカード型レイアウト・スクロール可能パネル・整列されたスピンボックス。
"""

import os
import sys
import json
import time
import glob
import shutil
import threading
import subprocess
from datetime import datetime
from typing import Dict, Any, Optional, List

import cv2
import numpy as np
import tkinter as tk
from tkinter import messagebox, ttk, filedialog
from PIL import Image, ImageTk

try:
    import pygame
    if not pygame.mixer.get_init():
        pygame.mixer.init()
    PYGAME_AVAILABLE = True
except Exception:
    PYGAME_AVAILABLE = False


from .constants import (
    COLOR_BG_MAIN, COLOR_BG_PANEL, COLOR_BG_INPUT,
    COLOR_TEXT_MAIN, COLOR_TEXT_SUB, COLOR_ACCENT,
    COLOR_OK, COLOR_NG, COLOR_WARNING, COLOR_BORDER,
    FONT_FAMILY, FONT_NORMAL, FONT_BOLD, FONT_LARGE,
    FONT_SET_TAB, FONT_SET_LBL, FONT_SET_VAL, FONT_BTN_LARGE,
    RES_OPTIONS, RES_OPTIONS_PREVIEW,
    COLOR_STATE_GREEN, COLOR_STATE_YELLOW, COLOR_STATE_RED, COLOR_STATE_OTHER,
    VERSION
)
from .widgets import create_card, Tooltip, configure_modal_toplevel
from .ocr_engine import setup_tesseract_path


def resolve_camera_index_by_path(by_path: str, fallback_idx: int = 0) -> int:
    """Linuxの /dev/v4l/by-path/ シンボリックリンクから現在の /dev/videoN のインデックス番号を逆引き解決"""
    if not by_path or not sys.platform.startswith("linux"):
        return fallback_idx
    try:
        if os.path.islink(by_path) or os.path.exists(by_path):
            real_path = os.path.realpath(by_path)
            base = os.path.basename(real_path)
            if base.startswith("video") and base[5:].isdigit():
                return int(base[5:])
    except Exception:
        pass
    return fallback_idx


def get_available_camera_list() -> List[Dict[str, Any]]:
    """OSが認識しているカメラデバイス一覧を安全・軽量に取得する (by-path対応)"""
    devices = []
    by_path_map = {}

    if sys.platform.startswith("linux"):
        by_path_dir = "/dev/v4l/by-path"
        if os.path.exists(by_path_dir):
            try:
                for entry in sorted(os.listdir(by_path_dir)):
                    full_p = os.path.join(by_path_dir, entry)
                    try:
                        target = os.path.realpath(full_p)
                        target_base = os.path.basename(target)
                        if target_base.startswith("video") and target_base[5:].isdigit():
                            target_idx = int(target_base[5:])
                            if target_idx not in by_path_map:
                                by_path_map[target_idx] = full_p
                    except Exception:
                        pass
            except Exception:
                pass

        v4l_paths = sorted(glob.glob("/sys/class/video4linux/video*"))
        ignore_keywords = ["codec", "rpivid", "vc4", "media-controller", "bcm2835-isp"]
        for p in v4l_paths:
            try:
                base = os.path.basename(p)
                idx_str = base.replace("video", "")
                if not idx_str.isdigit():
                    continue
                idx = int(idx_str)
                name_file = os.path.join(p, "name")
                name = ""
                if os.path.isfile(name_file):
                    with open(name_file, "r", encoding="utf-8", errors="ignore") as f:
                        name = f.read().strip()
                if any(kw in name.lower() for kw in ignore_keywords):
                    continue
                bp = by_path_map.get(idx, "")
                devices.append({"index": idx, "label": f"[{idx}] {name or 'USB Camera'}", "by_path": bp})
            except Exception:
                pass
    else:
        # Windows: 通常は0〜3まで軽量チェック
        for i in range(4):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW if sys.platform.startswith("win") else cv2.CAP_ANY)
            if cap.isOpened():
                devices.append({"index": i, "label": f"[{i}] カメラデバイス {i}", "by_path": ""})
                cap.release()

    if not devices:
        devices = [
            {"index": 0, "label": "[0] カメラ 0", "by_path": ""},
            {"index": 1, "label": "[1] カメラ 1", "by_path": ""}
        ]
    return devices


class SystemDateTimeDialog(tk.Toplevel):
    """ラズパイ本体日時設定ダイアログ (オフライン現場対応)"""

    def __init__(self, parent):
        super().__init__(parent)
        self.title("本体日時設定")
        self.geometry("560x490")
        self.configure(bg=COLOR_BG_MAIN)
        self.transient(parent)
        self.grab_set()
        configure_modal_toplevel(self)

        now = datetime.now()

        # 最下部ボタンエリア (side=BOTTOM 固定で縦潰れ防止)
        f_btns = tk.Frame(self, bg=COLOR_BG_MAIN)
        f_btns.pack(side=tk.BOTTOM, fill=tk.X, pady=20, padx=24)

        def _apply():
            try:
                y = self.v_year.get()
                m = self.v_month.get()
                d = self.v_day.get()
                h = self.v_hour.get()
                mi = self.v_min.get()
                s = self.v_sec.get()
                dt_str = f"{y:04d}-{m:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"
            except Exception as ex:
                messagebox.showerror("入力エラー", f"日時の入力値が不正です:\n{ex}", parent=self)
                return

            if sys.platform.startswith("win"):
                messagebox.showinfo(
                    "日時設定 (Windows)",
                    f"Windows環境のため実際のシステム時刻変更はスキップされました。\n設定指定値: {dt_str}\n(Linux/ラズパイ環境で自動設定コマンドが実行されます)",
                    parent=self
                )
                self.destroy()
                return

            cmds = [
                ["sudo", "timedatectl", "set-ntp", "false"],
                ["sudo", "timedatectl", "set-time", dt_str],
                ["sudo", "date", "-s", dt_str],
                ["sudo", "hwclock", "-w"]
            ]
            results = []
            success_count = 0
            for cmd in cmds:
                try:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                    if res.returncode == 0:
                        success_count += 1
                        results.append(f"成功: {' '.join(cmd)}")
                    else:
                        err = res.stderr.strip() or res.stdout.strip()
                        results.append(f"失敗 ({' '.join(cmd)}): {err}")
                except Exception as ex:
                    results.append(f"エラー ({' '.join(cmd)}): {ex}")

            msg = f"日時を [{dt_str}] に設定しました。\n\n【実行詳細】\n" + "\n".join(results)
            if success_count > 0:
                messagebox.showinfo("日時設定完了", msg, parent=self)
                self.destroy()
            else:
                messagebox.showerror("日時設定失敗", msg, parent=self)

        btn_save = tk.Button(
            f_btns, text="日時を本体に反映", font=(FONT_FAMILY, 12, "bold"),
            bg=COLOR_ACCENT, fg="white", relief="flat", padx=20, pady=8,
            cursor="hand2", command=_apply
        )
        btn_save.pack(side=tk.RIGHT, padx=(10, 0))

        btn_cancel = tk.Button(
            f_btns, text="キャンセル", font=(FONT_FAMILY, 12, "bold"),
            bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN, relief="flat", padx=18, pady=8,
            cursor="hand2", command=self.destroy
        )
        btn_cancel.pack(side=tk.RIGHT)

        # タイトル & 説明
        tk.Label(
            self, text="ラズパイ本体の日時設定", font=FONT_LARGE,
            bg=COLOR_BG_MAIN, fg=COLOR_ACCENT
        ).pack(pady=(20, 6))

        tk.Label(
            self, text="本体のシステム日付・時刻を設定します。\nLinux / Raspberry Pi 環境で timedatectl / date が更新されます",
            font=FONT_SET_VAL, bg=COLOR_BG_MAIN, fg=COLOR_TEXT_SUB, justify="center",
            wraplength=500
        ).pack(pady=(0, 16), padx=20)

        f_dt = tk.Frame(self, bg=COLOR_BG_PANEL, padx=20, pady=20)
        f_dt.pack(padx=24, fill=tk.X, expand=True)

        font_num = (FONT_FAMILY, 14, "bold")
        font_lbl = (FONT_FAMILY, 12, "bold")

        # 年月日
        f_date = tk.Frame(f_dt, bg=COLOR_BG_PANEL)
        f_date.pack(fill=tk.X, pady=8)
        self.v_year = tk.IntVar(value=now.year)
        self.v_month = tk.IntVar(value=now.month)
        self.v_day = tk.IntVar(value=now.day)

        tk.Label(f_date, text="日付:", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=6, anchor="w").pack(side=tk.LEFT)
        tk.Spinbox(f_date, from_=2020, to=2099, textvariable=self.v_year, font=font_num, width=6, justify="center").pack(side=tk.LEFT, padx=4)
        tk.Label(f_date, text="年", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(side=tk.LEFT, padx=(0, 8))

        tk.Spinbox(f_date, from_=1, to=12, textvariable=self.v_month, font=font_num, width=4, justify="center").pack(side=tk.LEFT, padx=4)
        tk.Label(f_date, text="月", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(side=tk.LEFT, padx=(0, 8))

        tk.Spinbox(f_date, from_=1, to=31, textvariable=self.v_day, font=font_num, width=4, justify="center").pack(side=tk.LEFT, padx=4)
        tk.Label(f_date, text="日", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(side=tk.LEFT)

        # 時分秒
        f_time = tk.Frame(f_dt, bg=COLOR_BG_PANEL)
        f_time.pack(fill=tk.X, pady=8)
        self.v_hour = tk.IntVar(value=now.hour)
        self.v_min = tk.IntVar(value=now.minute)
        self.v_sec = tk.IntVar(value=now.second)

        tk.Label(f_time, text="時刻:", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=6, anchor="w").pack(side=tk.LEFT)
        tk.Spinbox(f_time, from_=0, to=23, textvariable=self.v_hour, font=font_num, width=4, justify="center").pack(side=tk.LEFT, padx=4)
        tk.Label(f_time, text="時", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(side=tk.LEFT, padx=(0, 8))

        tk.Spinbox(f_time, from_=0, to=59, textvariable=self.v_min, font=font_num, width=4, justify="center").pack(side=tk.LEFT, padx=4)
        tk.Label(f_time, text="分", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(side=tk.LEFT, padx=(0, 8))

        tk.Spinbox(f_time, from_=0, to=59, textvariable=self.v_sec, font=font_num, width=4, justify="center").pack(side=tk.LEFT, padx=4)
        tk.Label(f_time, text="秒", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(side=tk.LEFT)


class SettingsDialog(tk.Toplevel):
    """詳細設定ダイアログ (5タブ構成)"""

    def __init__(self, parent, settings_mgr, qr_reader, ocr_engine, color_detector, on_save_callback=None, app_instance=None):
        super().__init__(parent)
        self.title("詳細設定")
        self.geometry("1400x900")
        self.configure(bg=COLOR_BG_MAIN)
        self.transient(parent)
        self.grab_set()
        configure_modal_toplevel(self)

        self.settings = settings_mgr
        self.temp_data = json.loads(json.dumps(self.settings.data))
        self.qr_reader = qr_reader
        self.ocr_engine = ocr_engine
        self.color_detector = color_detector
        self.on_save_callback = on_save_callback
        self.app_instance = app_instance or getattr(parent, "app_instance", None)

        self.has_changes = False
        self.active_roi_key = "cam_1_qr"
        self.roi_drag_start = None
        self.roi_drag_cur = None
        self.roi_draw_scale = (0, 0, 1, 1, 640, 480)
        self._roi_timer_id = None
        self._roi_img_tk = None
        self._last_test_time = 0.0

        # 4点台形補正モード (cam_2_commit / cam_2_color のみ対応)
        # v_perspective_mode: True = 4点指定モード, False = 矩形ドラッグモード
        self.v_perspective_mode = tk.BooleanVar(value=False)
        # 4点クリック設定中の点リスト (正規化座標 [[rx, ry], ...], 最大4点)
        self._persp_points: list = []

        self.v_cam1_by_path = tk.StringVar(value="")
        self.v_cam2_by_path = tk.StringVar(value="")
        self.v_cam3_by_path = tk.StringVar(value="")
        self.v_cam4_by_path = tk.StringVar(value="")

        # 監視サイクル数
        self.v_monitor_count = tk.IntVar(value=self.temp_data.get("system", {}).get("monitor_count", 1))

        # カメラフォーカス設定
        self.v_cam1_af = tk.BooleanVar(value=True)
        self.v_cam1_focus = tk.IntVar(value=-1)
        self.v_cam2_af = tk.BooleanVar(value=True)
        self.v_cam2_focus = tk.IntVar(value=-1)
        self.v_cam3_af = tk.BooleanVar(value=True)
        self.v_cam3_focus = tk.IntVar(value=-1)
        self.v_cam4_af = tk.BooleanVar(value=True)
        self.v_cam4_focus = tk.IntVar(value=-1)

        # HSVプレビュー制御
        self._hsv_timer_id = None
        self._hsv_freeze_frame = None
        self.v_hsv_freeze = tk.BooleanVar(value=False)
        self.v_hsv_view_mode = tk.StringVar(value="mask")        # "mask" (二値化) または "color" (元画像)
        self.v_hsv_target_color = tk.StringVar(value="green")    # "green", "yellow", "red"
        self._hsv_img_tk = None

        # GPIO / ブザー設定
        self.v_ok_out_time = tk.StringVar(value="0.5")
        self.v_ng_out_time = tk.StringVar(value="")
        self.v_ng_out_hold = tk.BooleanVar(value=False)
        self.v_buzzer_line1 = tk.StringVar(value="")
        self.v_buzzer_line2 = tk.StringVar(value="")
        self.v_buzzer_both = tk.StringVar(value="")

        # ラズパイ軽量化設定
        self.v_camera_fps = tk.IntVar(value=5)
        self.v_use_mjpg = tk.BooleanVar(value=True)

        self._init_ttk_style()
        self._build_ui()
        self._load_values()

        # ROI & HSVプレビューループ開始
        self._start_roi_preview()
        self._start_hsv_preview()

    def destroy(self):
        if getattr(self, "_roi_timer_id", None) is not None:
            try:
                self.after_cancel(self._roi_timer_id)
            except Exception:
                pass
            self._roi_timer_id = None

        if getattr(self, "_hsv_timer_id", None) is not None:
            try:
                self.after_cancel(self._hsv_timer_id)
            except Exception:
                pass
            self._hsv_timer_id = None

        if PYGAME_AVAILABLE:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass

        super().destroy()

    def _init_ttk_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook", background=COLOR_BG_MAIN, borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=COLOR_BG_PANEL,
            foreground=COLOR_TEXT_MAIN,
            font=FONT_SET_TAB,
            padding=[20, 10],
            focuscolor=COLOR_BG_MAIN
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", COLOR_ACCENT)],
            foreground=[("selected", "black")]
        )

    def _mark_changed(self, *args):
        self.has_changes = True
        self.btn_save.configure(bg=COLOR_OK, fg="black", text="変更を適用して保存")

    def _spinbox(self, parent, var: tk.Variable, from_, to, increment=1, width=6):
        """common_ui_requirements 準拠の標準スピンボックス"""
        sb = tk.Spinbox(
            parent, from_=from_, to=to, increment=increment, textvariable=var,
            font=FONT_SET_VAL, width=width, bg=COLOR_BG_INPUT, fg="white",
            buttonbackground="#78909C", bd=1, relief="solid",
            repeatdelay=0, repeatinterval=0, justify="center"
        )
        def _stop_repeat(event=None):
            try:
                rep_id = sb.tk.call("set", "::tk::spinbox::Repeater")
                if rep_id:
                    sb.tk.call("after", "cancel", rep_id)
            except Exception:
                pass
        sb.bind("<ButtonRelease-1>", _stop_repeat, add="+")
        sb.bind("<Leave>", _stop_repeat, add="+")
        sb.bind("<FocusOut>", _stop_repeat, add="+")
        var.trace_add("write", lambda *a: self._mark_changed())
        return sb

    def create_scrollable_panel(self, parent):
        """Canvas + Scrollbar によるスクロール可能パネル"""
        canvas = tk.Canvas(parent, bg=COLOR_BG_MAIN, highlightthickness=0)
        sb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        inner = tk.Frame(canvas, bg=COLOR_BG_MAIN)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        def _on_wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)
        self.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _build_ui(self):
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=16, pady=(16, 0))

        # タブ作成
        self.tab_cam = tk.Frame(self.notebook, bg=COLOR_BG_MAIN, padx=16, pady=16)
        self.tab_roi = tk.Frame(self.notebook, bg=COLOR_BG_MAIN, padx=16, pady=16)
        self.tab_color = tk.Frame(self.notebook, bg=COLOR_BG_MAIN)
        self.tab_res = tk.Frame(self.notebook, bg=COLOR_BG_MAIN, padx=16, pady=16)
        self.tab_sys = tk.Frame(self.notebook, bg=COLOR_BG_MAIN, padx=16, pady=16)

        self.notebook.add(self.tab_cam, text=" カメラ設定 ")
        self.notebook.add(self.tab_roi, text=" 読取・ROI設定 ")
        self.notebook.add(self.tab_color, text=" 色判定・サイクル設定 ")
        self.notebook.add(self.tab_res, text=" 画素数設定 ")
        self.notebook.add(self.tab_sys, text=" システム設定 ")

        # 下部ボタンバー
        f_bottom = tk.Frame(self, bg=COLOR_BG_MAIN, height=60)
        f_bottom.pack(fill=tk.X, side=tk.BOTTOM, padx=16, pady=12)

        btn_help = tk.Button(
            f_bottom, text="ヘルプ", font=FONT_BOLD,
            bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN, relief="flat",
            padx=16, pady=6, cursor="hand2", command=self._show_help
        )
        btn_help.pack(side=tk.LEFT)

        self.btn_save = tk.Button(
            f_bottom, text="保存して閉じる", font=FONT_BOLD,
            bg="#455A64", fg="white", relief="flat",
            padx=20, pady=6, cursor="hand2", command=self._save_and_close
        )
        self.btn_save.pack(side=tk.RIGHT, padx=(10, 0))

        btn_cancel = tk.Button(
            f_bottom, text="キャンセル", font=FONT_BOLD,
            bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN, relief="flat",
            padx=16, pady=6, cursor="hand2", command=self.destroy
        )
        btn_cancel.pack(side=tk.RIGHT)

        # 各タブの中身構築
        self._build_tab_cam()
        self._build_tab_roi()
        self._build_tab_color()
        self._build_tab_res()
        self._build_tab_sys()

    # -------------------------------------------------------------
    # タブ 1: カメラ設定 (フォーカス・4台カメラ対応)
    # -------------------------------------------------------------
    def _build_tab_cam(self):
        c_out, c_in = create_card(self.tab_cam, "カメラ設定・監視サイクル数・フォーカス")
        c_out.pack(fill=tk.BOTH, expand=True)

        self.dev_list = get_available_camera_list()
        self.dev_labels = [d["label"] for d in self.dev_list]

        # 1. 監視サイクル数選択
        f_mon = tk.LabelFrame(c_in, text="監視システム構成", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, padx=12, pady=8)
        f_mon.pack(fill=tk.X, pady=(0, 10))

        def _on_mon_changed():
            self._mark_changed()
            val = self.v_monitor_count.get()
            if val == 2:
                self.f_cam34_group.pack(fill=tk.X, pady=6)
            else:
                self.f_cam34_group.pack_forget()

        r1 = tk.Radiobutton(
            f_mon, text="1サイクル監視", variable=self.v_monitor_count, value=1,
            bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, selectcolor=COLOR_BG_INPUT, font=FONT_NORMAL, command=_on_mon_changed
        )
        r1.pack(side=tk.LEFT, padx=(0, 20))
        r2 = tk.Radiobutton(
            f_mon, text="2サイクル並列監視", variable=self.v_monitor_count, value=2,
            bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, selectcolor=COLOR_BG_INPUT, font=FONT_NORMAL, command=_on_mon_changed
        )
        r2.pack(side=tk.LEFT)
        Tooltip(r1, "1つの作業台でカメラ2台 (QR + 画面) を使用します")
        Tooltip(r2, "2つの作業台を同時に並列監視します (合計4台のカメラ: QR×2 + 画面×2)")

        # 2. カメラ行ビルダー
        def _build_cam_row(parent, cam_id: str, label_text: str, cb_var, by_path_var, af_var, focus_var):
            row = tk.Frame(parent, bg=COLOR_BG_PANEL)
            row.pack(fill=tk.X, pady=6)

            tk.Label(row, text=label_text, font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=12, anchor="w").pack(side=tk.LEFT)

            cb = ttk.Combobox(row, values=self.dev_labels, state="readonly", font=FONT_SET_VAL, width=28)
            cb.pack(side=tk.LEFT, padx=(0, 10))

            def _on_sel(e=None):
                s_idx = cb.current()
                if 0 <= s_idx < len(self.dev_list):
                    by_path_var.set(self.dev_list[s_idx].get("by_path", ""))
                self._mark_changed()
            cb.bind("<<ComboboxSelected>>", _on_sel)

            # フォーカス調整 (AF / 手動スピンボックス)
            chk_af = tk.Checkbutton(
                row, text="AF", variable=af_var, font=FONT_NORMAL,
                bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, selectcolor=COLOR_BG_INPUT,
                command=lambda: (self._mark_changed(), _on_af_toggle())
            )
            chk_af.pack(side=tk.LEFT, padx=(5, 6))

            tk.Label(row, text="フォーカス:", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(side=tk.LEFT)
            sp_foc = self._spinbox(row, focus_var, 0, 1024, increment=5, width=5)
            sp_foc.pack(side=tk.LEFT, padx=(4, 0))

            def _on_af_toggle():
                if af_var.get():
                    sp_foc.configure(state="disabled")
                else:
                    sp_foc.configure(state="normal")
                self._apply_cam_focus_live(cam_id, af_var.get(), focus_var.get())

            def _on_focus_change(*a):
                if not af_var.get():
                    self._apply_cam_focus_live(cam_id, af_var.get(), focus_var.get())
            focus_var.trace_add("write", _on_focus_change)

            if af_var.get():
                sp_foc.configure(state="disabled")

            Tooltip(chk_af, "カメラ本体のオートフォーカスを有効にします")
            Tooltip(sp_foc, "フォーカス値 (0〜1024)。AFをOFFにすると手動でピントを調整できます")
            return cb, sp_foc

        # ライン1用グループ
        f_l1_grp = tk.LabelFrame(c_in, text="ライン 1 カメラ設定", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT, padx=10, pady=6)
        f_l1_grp.pack(fill=tk.X, pady=6)
        self.cb_cam1, self.sp_cam1_focus = _build_cam_row(
            f_l1_grp, "cam_1", "カメラ 1:", None, self.v_cam1_by_path, self.v_cam1_af, self.v_cam1_focus
        )
        self.cb_cam2, self.sp_cam2_focus = _build_cam_row(
            f_l1_grp, "cam_2", "カメラ 2:", None, self.v_cam2_by_path, self.v_cam2_af, self.v_cam2_focus
        )

        # ライン2用グループ (監視サイクル数=2の時のみ表示)
        self.f_cam34_group = tk.LabelFrame(c_in, text="ライン 2 カメラ設定", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT, padx=10, pady=6)
        self.cb_cam3, self.sp_cam3_focus = _build_cam_row(
            self.f_cam34_group, "cam_3", "カメラ 3:", None, self.v_cam3_by_path, self.v_cam3_af, self.v_cam3_focus
        )
        self.cb_cam4, self.sp_cam4_focus = _build_cam_row(
            self.f_cam34_group, "cam_4", "カメラ 4:", None, self.v_cam4_by_path, self.v_cam4_af, self.v_cam4_focus
        )
        if self.v_monitor_count.get() == 2:
            self.f_cam34_group.pack(fill=tk.X, pady=6)

        def _rescan():
            self.dev_list = get_available_camera_list()
            self.dev_labels = [d["label"] for d in self.dev_list]
            for cb in (self.cb_cam1, self.cb_cam2, self.cb_cam3, self.cb_cam4):
                cb.configure(values=self.dev_labels)
            messagebox.showinfo("再スキャン完了", f"{len(self.dev_list)}台のカメラを検出しました", parent=self)

        btn_scan = tk.Button(c_in, text="接続カメラを再スキャン", font=FONT_NORMAL, bg="#546E7A", fg="white", relief="flat", padx=12, pady=6, command=_rescan)
        btn_scan.pack(anchor="w", pady=10)
        Tooltip(btn_scan, "PCに接続されているカメラデバイスを再スキャンします")

    def _apply_cam_focus_live(self, cam_id: str, is_af: bool, focus_val: int):
        """設定ダイアログから実機カメラへ即時フォーカス適用"""
        if self.app_instance and hasattr(self.app_instance, "caps"):
            with self.app_instance.camera_lock:
                cap = self.app_instance.caps.get(cam_id)
                if cap and cap.isOpened():
                    try:
                        cap.set(cv2.CAP_PROP_AUTOFOCUS, 1 if is_af else 0)
                        if not is_af and focus_val >= 0:
                            cap.set(cv2.CAP_PROP_FOCUS, focus_val)
                    except Exception:
                        pass

    # -------------------------------------------------------------
    # タブ 2: 読取・ROI設定
    # -------------------------------------------------------------
    def _build_tab_roi(self):
        f_left = tk.Frame(self.tab_roi, bg=COLOR_BG_MAIN)
        f_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

        f_right = tk.Frame(self.tab_roi, bg=COLOR_BG_MAIN, width=450)
        f_right.pack(side=tk.RIGHT, fill=tk.BOTH)
        f_right.pack_propagate(False)

        c_canvas_out, c_canvas_in = create_card(f_left, "ROIエリア指定プレビュー")
        c_canvas_out.pack(fill=tk.BOTH, expand=True)

        self.roi_canvas = tk.Canvas(c_canvas_in, bg="black", cursor="crosshair", highlightthickness=0)
        self.roi_canvas.pack(fill=tk.BOTH, expand=True)
        self.roi_canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.roi_canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.roi_canvas.bind("<ButtonRelease-1>", self._on_canvas_release)

        c_ctrl_out, c_ctrl_in = create_card(f_right, "ROI選択・調整")
        c_ctrl_out.pack(fill=tk.BOTH, expand=True)

        tk.Label(c_ctrl_in, text="調整するROIを選択:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(anchor="w", pady=(5, 4))
        self.v_roi_target = tk.StringVar(value="cam_1_qr")

        self.roi_radio_buttons = []
        roi_list = [
            ("cam_1_qr", "1. カメラ 1: QRコード (L1)"),
            ("cam_2_commit", "2. カメラ 2: コミット番号 (L1)"),
            ("cam_2_color", "3. カメラ 2: 画面色 (L1)"),
            ("cam_3_qr", "4. カメラ 3: QRコード (L2)"),
            ("cam_4_commit", "5. カメラ 4: コミット番号 (L2)"),
            ("cam_4_color", "6. カメラ 4: 画面色 (L2)")
        ]

        for key, name in roi_list:
            r = tk.Radiobutton(
                c_ctrl_in, text=name, variable=self.v_roi_target, value=key,
                bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, selectcolor=COLOR_BG_INPUT,
                font=FONT_NORMAL, command=self._on_roi_target_changed
            )
            r.pack(anchor="w", pady=2)
            self.roi_radio_buttons.append((key, r))

        # 4点台形補正モードトグル (cam_2 系のみ有効)
        ttk.Separator(c_ctrl_in, orient="horizontal").pack(fill=tk.X, pady=(10, 6))

        self.chk_persp = tk.Checkbutton(
            c_ctrl_in, text="4点台形補正モード",
            variable=self.v_perspective_mode, font=FONT_NORMAL,
            bg=COLOR_BG_PANEL, fg=COLOR_ACCENT, selectcolor=COLOR_BG_INPUT,
            activebackground=COLOR_BG_PANEL, activeforeground=COLOR_ACCENT,
            command=self._on_persp_mode_changed
        )
        self.chk_persp.pack(anchor="w", pady=(0, 4))
        Tooltip(self.chk_persp, "カメラを斜めに設置する場合に4頂点を指定して台形歪みを補正します")

        self.lbl_persp_hint = tk.Label(
            c_ctrl_in,
            text="左上→右上→右下→左下の順にクリックして4頂点を指定",
            font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB,
            wraplength=390, justify="left"
        )
        self.lbl_persp_hint.pack(anchor="w", pady=(0, 4))
        self.lbl_persp_hint.pack_forget()  # 初期は非表示

        self.btn_persp_clear = tk.Button(
            c_ctrl_in, text="4点クリア", font=FONT_NORMAL,
            bg="#546E7A", fg="white", relief="flat", padx=8, pady=4,
            command=self._clear_persp_points
        )
        self.btn_persp_clear.pack(anchor="w", pady=(0, 8))
        self.btn_persp_clear.pack_forget()  # 初期は非表示
        Tooltip(self.btn_persp_clear, "指定した4点を消去し通常の矩形ROIに戻します")

        ttk.Separator(c_ctrl_in, orient="horizontal").pack(fill=tk.X, pady=(0, 10))

        f_coords = tk.Frame(c_ctrl_in, bg=COLOR_BG_PANEL)
        f_coords.pack(fill=tk.X, pady=(0, 10))
        self.lbl_roi_coords = tk.Label(
            f_coords, text="rx1: 0.000, ry1: 0.000\nrx2: 1.000, ry2: 1.000",
            font=FONT_NORMAL, bg=COLOR_BG_INPUT, fg=COLOR_ACCENT, pady=6
        )
        self.lbl_roi_coords.pack(fill=tk.X)

        c_test_out, c_test_in = create_card(c_ctrl_in, "認識テスト結果")
        c_test_out.pack(fill=tk.X, pady=10)
        self.lbl_roi_test = tk.Label(
            c_test_in, text="読取待機中...", font=FONT_NORMAL,
            bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB, justify="left", wraplength=400
        )
        self.lbl_roi_test.pack(fill=tk.X, pady=4)

        # ツールチップ設定
        Tooltip(self.roi_canvas,
                "カメラ映像のプレビュー\n"
                "矩形モード: マウスドラッグでROIエリアを指定\n"
                "4点モード: クリックで頂点を1点ずつ指定 (左上→右上→右下→左下)")
        Tooltip(self.chk_persp,
                "カメラ2が画面に対して斜め設置の場合にONにします\n"
                "ONにするとプレビュー上でクリックして4頂点を指定し\n"
                "透視変換 (台形補正) を適用してからOCR/色判定を行います")
        Tooltip(self.btn_persp_clear,
                "指定した4頂点をクリアして通常の矩形ROIモードに戻します")
        Tooltip(self.lbl_roi_coords,
                "現在設定中のROIエリアの座標を表示します\n"
                "値は画像サイズに対する正規化座標 (0.0〜1.0)")

    def _on_persp_mode_changed(self):
        """台形補正モード切り替え時の処理"""
        is_persp = self.v_perspective_mode.get()
        can_persp = self.active_roi_key in ("cam_2_commit", "cam_2_color", "cam_4_commit", "cam_4_color")
        if is_persp and can_persp:
            self.lbl_persp_hint.pack(anchor="w", pady=(0, 4))
            self.btn_persp_clear.pack(anchor="w", pady=(0, 8))
            self._persp_points = []
            self._update_roi_coords_label()
            self._mark_changed()
        else:
            self.lbl_persp_hint.pack_forget()
            self.btn_persp_clear.pack_forget()

    def _clear_persp_points(self):
        """台形補正の4点をクリアして矩形モードに戻す"""
        self._persp_points = []
        r = self.temp_data["roi"].get(self.active_roi_key, {})
        r.pop("points", None)
        r["points"] = None
        self._update_roi_coords_label()
        self._mark_changed()

    def _on_roi_target_changed(self):
        self.active_roi_key = self.v_roi_target.get()
        r = self.temp_data["roi"].get(self.active_roi_key, {})
        pts = r.get("points")
        # チェックボックスを保存済み状態に同期
        self.v_perspective_mode.set(isinstance(pts, list) and len(pts) == 4)
        self._on_persp_mode_changed()
        self._update_roi_coords_label()

    def _update_roi_coords_label(self):
        r = self.temp_data["roi"].get(self.active_roi_key, {})
        pts = r.get("points")
        if isinstance(pts, list) and len(pts) == 4:
            lines = [f"P{i+1}: ({pts[i][0]:.3f}, {pts[i][1]:.3f})" for i in range(4)]
            txt = "  ".join(lines[:2]) + "\n" + "  ".join(lines[2:])
        elif self.v_perspective_mode.get() and self._persp_points:
            pts_disp = [f"P{i+1}: ({p[0]:.3f}, {p[1]:.3f})" for i, p in enumerate(self._persp_points)]
            txt = "選択中: " + " / ".join(pts_disp) + f"\n残り{4 - len(self._persp_points)}点"
        else:
            txt = (
                f"rx1: {r.get('rx1', 0.0):.3f}, ry1: {r.get('ry1', 0.0):.3f}\n"
                f"rx2: {r.get('rx2', 1.0):.3f}, ry2: {r.get('ry2', 1.0):.3f}"
            )
        self.lbl_roi_coords.configure(text=txt)

    def _on_canvas_press(self, event):
        ox, oy, nw, nh, fw, fh = self.roi_draw_scale
        cx = max(ox, min(ox + nw, event.x))
        cy = max(oy, min(oy + nh, event.y))

        # 4点台形補正モード: クリックごとに1点追加
        if (self.v_perspective_mode.get()
                and self.active_roi_key in ("cam_2_commit", "cam_2_color", "cam_4_commit", "cam_4_color")
                and nw > 0 and nh > 0):
            rx = round(max(0.0, min(1.0, (cx - ox) / nw)), 4)
            ry = round(max(0.0, min(1.0, (cy - oy) / nh)), 4)
            self._persp_points.append([rx, ry])
            if len(self._persp_points) == 4:
                # 4点揃ったら保存
                r = self.temp_data["roi"].get(self.active_roi_key, {})
                r["points"] = [list(p) for p in self._persp_points]
                self.temp_data["roi"][self.active_roi_key] = r
                self._persp_points = []
                self._mark_changed()
            self._update_roi_coords_label()
            return

        # 通常矩形ドラッグモード
        self.roi_drag_start = (cx, cy)
        self.roi_drag_cur = (cx, cy)

    def _on_canvas_drag(self, event):
        if self.roi_drag_start:
            ox, oy, nw, nh, fw, fh = self.roi_draw_scale
            cx = max(ox, min(ox + nw, event.x))
            cy = max(oy, min(oy + nh, event.y))
            self.roi_drag_cur = (cx, cy)

    def _on_canvas_release(self, event):
        if not self.roi_drag_start:
            return
        ox, oy, nw, nh, fw, fh = self.roi_draw_scale
        x1, y1 = self.roi_drag_start
        x2 = max(ox, min(ox + nw, event.x))
        y2 = max(oy, min(oy + nh, event.y))

        if nw > 0 and nh > 0:
            rx1 = round(max(0.0, min(1.0, (min(x1, x2) - ox) / nw)), 3)
            ry1 = round(max(0.0, min(1.0, (min(y1, y2) - oy) / nh)), 3)
            rx2 = round(max(0.0, min(1.0, (max(x1, x2) - ox) / nw)), 3)
            ry2 = round(max(0.0, min(1.0, (max(y1, y2) - oy) / nh)), 3)

            if (rx2 - rx1) > 0.02 and (ry2 - ry1) > 0.02:
                r = self.temp_data["roi"].get(self.active_roi_key, {})
                r.update({"rx1": rx1, "ry1": ry1, "rx2": rx2, "ry2": ry2})
                # 矩形ドラッグ時は points をリセット (台形補正解除)
                r["points"] = None
                self.temp_data["roi"][self.active_roi_key] = r
                self._update_roi_coords_label()
                self._mark_changed()

        self.roi_drag_start = None
        self.roi_drag_cur = None

    def _start_roi_preview(self):
        self._update_roi_preview()

    def _update_roi_preview(self):
        if not self.winfo_exists():
            return

        try:
            sel_tab = self.notebook.select()
            if sel_tab == str(self.tab_roi):
                if self.active_roi_key == "cam_1_qr":
                    cam_key = "cam_1"
                elif self.active_roi_key in ("cam_2_commit", "cam_2_color"):
                    cam_key = "cam_2"
                elif self.active_roi_key == "cam_3_qr":
                    cam_key = "cam_3"
                elif self.active_roi_key in ("cam_4_commit", "cam_4_color"):
                    cam_key = "cam_4"
                else:
                    cam_key = "cam_1"

                frame = None
                if self.app_instance and hasattr(self.app_instance, "last_frames"):
                    with self.app_instance.camera_lock:
                        f = self.app_instance.last_frames.get(cam_key)
                        if f is not None:
                            frame = f.copy()

                if frame is None:
                    frame = np.zeros((480, 640, 3), dtype=np.uint8)
                    frame[:] = (30, 30, 30)
                    cv2.putText(frame, f"{cam_key} [NO SIGNAL]", (140, 240),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (120, 120, 120), 2)

                cw = max(10, self.roi_canvas.winfo_width())
                ch = max(10, self.roi_canvas.winfo_height())
                fh, fw = frame.shape[:2]

                scale = min(cw / fw, ch / fh)
                nw = max(1, int(fw * scale))
                nh = max(1, int(fh * scale))
                ox = (cw - nw) // 2
                oy = (ch - nh) // 2
                self.roi_draw_scale = (ox, oy, nw, nh, fw, fh)

                resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
                rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
                self._roi_img_tk = ImageTk.PhotoImage(image=Image.fromarray(rgb))

                self.roi_canvas.delete("all")
                self.roi_canvas.create_image(ox, oy, anchor="nw", image=self._roi_img_tk)

                # 現在設定中の ROI 枠
                r = self.temp_data["roi"].get(self.active_roi_key, {})
                pts = r.get("points")

                if isinstance(pts, list) and len(pts) == 4:
                    # 4点台形補正: 緑のポリゴン
                    poly = [(ox + int(p[0] * nw), oy + int(p[1] * nh)) for p in pts]
                    flat = [coord for px_py in poly for coord in px_py]
                    self.roi_canvas.create_polygon(flat, outline="#00e676", fill="", width=2)
                    # 頂点マーカー (丸のみ、ラベルなし)
                    for (px, py) in poly:
                        self.roi_canvas.create_oval(px - 4, py - 4, px + 4, py + 4, fill="#00e676", outline="")
                else:
                    # 矩形
                    rx1, ry1 = r.get("rx1", 0.0), r.get("ry1", 0.0)
                    rx2, ry2 = r.get("rx2", 1.0), r.get("ry2", 1.0)
                    cx1 = ox + int(rx1 * nw)
                    cy1 = oy + int(ry1 * nh)
                    cx2 = ox + int(rx2 * nw)
                    cy2 = oy + int(ry2 * nh)
                    self.roi_canvas.create_rectangle(cx1, cy1, cx2, cy2, outline="#00e676", width=2)

                # 4点選択中の仮点 (シアン色、ラベルなし)
                if self.v_perspective_mode.get() and self._persp_points:
                    for p in self._persp_points:
                        px = ox + int(p[0] * nw)
                        py = oy + int(p[1] * nh)
                        self.roi_canvas.create_oval(px - 5, py - 5, px + 5, py + 5, fill="#00bcd4", outline="")

                # ドラッグ中の枠 (黄色破線)
                if self.roi_drag_start and self.roi_drag_cur:
                    sx, sy = self.roi_drag_start
                    ex, ey = self.roi_drag_cur
                    self.roi_canvas.create_rectangle(
                        sx, sy, ex, ey, outline="#ffeb3b", width=2, dash=(4, 4)
                    )

                # 認識テスト (約0.25秒ごとに実行)
                now = time.time()
                if now - self._last_test_time > 0.25:
                    self._last_test_time = now
                    self._run_roi_test(frame, r)

        except Exception:
            pass

        self._roi_timer_id = self.after(50, self._update_roi_preview)

    def _run_roi_test(self, frame: np.ndarray, roi: dict):
        try:
            if self.active_roi_key == "cam_1_qr":
                txt, _, _ = self.qr_reader.read_qr(frame, roi)
                if txt:
                    clean = txt.strip()
                    head = clean[:4] if len(clean) >= 4 else clean
                    self.lbl_roi_test.configure(text=f"QRコード検出: {head}", fg=COLOR_OK)
                else:
                    self.lbl_roi_test.configure(text="QRコード: 未検出", fg=COLOR_TEXT_SUB)
            elif self.active_roi_key == "cam_2_commit":
                res = self.ocr_engine.inspect_frame(frame, roi)
                commit_num = res.get("commit_number")
                raw = res.get("raw_text", "")
                if commit_num:
                    self.lbl_roi_test.configure(text=f"コミット番号検出: {commit_num}", fg=COLOR_OK)
                elif raw:
                    self.lbl_roi_test.configure(text=f"OCR読取中: {raw}", fg=COLOR_WARNING)
                else:
                    self.lbl_roi_test.configure(text="コミット番号: 未検出", fg=COLOR_TEXT_SUB)
            elif self.active_roi_key == "cam_2_color":
                res = self.color_detector.detect_color(frame, roi)
                color = res.get("color", "NONE")
                hsv = res.get("hsv_mean", (0, 0, 0))
                col_disp = {"GREEN": "緑色", "YELLOW": "黄色", "RED": "赤色"}.get(color, "未検知")
                fg_col = COLOR_OK if color == "GREEN" else (COLOR_WARNING if color == "YELLOW" else (COLOR_NG if color == "RED" else COLOR_TEXT_SUB))
                self.lbl_roi_test.configure(
                    text=f"画面色判定: {col_disp} [H:{int(hsv[0])}, S:{int(hsv[1])}, V:{int(hsv[2])}]",
                    fg=fg_col
                )
        except Exception:
            pass

    # -------------------------------------------------------------
    # タブ 3: 色判定・HSV設定 (リアルタイム映像・二値化マスク・スポイト対応)
    # -------------------------------------------------------------
    def _build_tab_color(self):
        f_left = tk.Frame(self.tab_color, bg=COLOR_BG_MAIN)
        f_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(16, 8), pady=16)

        f_right = tk.Frame(self.tab_color, bg=COLOR_BG_MAIN, width=480)
        f_right.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(8, 16), pady=16)
        f_right.pack_propagate(False)

        # --- 左側: 映像・マスクプレビューカード ---
        c_prev_out, c_prev_in = create_card(f_left, "HSV判定プレビュー & マスク確認")
        c_prev_out.pack(fill=tk.BOTH, expand=True)

        # プレビュー上部コントロール
        f_hsv_ctrl = tk.Frame(c_prev_in, bg=COLOR_BG_PANEL)
        f_hsv_ctrl.pack(fill=tk.X, pady=(0, 6))

        # 静止画 / リアルタイム 切替
        self.btn_hsv_freeze = tk.Button(
            f_hsv_ctrl, text="静止画キャプチャ", font=FONT_NORMAL,
            bg="#546E7A", fg="white", relief="flat", padx=8, pady=2,
            command=self._toggle_hsv_freeze
        )
        self.btn_hsv_freeze.pack(side=tk.LEFT, padx=(0, 10))
        Tooltip(self.btn_hsv_freeze, "現在の映像を静止画として固定し、じっくりHSV閾値を調整できます\nもう一度押すとリアルタイム映像に戻ります")

        # 表示モード (二値化マスク / カラー元画像)
        tk.Label(f_hsv_ctrl, text="表示:", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(5, 2))
        r_mask = tk.Radiobutton(
            f_hsv_ctrl, text="二値化マスク", variable=self.v_hsv_view_mode, value="mask",
            bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, selectcolor=COLOR_BG_INPUT, font=FONT_NORMAL
        )
        r_mask.pack(side=tk.LEFT, padx=2)
        r_col = tk.Radiobutton(
            f_hsv_ctrl, text="カラー映像", variable=self.v_hsv_view_mode, value="color",
            bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, selectcolor=COLOR_BG_INPUT, font=FONT_NORMAL
        )
        r_col.pack(side=tk.LEFT, padx=2)

        # 調整色選択
        tk.Label(f_hsv_ctrl, text="対象:", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(10, 2))
        for col_k, col_l in [("green", "緑"), ("yellow", "黄"), ("red", "赤")]:
            tk.Radiobutton(
                f_hsv_ctrl, text=col_l, variable=self.v_hsv_target_color, value=col_k,
                bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, selectcolor=COLOR_BG_INPUT, font=FONT_NORMAL
            ).pack(side=tk.LEFT)

        # プレビューキャンバス
        self.hsv_canvas = tk.Canvas(c_prev_in, bg="black", cursor="crosshair", highlightthickness=0)
        self.hsv_canvas.pack(fill=tk.BOTH, expand=True)
        self.hsv_canvas.bind("<Button-1>", self._on_hsv_canvas_click)
        Tooltip(self.hsv_canvas, "クリックした地点のピクセルのHSV値をスポイト取得します\nマスク表示時は「白」が該当判定色として認識されるピクセルです")

        # プレビュー下部ステータス表示
        f_hsv_info = tk.Frame(c_prev_in, bg=COLOR_BG_PANEL)
        f_hsv_info.pack(fill=tk.X, pady=(6, 0))
        self.lbl_hsv_clicked = tk.Label(
            f_hsv_info, text="クリック地点: 未選択", font=FONT_NORMAL,
            bg=COLOR_BG_INPUT, fg=COLOR_ACCENT, padx=6, pady=4
        )
        self.lbl_hsv_clicked.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))

        self.lbl_hsv_current = tk.Label(
            f_hsv_info, text="ROI平均: 計算中...", font=FONT_NORMAL,
            bg=COLOR_BG_INPUT, fg=COLOR_OK, padx=6, pady=4
        )
        self.lbl_hsv_current.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(4, 0))

        # --- 右側: スクロール可能 閾値スピンボックス群 ---
        scroll_right = self.create_scrollable_panel(f_right)

        c_col_out, c_col_in = create_card(scroll_right, "画面色 HSV 閾値設定")
        c_col_out.pack(fill=tk.X, pady=(0, 10))

        # 変数定義
        self.v_g_hmin = tk.IntVar(value=35)
        self.v_g_hmax = tk.IntVar(value=85)
        self.v_g_smin = tk.IntVar(value=50)
        self.v_g_smax = tk.IntVar(value=255)
        self.v_g_vmin = tk.IntVar(value=50)
        self.v_g_vmax = tk.IntVar(value=255)

        self.v_y_hmin = tk.IntVar(value=18)
        self.v_y_hmax = tk.IntVar(value=35)
        self.v_y_smin = tk.IntVar(value=70)
        self.v_y_smax = tk.IntVar(value=255)
        self.v_y_vmin = tk.IntVar(value=70)
        self.v_y_vmax = tk.IntVar(value=255)

        self.v_r_hmin = tk.IntVar(value=0)
        self.v_r_hmax = tk.IntVar(value=10)
        self.v_r_smin = tk.IntVar(value=70)
        self.v_r_smax = tk.IntVar(value=255)
        self.v_r_vmin = tk.IntVar(value=60)
        self.v_r_vmax = tk.IntVar(value=255)

        self.v_lost_frames = tk.IntVar(value=10)

        # 1. 緑色設定
        fg = tk.LabelFrame(c_col_in, text="緑色 判定範囲", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_OK, padx=10, pady=6)
        fg.pack(fill=tk.X, pady=4)
        self._add_range_row(fg, "色相:", self.v_g_hmin, self.v_g_hmax, 0, 180)
        self._add_range_row(fg, "彩度:", self.v_g_smin, self.v_g_smax, 0, 255)
        self._add_range_row(fg, "明度:", self.v_g_vmin, self.v_g_vmax, 0, 255)

        # 2. 黄色設定
        fy = tk.LabelFrame(c_col_in, text="黄色 判定範囲", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_WARNING, padx=10, pady=6)
        fy.pack(fill=tk.X, pady=4)
        self._add_range_row(fy, "色相:", self.v_y_hmin, self.v_y_hmax, 0, 180)
        self._add_range_row(fy, "彩度:", self.v_y_smin, self.v_y_smax, 0, 255)
        self._add_range_row(fy, "明度:", self.v_y_vmin, self.v_y_vmax, 0, 255)

        # 3. 赤色設定
        fr = tk.LabelFrame(c_col_in, text="赤色 判定範囲", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_NG, padx=10, pady=6)
        fr.pack(fill=tk.X, pady=4)
        self._add_range_row(fr, "色相:", self.v_r_hmin, self.v_r_hmax, 0, 180)
        self._add_range_row(fr, "彩度:", self.v_r_smin, self.v_r_smax, 0, 255)
        self._add_range_row(fr, "明度:", self.v_r_vmin, self.v_r_vmax, 0, 255)

        # 4. QR消失許容フレーム設定
        c_cyc_out, c_cyc_in = create_card(scroll_right, "QR消失判定設定")
        c_cyc_out.pack(fill=tk.X, pady=(0, 10))
        f_lost = tk.Frame(c_cyc_in, bg=COLOR_BG_PANEL)
        f_lost.pack(fill=tk.X, pady=4)
        tk.Label(f_lost, text="QR消失判定 許容フレーム:", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT)
        sp_lost = self._spinbox(f_lost, self.v_lost_frames, 1, 100, increment=1, width=6)
        sp_lost.pack(side=tk.LEFT, padx=6)
        tk.Label(f_lost, text="フレーム", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(side=tk.LEFT)
        Tooltip(sp_lost, "QRコードが一定フレーム以上読めなくなると持ち出し異常NGを出力します")

    def _toggle_hsv_freeze(self):
        """静止画キャプチャとリアルタイム映像の切り替え"""
        cur = self.v_hsv_freeze.get()
        self.v_hsv_freeze.set(not cur)
        if self.v_hsv_freeze.get():
            self.btn_hsv_freeze.configure(text="リアルタイムに戻す", bg=COLOR_OK, fg="black")
            # 現在のフレームを固定保存
            frame = self._get_hsv_target_frame()
            if frame is not None:
                self._hsv_freeze_frame = frame.copy()
        else:
            self.btn_hsv_freeze.configure(text="静止画キャプチャ", bg="#546E7A", fg="white")
            self._hsv_freeze_frame = None

    def _get_hsv_target_frame(self) -> Optional[np.ndarray]:
        """色判定対象カメラ (cam_2 または cam_4) の最新フレームを取得"""
        if self.app_instance and hasattr(self.app_instance, "last_frames"):
            with self.app_instance.camera_lock:
                # 調整対象ROIに応じてフレーム取得
                cid = "cam_4" if self.active_roi_key in ("cam_4_commit", "cam_4_color") else "cam_2"
                f = self.app_instance.last_frames.get(cid)
                if f is not None:
                    return f
                return self.app_instance.last_frames.get("cam_1")
        return None

    def _start_hsv_preview(self):
        self._update_hsv_preview()

    def _update_hsv_preview(self):
        if not self.winfo_exists():
            return

        try:
            sel_tab = self.notebook.select()
            if sel_tab == str(self.tab_color):
                frame = self._hsv_freeze_frame if self.v_hsv_freeze.get() else self._get_hsv_target_frame()
                if frame is not None:
                    self._draw_hsv_preview_to_canvas(frame)
        except Exception:
            pass

        self._hsv_timer_id = self.after(80, self._update_hsv_preview)

    def _draw_hsv_preview_to_canvas(self, frame: np.ndarray):
        cw = max(10, self.hsv_canvas.winfo_width())
        ch = max(10, self.hsv_canvas.winfo_height())
        fh, fw = frame.shape[:2]

        scale = min(cw / fw, ch / fh)
        nw = max(1, int(fw * scale))
        nh = max(1, int(fh * scale))
        ox = (cw - nw) // 2
        oy = (ch - nh) // 2
        self.hsv_draw_scale = (ox, oy, nw, nh, fw, fh)

        # ROI取得
        roi_key = "cam_4_color" if self.active_roi_key in ("cam_4_commit", "cam_4_color") else "cam_2_color"
        roi_color = self.temp_data.get("roi", {}).get(roi_key, {})

        # 色判定ROI領域の切り出し (extract_roi_image を使用)
        from .ocr_engine import extract_roi_image
        crop = extract_roi_image(frame, roi_color)

        mean_hsv = (0, 0, 0)
        if crop is not None and crop.size > 0:
            hsv_crop = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            m = cv2.mean(hsv_crop)[:3]
            mean_hsv = (round(m[0], 1), round(m[1], 1), round(m[2], 1))
            self.lbl_hsv_current.configure(
                text=f"ROI平均: H:{int(mean_hsv[0])} S:{int(mean_hsv[1])} V:{int(mean_hsv[2])}"
            )

        # マスクまたはカラー画像の生成
        target_col = self.v_hsv_target_color.get()
        if target_col == "green":
            hmin, hmax = self.v_g_hmin.get(), self.v_g_hmax.get()
            smin, smax = self.v_g_smin.get(), self.v_g_smax.get()
            vmin, vmax = self.v_g_vmin.get(), self.v_g_vmax.get()
        elif target_col == "yellow":
            hmin, hmax = self.v_y_hmin.get(), self.v_y_hmax.get()
            smin, smax = self.v_y_smin.get(), self.v_y_smax.get()
            vmin, vmax = self.v_y_vmin.get(), self.v_y_vmax.get()
        else: # red
            hmin, hmax = self.v_r_hmin.get(), self.v_r_hmax.get()
            smin, smax = self.v_r_smin.get(), self.v_r_smax.get()
            vmin, vmax = self.v_r_vmin.get(), self.v_r_vmax.get()

        hsv_full = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        if target_col == "red":
            mask1 = cv2.inRange(hsv_full, np.array([0, smin, vmin], dtype=np.uint8), np.array([hmax, smax, vmax], dtype=np.uint8))
            mask2 = cv2.inRange(hsv_full, np.array([180 - hmax, smin, vmin], dtype=np.uint8), np.array([180, smax, vmax], dtype=np.uint8))
            mask = cv2.bitwise_or(mask1, mask2)
        else:
            mask = cv2.inRange(hsv_full, np.array([hmin, smin, vmin], dtype=np.uint8), np.array([hmax, smax, vmax], dtype=np.uint8))

        if self.v_hsv_view_mode.get() == "mask":
            disp = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        else:
            disp = frame.copy()

        # ROI領域の枠線を描画
        pts = roi_color.get("points")
        if isinstance(pts, list) and len(pts) == 4:
            poly = np.array([[int(p[0]*fw), int(p[1]*fh)] for p in pts], dtype=np.int32)
            cv2.polylines(disp, [poly.reshape((-1, 1, 2))], True, (255, 255, 0), 2)
        else:
            rx1, ry1 = int(roi_color.get("rx1", 0)*fw), int(roi_color.get("ry1", 0)*fh)
            rx2, ry2 = int(roi_color.get("rx2", 1)*fw), int(roi_color.get("ry2", 1)*fh)
            cv2.rectangle(disp, (rx1, ry1), (rx2, ry2), (255, 255, 0), 2)

        resized = cv2.resize(disp, (nw, nh), interpolation=cv2.INTER_NEAREST)
        self._hsv_img_tk = ImageTk.PhotoImage(image=Image.fromarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)))

        self.hsv_canvas.delete("all")
        self.hsv_canvas.create_image(ox, oy, anchor="nw", image=self._hsv_img_tk)

    def _on_hsv_canvas_click(self, event):
        """キャンバスクリック地点のHSV値をスポイト取得"""
        if not hasattr(self, "hsv_draw_scale"):
            return
        ox, oy, nw, nh, fw, fh = self.hsv_draw_scale
        if nw <= 0 or nh <= 0:
            return

        cx = event.x - ox
        cy = event.y - oy
        if 0 <= cx < nw and 0 <= cy < nh:
            fx = int((cx / nw) * fw)
            fy = int((cy / nh) * fh)

            frame = self._hsv_freeze_frame if self.v_hsv_freeze.get() else self._get_hsv_target_frame()
            if frame is not None and 0 <= fy < frame.shape[0] and 0 <= fx < frame.shape[1]:
                pixel_bgr = frame[fy, fx]
                pixel_hsv = cv2.cvtColor(np.uint8([[pixel_bgr]]), cv2.COLOR_BGR2HSV)[0][0]
                h, s, v = int(pixel_hsv[0]), int(pixel_hsv[1]), int(pixel_hsv[2])
                self.lbl_hsv_clicked.configure(
                    text=f"クリック位置: H:{h}  S:{s}  V:{v}"
                )

    def _add_range_row(self, parent, label_text, var_min, var_max, from_, to):
        """1行に整然と [ラベル] [最小値] 〜 [最大値] を配置 (冗長な括弧表記なし)"""
        row = tk.Frame(parent, bg=COLOR_BG_PANEL)
        row.pack(fill=tk.X, pady=4)

        lbl = tk.Label(row, text=label_text, font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=10, anchor="w")
        lbl.pack(side=tk.LEFT)

        sp1 = self._spinbox(row, var_min, from_, to, increment=1, width=6)
        sp1.pack(side=tk.LEFT, padx=(0, 6))

        lbl_wave = tk.Label(row, text="〜", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB)
        lbl_wave.pack(side=tk.LEFT, padx=(0, 6))

        sp2 = self._spinbox(row, var_max, from_, to, increment=1, width=6)
        sp2.pack(side=tk.LEFT, padx=(0, 10))

    def _add_single_row(self, parent, label_text, var, from_, to, note=""):
        """1行に [ラベル] [値] [単位/説明] を配置"""
        row = tk.Frame(parent, bg=COLOR_BG_PANEL)
        row.pack(fill=tk.X, pady=4)

        lbl = tk.Label(row, text=label_text, font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=14, anchor="w")
        lbl.pack(side=tk.LEFT)

        sp = self._spinbox(row, var, from_, to, increment=1, width=6)
        sp.pack(side=tk.LEFT, padx=(0, 10))

        if note:
            lbl_note = tk.Label(row, text=note, font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB)
            lbl_note.pack(side=tk.LEFT)

    # -------------------------------------------------------------
    # タブ 4: 画素数・パフォーマンス設定
    # -------------------------------------------------------------
    def _build_tab_res(self):
        c_out, c_in = create_card(self.tab_res, "解像度・FPS・ラズパイ軽量化設定")
        c_out.pack(fill=tk.BOTH, expand=True)

        f_cap = tk.Frame(c_in, bg=COLOR_BG_PANEL)
        f_cap.pack(fill=tk.X, pady=8)
        tk.Label(f_cap, text="撮影解像度:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=22, anchor="w").pack(side=tk.LEFT)
        self.cb_cap_res = ttk.Combobox(f_cap, values=RES_OPTIONS, state="readonly", font=FONT_SET_VAL, width=20)
        self.cb_cap_res.pack(side=tk.LEFT, padx=10)
        self.cb_cap_res.bind("<<ComboboxSelected>>", self._mark_changed)
        Tooltip(self.cb_cap_res,
                "カメラからの撮影 (キャプチャ) 解像度を設定します\n"
                "高解像度ほど認識精度が上がりますが処理負荷が増します\n"
                "ラズパイでは 640x480 または 1280x720 を推奨します")

        f_prev = tk.Frame(c_in, bg=COLOR_BG_PANEL)
        f_prev.pack(fill=tk.X, pady=8)
        tk.Label(f_prev, text="プレビュー解像度:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=22, anchor="w").pack(side=tk.LEFT)
        self.cb_prev_res = ttk.Combobox(f_prev, values=RES_OPTIONS_PREVIEW, state="readonly", font=FONT_SET_VAL, width=20)
        self.cb_prev_res.pack(side=tk.LEFT, padx=10)
        self.cb_prev_res.bind("<<ComboboxSelected>>", self._mark_changed)
        Tooltip(self.cb_prev_res,
                "メイン画面に表示するプレビュー映像の解像度を設定します\n"
                "撮影解像度より低くすることでCPU負荷を軽減できます\n"
                "ラズパイでは 320x240 または 640x480 を推奨します")

        f_fps = tk.Frame(c_in, bg=COLOR_BG_PANEL)
        f_fps.pack(fill=tk.X, pady=8)
        tk.Label(f_fps, text="表示FPS:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=22, anchor="w").pack(side=tk.LEFT)
        self.cb_prev_fps = ttk.Combobox(f_fps, values=["1", "2", "5", "10", "15", "20", "25", "30"], state="readonly", font=FONT_SET_VAL, width=20)
        self.cb_prev_fps.pack(side=tk.LEFT, padx=10)
        self.cb_prev_fps.bind("<<ComboboxSelected>>", self._mark_changed)
        Tooltip(self.cb_prev_fps,
                "メイン画面のプレビュー映像更新レート (fps) を設定します\n"
                "低いほどCPU負荷が下がります\n"
                "ラズパイでは 5〜10fps を推奨します")

        f_ifps = tk.Frame(c_in, bg=COLOR_BG_PANEL)
        f_ifps.pack(fill=tk.X, pady=8)
        tk.Label(f_ifps, text="判定FPS:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=22, anchor="w").pack(side=tk.LEFT)
        self.cb_inspect_fps = ttk.Combobox(f_ifps, values=["1", "2", "3", "5", "10", "15", "20"], state="readonly", font=FONT_SET_VAL, width=20)
        self.cb_inspect_fps.pack(side=tk.LEFT, padx=10)
        self.cb_inspect_fps.bind("<<ComboboxSelected>>", self._mark_changed)
        Tooltip(self.cb_inspect_fps,
                "QRコード読取・OCR・画面色判定を行う頻度 (fps) を設定します\n"
                "表示FPSとは独立して動作します\n"
                "低いほどCPU負荷が下がります (ラズパイでは 2〜5fps を推奨)")

        # ラズパイ超軽量化設定 (カメラ取得FPS & MJPG圧縮)
        f_cfps = tk.Frame(c_in, bg=COLOR_BG_PANEL)
        f_cfps.pack(fill=tk.X, pady=8)
        tk.Label(f_cfps, text="カメラ取得FPS:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=22, anchor="w").pack(side=tk.LEFT)
        self.cb_cam_fps = ttk.Combobox(f_cfps, values=["1", "2", "3", "5", "10", "15", "20", "30"], state="readonly", font=FONT_SET_VAL, width=20)
        self.cb_cam_fps.pack(side=tk.LEFT, padx=10)
        self.cb_cam_fps.bind("<<ComboboxSelected>>", self._mark_changed)
        Tooltip(self.cb_cam_fps,
                "USBカメラからの画像キャプチャ間隔 (fps) を制限します\n"
                "4台のカメラを同時稼働する際は 5fps 前後に制限することで\n"
                "USBバス帯域とCPU負荷を大幅に削減できます (推奨: 3〜5fps)")

        f_mjpg = tk.Frame(c_in, bg=COLOR_BG_PANEL)
        f_mjpg.pack(fill=tk.X, pady=8)
        tk.Label(f_mjpg, text="MJPG圧縮キャプチャ:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=22, anchor="w").pack(side=tk.LEFT)
        chk_mjpg = tk.Checkbutton(
            f_mjpg, text="MJPG形式でキャプチャ",
            variable=self.v_use_mjpg, font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN,
            selectcolor=COLOR_BG_INPUT, command=self._mark_changed
        )
        chk_mjpg.pack(side=tk.LEFT, padx=10)
        Tooltip(chk_mjpg,
                "カメラからの映像取得をMJPG圧縮フォーマットで行います\n"
                "非圧縮(YUYV)と比較してUSB帯域を1/10以下に削減できるため、\n"
                "ラズパイにカメラを複数接続する際は必ずONにしてください")

    # -------------------------------------------------------------
    # タブ 5: システム設定 (GPIO出力・ブザー音・OCR・メンテナンス)
    # -------------------------------------------------------------
    def _build_tab_sys(self):
        scroll_sys = self.create_scrollable_panel(self.tab_sys)

        # 1. GPIO出力・信号時間設定
        c_gpio_out, c_gpio_in = create_card(scroll_sys, "GPIO出力・異常検知信号設定")
        c_gpio_out.pack(fill=tk.X, pady=(0, 10))

        # OK出力時間
        f_ok_time = tk.Frame(c_gpio_in, bg=COLOR_BG_PANEL)
        f_ok_time.pack(fill=tk.X, pady=6)
        tk.Label(f_ok_time, text="OK 出力時間:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=22, anchor="w").pack(side=tk.LEFT)
        ent_ok_time = tk.Entry(f_ok_time, textvariable=self.v_ok_out_time, font=FONT_SET_VAL, bg=COLOR_BG_INPUT, fg="white", width=8, justify="center")
        ent_ok_time.pack(side=tk.LEFT, padx=6)
        ent_ok_time.bind("<KeyRelease>", self._mark_changed)
        tk.Label(f_ok_time, text="秒", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(side=tk.LEFT, padx=4)
        Tooltip(ent_ok_time, "OK判定時にGPIO信号をONにする時間 (秒) を設定します (デフォルト: 0.5秒)")

        # NG出力時間
        f_ng_time = tk.Frame(c_gpio_in, bg=COLOR_BG_PANEL)
        f_ng_time.pack(fill=tk.X, pady=6)
        tk.Label(f_ng_time, text="NG 出力時間:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=22, anchor="w").pack(side=tk.LEFT)
        ent_ng_time = tk.Entry(f_ng_time, textvariable=self.v_ng_out_time, font=FONT_SET_VAL, bg=COLOR_BG_INPUT, fg="white", width=8, justify="center")
        ent_ng_time.pack(side=tk.LEFT, padx=6)
        ent_ng_time.bind("<KeyRelease>", self._mark_changed)
        tk.Label(f_ng_time, text="秒", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(side=tk.LEFT, padx=4)
        Tooltip(ent_ng_time, "NG判定時にGPIO信号をONにする時間 (秒)。空欄の場合はブザー停止ボタンが押されるまで保持されます")

        # NG保持
        f_ng_hold = tk.Frame(c_gpio_in, bg=COLOR_BG_PANEL)
        f_ng_hold.pack(fill=tk.X, pady=6)
        tk.Label(f_ng_hold, text="NG 信号保持モード:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=22, anchor="w").pack(side=tk.LEFT)
        chk_ng_hold = tk.Checkbutton(
            f_ng_hold, text="ブザー停止ボタンが押されるまでNG信号を保持する",
            variable=self.v_ng_out_hold, font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN,
            selectcolor=COLOR_BG_INPUT, command=self._mark_changed
        )
        chk_ng_hold.pack(side=tk.LEFT, padx=6)
        Tooltip(chk_ng_hold, "有効にすると、指定秒数に関わらず作業者が停止ボタンを押すまでNG出力を継続します")

        # 2. ブザー音声設定
        c_buz_out, c_buz_in = create_card(scroll_sys, "ブザー音声設定")
        c_buz_out.pack(fill=tk.X, pady=(0, 10))

        self._current_test_buzzer = None

        def _toggle_buzzer_test(var_path, btn):
            if self._current_test_buzzer == btn:
                if PYGAME_AVAILABLE:
                    try:
                        pygame.mixer.music.stop()
                    except Exception:
                        pass
                btn.configure(text="試聴", bg="#546E7A")
                self._current_test_buzzer = None
                return

            if self._current_test_buzzer is not None:
                try:
                    self._current_test_buzzer.configure(text="試聴", bg="#546E7A")
                except Exception:
                    pass
                if PYGAME_AVAILABLE:
                    try:
                        pygame.mixer.music.stop()
                    except Exception:
                        pass
                self._current_test_buzzer = None

            p = var_path.get().strip()
            if not p or not os.path.isfile(p):
                messagebox.showwarning("ファイル未選択", "有効な音声ファイル (wav/mp3) が指定されていません", parent=self)
                return

            if not PYGAME_AVAILABLE:
                messagebox.showwarning("pygame未対応", "pygame が利用できないため試聴できません", parent=self)
                return

            try:
                pygame.mixer.music.load(p)
                pygame.mixer.music.play()
                btn.configure(text="停止", bg=COLOR_NG)
                self._current_test_buzzer = btn
            except Exception as ex:
                messagebox.showerror("再生エラー", f"音声ファイルの再生に失敗しました:\n{ex}", parent=self)

        def _build_buzzer_row(parent, label_text, var_path, tooltip_text):
            row = tk.Frame(parent, bg=COLOR_BG_PANEL)
            row.pack(fill=tk.X, pady=6)
            tk.Label(row, text=label_text, font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=22, anchor="w").pack(side=tk.LEFT)
            ent = tk.Entry(row, textvariable=var_path, font=FONT_SET_VAL, bg=COLOR_BG_INPUT, fg="white", width=36)
            ent.pack(side=tk.LEFT, padx=6)
            ent.bind("<KeyRelease>", self._mark_changed)

            def _browse():
                ft = [("音声ファイル", "*.wav *.mp3 *.ogg"), ("すべてのファイル", "*.*")]
                p = filedialog.askopenfilename(title=f"{label_text} 音声ファイル選択", filetypes=ft, parent=self)
                if p:
                    var_path.set(p)
                    self._mark_changed()

            tk.Button(row, text="参照...", font=FONT_NORMAL, bg="#546E7A", fg="white", relief="flat", padx=8, command=_browse).pack(side=tk.LEFT, padx=3)

            btn_test = tk.Button(row, text="試聴", font=FONT_NORMAL, bg="#546E7A", fg="white", relief="flat", padx=8)
            btn_test.configure(command=lambda bp=var_path, b=btn_test: _toggle_buzzer_test(bp, b))
            btn_test.pack(side=tk.LEFT, padx=3)

            Tooltip(ent, tooltip_text)
            Tooltip(btn_test, "指定した音声ファイルを再生テストします")

        _build_buzzer_row(c_buz_in, "ライン 1 NG 音声:", self.v_buzzer_line1, "ライン1 (カメラ1/2) がNG判定時に再生される音声ファイル (.wav/.mp3)")
        _build_buzzer_row(c_buz_in, "ライン 2 NG 音声:", self.v_buzzer_line2, "ライン2 (カメラ3/4) がNG判定時に再生される音声ファイル (.wav/.mp3)")
        _build_buzzer_row(c_buz_in, "両ライン NG 音声:", self.v_buzzer_both, "ライン1・ライン2の双方が同時にNG判定時に再生される音声ファイル (.wav/.mp3)")

        # 3. システム・OCR・メンテナンス設定
        c_out, c_in = create_card(scroll_sys, "システム・外部ツール連携・本体メンテナンス")
        c_out.pack(fill=tk.X, pady=(0, 10))

        f_tess = tk.Frame(c_in, bg=COLOR_BG_PANEL)
        f_tess.pack(fill=tk.X, pady=8)
        tk.Label(f_tess, text="Tesseract-OCR パス:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=22, anchor="w").pack(side=tk.LEFT)
        self.ent_tess = tk.Entry(f_tess, font=FONT_SET_VAL, bg=COLOR_BG_INPUT, fg="white", width=36)
        self.ent_tess.pack(side=tk.LEFT, padx=6)
        self.ent_tess.bind("<KeyRelease>", self._mark_changed)

        def _browse_tess():
            ext = "*.exe" if sys.platform.startswith("win") else "*"
            ft = [("Tesseract Executable", ext), ("All Files", "*.*")]
            p = filedialog.askopenfilename(title="Tesseract実行ファイル選択", filetypes=ft, parent=self)
            if p:
                self.ent_tess.delete(0, tk.END)
                self.ent_tess.insert(0, p)
                self._mark_changed()

        tk.Button(f_tess, text="参照...", font=FONT_NORMAL, bg="#546E7A", fg="white", relief="flat", padx=8, command=_browse_tess).pack(side=tk.LEFT, padx=3)

        def _test_tess():
            cmd = self.ent_tess.get().strip()
            ok = setup_tesseract_path(cmd)
            if ok:
                messagebox.showinfo("Tesseract検証", "Tesseract-OCR は正常に利用可能です", parent=self)
            else:
                messagebox.showerror("Tesseract検証", "Tesseract-OCR が見つかりません。パスを確認してください", parent=self)

        tk.Button(f_tess, text="検証", font=FONT_NORMAL, bg="#546E7A", fg="white", relief="flat", padx=8, command=_test_tess).pack(side=tk.LEFT, padx=3)

        # パス自動設定 & 自動インストールボタン行
        f_tess_tools = tk.Frame(c_in, bg=COLOR_BG_PANEL)
        f_tess_tools.pack(fill=tk.X, pady=(2, 10))
        tk.Label(f_tess_tools, text="", width=22, bg=COLOR_BG_PANEL).pack(side=tk.LEFT)

        def _auto_detect_tess():
            candidates = []
            which_p = shutil.which("tesseract")
            if which_p:
                candidates.append(which_p)

            if sys.platform.startswith("win"):
                candidates.extend([
                    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                    os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
                    os.path.expanduser(r"~\AppData\Local\Tesseract-OCR\tesseract.exe"),
                ])
            else:
                candidates.extend([
                    "/usr/bin/tesseract",
                    "/usr/local/bin/tesseract",
                ])

            found = None
            for c in candidates:
                if c and os.path.isfile(c):
                    found = os.path.abspath(c)
                    break

            if found:
                self.ent_tess.delete(0, tk.END)
                self.ent_tess.insert(0, found)
                setup_tesseract_path(found)
                self._mark_changed()
                messagebox.showinfo("パス設定完了", f"Tesseract-OCR を検出してパスを設定しました:\n\n{found}", parent=self)
            else:
                messagebox.showwarning(
                    "未検出",
                    "PC内に Tesseract-OCR が見つかりませんでした。\n「OCR自動インストール」ボタンからインストールを行ってください。",
                    parent=self
                )

        btn_detect = tk.Button(
            f_tess_tools, text="パス自動設定", font=FONT_NORMAL,
            bg=COLOR_ACCENT, fg="black", relief="flat", padx=12, pady=4,
            command=_auto_detect_tess
        )
        btn_detect.pack(side=tk.LEFT, padx=(6, 8))
        Tooltip(btn_detect, "PC内にインストールされている Tesseract-OCR を自動検索してパスを設定します")

        def _auto_install_tess():
            if messagebox.askyesno(
                "OCR自動インストール",
                "Tesseract-OCR を自動インストールしますか？\n（管理者権限が必要となる場合があります）",
                parent=self
            ):
                self._run_tesseract_install()

        self.btn_install_tess = tk.Button(
            f_tess_tools, text="OCR自動インストール", font=FONT_NORMAL,
            bg="#2E7D32", fg="white", relief="flat", padx=12, pady=4,
            command=_auto_install_tess
        )
        self.btn_install_tess.pack(side=tk.LEFT, padx=4)
        Tooltip(self.btn_install_tess, "OSに応じたTesseract-OCRパッケージを自動ダウンロード・インストールします")

        f_sc = tk.Frame(c_in, bg=COLOR_BG_PANEL)
        f_sc.pack(fill=tk.X, pady=12)
        tk.Label(f_sc, text="起動スクリプト作成:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=22, anchor="w").pack(side=tk.LEFT)

        def _create_shortcut():
            try:
                desktop = os.path.join(os.path.expanduser("~"), "Desktop")
                if not os.path.exists(desktop):
                    os.makedirs(desktop, exist_ok=True)
                app_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
                py_exe = sys.executable

                if sys.platform.startswith("win"):
                    sc_path = os.path.join(desktop, "ECU書き込みサイクル判定システム起動.bat")
                    content = (
                        "@echo off\n"
                        "chcp 65001 > nul\n"
                        "title ECU書き込みサイクル判定システム\n"
                        f'cd /d "{app_dir}"\n'
                        f'"{py_exe}" main.py\n'
                        "if %errorlevel% neq 0 pause\n"
                    )
                    with open(sc_path, "w", encoding="utf-8") as f:
                        f.write(content)
                else:
                    sc_path = os.path.join(desktop, "ECU書き込みサイクル判定システム起動.sh")
                    content = (
                        "#!/bin/bash\n"
                        f'cd "{app_dir}"\n'
                        f'"{py_exe}" main.py\n'
                    )
                    with open(sc_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    try:
                        os.chmod(sc_path, 0o755)
                    except Exception:
                        pass

                messagebox.showinfo("作成完了", f"デスクトップに起動スクリプトを作成しました:\n\n{sc_path}", parent=self)
            except Exception as e:
                messagebox.showerror("作成失敗", f"エラーが発生しました:\n{e}", parent=self)

        btn_sc = tk.Button(f_sc, text="デスクトップに起動ファイルを作成", font=FONT_NORMAL, bg=COLOR_ACCENT, fg="white", relief="flat", padx=12, pady=4, command=_create_shortcut)
        btn_sc.pack(side=tk.LEFT, padx=10)
        Tooltip(btn_sc,
                "デスクトップに起動スクリプトを作成します\n"
                "Windows: .bat ファイル   Linux/Raspberry Pi: .sh ファイル\n"
                "ダブルクリックするとシステムが起動します")

        f_dt = tk.Frame(c_in, bg=COLOR_BG_PANEL)
        f_dt.pack(fill=tk.X, pady=12)
        tk.Label(f_dt, text="ラズパイ本体日時設定:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=22, anchor="w").pack(side=tk.LEFT)

        def _open_datetime():
            SystemDateTimeDialog(self)

        btn_dt = tk.Button(f_dt, text="ラズパイ本体の日時を設定", font=FONT_NORMAL, bg=COLOR_ACCENT, fg="white", relief="flat", padx=12, pady=4, command=_open_datetime)
        btn_dt.pack(side=tk.LEFT, padx=10)
        Tooltip(btn_dt, "Linux/Raspberry Piのシステム日時を手動同期・設定します")

    # -------------------------------------------------------------
    # 設定のロード & 保存
    # -------------------------------------------------------------
    def _load_values(self):
        # 1. 監視サイクル数
        mon = self.temp_data.get("system", {}).get("monitor_count", 1)
        self.v_monitor_count.set(mon)
        if hasattr(self, "f_cam34_group"):
            if mon == 2:
                self.f_cam34_group.pack(fill=tk.X, pady=6)
            else:
                self.f_cam34_group.pack_forget()

        # 2. カメラ 1〜4
        cams = self.temp_data.get("cameras", [])

        def _load_cam(idx_pos, cb, by_path_var, af_var, focus_var, sp_focus, def_idx):
            if len(cams) > idx_pos:
                c = cams[idx_pos]
                idx = c.get("index", def_idx)
                bp = c.get("by_path", "")
                by_path_var.set(bp)
                af_val = bool(c.get("autofocus", True))
                af_var.set(af_val)
                focus_var.set(int(c.get("focus", -1)))
                if sp_focus:
                    sp_focus.configure(state="disabled" if af_val else "normal")

                sel = -1
                if bp:
                    for i, d in enumerate(self.dev_list):
                        if d.get("by_path") == bp:
                            sel = i
                            break
                if sel == -1:
                    for i, d in enumerate(self.dev_list):
                        if d.get("index") == idx:
                            sel = i
                            break
                if sel != -1:
                    cb.current(sel)
                    by_path_var.set(self.dev_list[sel].get("by_path", ""))
                elif len(self.dev_labels) > idx_pos:
                    cb.current(idx_pos)
                    by_path_var.set(self.dev_list[idx_pos].get("by_path", ""))
                elif self.dev_labels:
                    cb.current(0)
                    by_path_var.set(self.dev_list[0].get("by_path", ""))

        _load_cam(0, self.cb_cam1, self.v_cam1_by_path, self.v_cam1_af, self.v_cam1_focus, getattr(self, "sp_cam1_focus", None), 0)
        _load_cam(1, self.cb_cam2, self.v_cam2_by_path, self.v_cam2_af, self.v_cam2_focus, getattr(self, "sp_cam2_focus", None), 1)
        _load_cam(2, self.cb_cam3, self.v_cam3_by_path, self.v_cam3_af, self.v_cam3_focus, getattr(self, "sp_cam3_focus", None), 2)
        _load_cam(3, self.cb_cam4, self.v_cam4_by_path, self.v_cam4_af, self.v_cam4_focus, getattr(self, "sp_cam4_focus", None), 3)

        self._update_roi_coords_label()

        # 3. 色閾値
        col = self.temp_data.get("color_thresholds", {})
        g = col.get("green", {})
        self.v_g_hmin.set(g.get("h_min", 35))
        self.v_g_hmax.set(g.get("h_max", 85))
        self.v_g_smin.set(g.get("s_min", 50))
        self.v_g_smax.set(g.get("s_max", 255))
        self.v_g_vmin.set(g.get("v_min", 50))
        self.v_g_vmax.set(g.get("v_max", 255))

        y = col.get("yellow", {})
        self.v_y_hmin.set(y.get("h_min", 18))
        self.v_y_hmax.set(y.get("h_max", 35))
        self.v_y_smin.set(y.get("s_min", 70))
        self.v_y_smax.set(y.get("s_max", 255))
        self.v_y_vmin.set(y.get("v_min", 70))
        self.v_y_vmax.set(y.get("v_max", 255))

        r = col.get("red", {})
        self.v_r_hmin.set(r.get("h_min1", 0))
        self.v_r_hmax.set(r.get("h_max1", 10))
        self.v_r_smin.set(r.get("s_min", 70))
        self.v_r_smax.set(r.get("s_max", 255))
        self.v_r_vmin.set(r.get("v_min", 60))
        self.v_r_vmax.set(r.get("v_max", 255))

        # 4. サイクル・FPS
        cyc = self.temp_data.get("cycle", {})
        self.v_lost_frames.set(cyc.get("lost_frames_threshold", 10))
        self.cb_inspect_fps.set(str(cyc.get("inspect_fps", 5)))

        stg = self.temp_data.get("storage", {})
        self.cb_cap_res.set(stg.get("capture_res", "1280x720"))
        self.cb_prev_res.set(stg.get("preview_res", "640x480"))
        self.cb_prev_fps.set(str(stg.get("preview_fps", 5)))

        # 5. パフォーマンス (カメラ取得FPS & MJPG)
        perf = self.temp_data.get("performance", {})
        cam_fps_val = perf.get("camera_fps", 5)
        self.v_camera_fps.set(cam_fps_val)
        self.cb_cam_fps.set(str(cam_fps_val))
        self.v_use_mjpg.set(bool(perf.get("use_mjpg", True)))

        # 6. GPIO & ブザー
        gpio_cfg = self.temp_data.get("gpio", {})
        self.v_ok_out_time.set(str(gpio_cfg.get("ok_output_time", "0.5")))
        self.v_ng_out_time.set(str(gpio_cfg.get("ng_output_time", "")))
        self.v_ng_out_hold.set(bool(gpio_cfg.get("ng_output_hold", False)))
        self.v_buzzer_line1.set(str(gpio_cfg.get("buzzer_path_line1", "")))
        self.v_buzzer_line2.set(str(gpio_cfg.get("buzzer_path_line2", "")))
        self.v_buzzer_both.set(str(gpio_cfg.get("buzzer_path_both", "")))

        # 7. OCR
        ocr = self.temp_data.get("ocr", {})
        self.ent_tess.delete(0, "end")
        self.ent_tess.insert(0, str(ocr.get("tesseract_cmd", "")))

        self.has_changes = False

    @staticmethod
    def _safe_range(v1, v2):
        i1, i2 = int(v1), int(v2)
        return min(i1, i2), max(i1, i2)

    def _save_and_close(self):
        try:
            def _get_idx(cb, default_idx):
                val = cb.get()
                if "[" in val and "]" in val:
                    try:
                        return int(val.split("[")[1].split("]")[0])
                    except Exception:
                        pass
                return default_idx

            mon_count = self.v_monitor_count.get()
            c1_idx = _get_idx(self.cb_cam1, 0)
            c2_idx = _get_idx(self.cb_cam2, 1)

            if c1_idx == c2_idx:
                messagebox.showerror("検証エラー", "カメラ1とカメラ2に同じデバイスが指定されています", parent=self)
                return

            if mon_count == 2:
                c3_idx = _get_idx(self.cb_cam3, 2)
                c4_idx = _get_idx(self.cb_cam4, 3)
                all_idx = [c1_idx, c2_idx, c3_idx, c4_idx]
                if len(all_idx) != len(set(all_idx)):
                    messagebox.showerror("検証エラー", "複数のカメラに同じデバイスが重複指定されています", parent=self)
                    return

            # 監視サイクル数
            self.temp_data.setdefault("system", {})["monitor_count"] = mon_count

            # カメラリスト初期化/拡張 (最大4台分確保)
            cams = self.temp_data.setdefault("cameras", [])
            while len(cams) < 4:
                idx_len = len(cams)
                cams.append({
                    "id": f"cam_{idx_len+1}",
                    "name": f"カメラ {idx_len+1}",
                    "index": idx_len,
                    "autofocus": True,
                    "focus": -1,
                    "fourcc": "MJPG"
                })

            cams[0].update({
                "index": c1_idx, "by_path": self.v_cam1_by_path.get(),
                "autofocus": bool(self.v_cam1_af.get()), "focus": int(self.v_cam1_focus.get())
            })
            cams[1].update({
                "index": c2_idx, "by_path": self.v_cam2_by_path.get(),
                "autofocus": bool(self.v_cam2_af.get()), "focus": int(self.v_cam2_focus.get())
            })
            if mon_count == 2:
                cams[2].update({
                    "index": c3_idx, "by_path": self.v_cam3_by_path.get(),
                    "autofocus": bool(self.v_cam3_af.get()), "focus": int(self.v_cam3_focus.get())
                })
                cams[3].update({
                    "index": c4_idx, "by_path": self.v_cam4_by_path.get(),
                    "autofocus": bool(self.v_cam4_af.get()), "focus": int(self.v_cam4_focus.get())
                })

            # 色閾値 (最小・最大の逆転防止・自動整流)
            gh_min, gh_max = self._safe_range(self.v_g_hmin.get(), self.v_g_hmax.get())
            gs_min, gs_max = self._safe_range(self.v_g_smin.get(), self.v_g_smax.get())
            gv_min, gv_max = self._safe_range(self.v_g_vmin.get(), self.v_g_vmax.get())
            self.temp_data["color_thresholds"]["green"] = {
                "h_min": gh_min, "h_max": gh_max,
                "s_min": gs_min, "s_max": gs_max,
                "v_min": gv_min, "v_max": gv_max
            }

            yh_min, yh_max = self._safe_range(self.v_y_hmin.get(), self.v_y_hmax.get())
            ys_min, ys_max = self._safe_range(self.v_y_smin.get(), self.v_y_smax.get())
            yv_min, yv_max = self._safe_range(self.v_y_vmin.get(), self.v_y_vmax.get())
            self.temp_data["color_thresholds"]["yellow"] = {
                "h_min": yh_min, "h_max": yh_max,
                "s_min": ys_min, "s_max": ys_max,
                "v_min": yv_min, "v_max": yv_max
            }

            rh_min, rh_max = self._safe_range(self.v_r_hmin.get(), self.v_r_hmax.get())
            rs_min, rs_max = self._safe_range(self.v_r_smin.get(), self.v_r_smax.get())
            rv_min, rv_max = self._safe_range(self.v_r_vmin.get(), self.v_r_vmax.get())
            self.temp_data["color_thresholds"]["red"] = {
                "h_min1": rh_min, "h_max1": rh_max,
                "h_min2": max(0, 180 - rh_max), "h_max2": 180,
                "s_min": rs_min, "s_max": rs_max,
                "v_min": rv_min, "v_max": rv_max
            }

            # サイクル・判定FPS設定
            self.temp_data["cycle"]["lost_frames_threshold"] = int(self.v_lost_frames.get())
            try:
                self.temp_data["cycle"]["inspect_fps"] = int(self.cb_inspect_fps.get())
            except (ValueError, TypeError):
                self.temp_data["cycle"]["inspect_fps"] = 5

            # 解像度・表示FPS
            self.temp_data["storage"]["capture_res"] = self.cb_cap_res.get()
            self.temp_data["storage"]["preview_res"] = self.cb_prev_res.get()
            try:
                self.temp_data["storage"]["preview_fps"] = int(self.cb_prev_fps.get())
            except (ValueError, TypeError):
                self.temp_data["storage"]["preview_fps"] = 5

            # パフォーマンス (カメラ取得FPS & MJPG)
            perf = self.temp_data.setdefault("performance", {})
            try:
                perf["camera_fps"] = int(self.cb_cam_fps.get())
            except (ValueError, TypeError):
                perf["camera_fps"] = 5
            perf["use_mjpg"] = bool(self.v_use_mjpg.get())

            # GPIO & ブザー
            gpio_cfg = self.temp_data.setdefault("gpio", {})
            try:
                gpio_cfg["ok_output_time"] = float(self.v_ok_out_time.get().strip())
            except ValueError:
                gpio_cfg["ok_output_time"] = 0.5
            gpio_cfg["ng_output_time"] = self.v_ng_out_time.get().strip()
            gpio_cfg["ng_output_hold"] = bool(self.v_ng_out_hold.get())
            gpio_cfg["buzzer_path_line1"] = self.v_buzzer_line1.get().strip()
            gpio_cfg["buzzer_path_line2"] = self.v_buzzer_line2.get().strip()
            gpio_cfg["buzzer_path_both"] = self.v_buzzer_both.get().strip()

            # OCR
            self.temp_data["ocr"]["tesseract_cmd"] = self.ent_tess.get().strip()

            # 設定実体に反映して保存
            self.settings.data.clear()
            self.settings.data.update(self.temp_data)
            self.settings.save_settings()

            if self.on_save_callback:
                self.on_save_callback()

            messagebox.showinfo("保存完了", "設定を保存しました", parent=self)
            self.destroy()
        except Exception as e:
            messagebox.showerror("保存エラー", f"設定の保存中にエラーが発生しました:\n{e}", parent=self)

    def _run_tesseract_install(self):
        """別スレッドで Tesseract-OCR の自動インストールを実行"""
        if hasattr(self, "btn_install_tess"):
            self.btn_install_tess.configure(state="disabled", text="インストール中...")

        def _worker():
            success = False
            msg = ""
            try:
                if sys.platform.startswith("win"):
                    cmd = ["winget", "install", "--id", "UB-Mannheim.TesseractOCR", "--accept-package-agreements", "--accept-source-agreements", "--silent"]
                    try:
                        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                        if res.returncode == 0:
                            success = True
                            msg = "winget による Tesseract-OCR のインストールが完了しました。"
                        else:
                            msg = f"winget 終了コード: {res.returncode}\n{res.stdout or res.stderr}"
                    except FileNotFoundError:
                        msg = "winget コマンドが見つかりませんでした。"
                else:
                    cmd1 = ["sudo", "apt-get", "update"]
                    subprocess.run(cmd1, capture_output=True, text=True, timeout=60)
                    cmd2 = ["sudo", "apt-get", "install", "-y", "tesseract-ocr", "tesseract-ocr-jpn"]
                    res2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=180)
                    if res2.returncode == 0:
                        success = True
                        msg = "apt による Tesseract-OCR のインストールが完了しました。"
                    else:
                        msg = f"apt 終了コード: {res2.returncode}\n{res2.stderr}"
            except Exception as ex:
                msg = f"インストール処理中にエラーが発生しました: {ex}"

            def _finish():
                if hasattr(self, "btn_install_tess") and self.btn_install_tess.winfo_exists():
                    self.btn_install_tess.configure(state="normal", text="OCR自動インストール")
                if success:
                    which_p = shutil.which("tesseract")
                    if not which_p and sys.platform.startswith("win"):
                        for p in [r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"]:
                            if os.path.isfile(p):
                                which_p = p
                                break
                    elif not which_p:
                        which_p = "/usr/bin/tesseract"

                    if which_p and os.path.isfile(which_p):
                        self.ent_tess.delete(0, tk.END)
                        self.ent_tess.insert(0, which_p)
                        setup_tesseract_path(which_p)
                        self._mark_changed()
                        messagebox.showinfo("インストール成功", f"{msg}\nパスを設定しました:\n{which_p}", parent=self)
                    else:
                        messagebox.showinfo("インストール完了", f"{msg}\n「パス自動設定」ボタンを押して確認してください。", parent=self)
                else:
                    messagebox.showerror(
                        "インストール失敗",
                        f"自動インストールを完了できませんでした:\n{msg}\n\n公式配布元（https://github.com/UB-Mannheim/tesseract/wiki）より手動インストールを行ってください。",
                        parent=self
                    )

            self.after(0, _finish)

        threading.Thread(target=_worker, daemon=True).start()

    def _show_help(self):
        from .widgets import HelpWindow
        help_data = {
            "監視システム構成・カメラ設定": (
                "■ 監視サイクル数\n"
                "・1サイクル監視: 1つの作業台をカメラ1とカメラ2で監視します。\n"
                "・2サイクル並列監視: 2つの作業台を完全並列で同時監視します。\n"
                "  ライン1: カメラ1 / カメラ2\n"
                "  ライン2: カメラ3 / カメラ4\n\n"
                "■ カメラフォーカス設定\n"
                "・「AF」をONにするとカメラ本体のオートフォーカスが動作します。\n"
                "・AFのチェックを外すと、スピンボックス (0〜1024) で手動ピント調整が可能です。\n"
                "・設定値は接続中のカメラに即時反映されるため、ROIタブや色判定タブの映像を見ながらピントを追い込めます。\n\n"
                "■ USB物理ポート記憶\n"
                "・Linux環境ではUSBの物理ポートパス (by-path) を自動記憶します。\n"
                "・再起動やUSBの抜き差しを行ってもカメラの割り当て順が入れ替わりません。"
            ),
            "読取・ROI設定 (台形補正)": (
                "ROI (Region of Interest) とは、画像認識・色判定を行う対象領域のことです。\n\n"
                "■ 矩形モード (通常)\n"
                "・プレビュー映像上でマウスをドラッグして矩形のROIエリアを指定します。\n"
                "・枠線: 赤枠 = QR / コミットOCRエリア、シアン枠 = 画面色判定エリア\n\n"
                "■ 4点台形補正モード (斜め設置カメラ対応)\n"
                "・コミット番号表示画面に対してカメラを斜め (約45度など) に設置している場合、\n"
                "  文字が台形に歪んでOCR認識精度が低下します。\n"
                "・「4点台形補正モード」をONにし、プレビュー上で「左上 → 右上 → 右下 → 左下」の順に\n"
                "  4頂点をクリックすると、透視変換で正面視点に補正してから高精度OCRを行います。\n"
                "・やり直す場合は「頂点クリア」ボタンを押してください。"
            ),
            "色判定設定 (リアルタイム・マスク・スポイト)": (
                "■ リアルタイム映像 & 二値化マスク表示\n"
                "・色判定タブでは、カメラ2 (または4) の映像をリアルタイム表示しながらHSV閾値を設定できます。\n"
                "・「二値化マスク表示」に切り替えると、指定したHSV閾値に合致する画素が白色、それ以外が黒色で表示されます。\n"
                "  画面の該当ランプ全体が綺麗に白く抜けるようにスライダーを調整してください。\n\n"
                "■ 静止画キャプチャ & スポイト機能\n"
                "・「静止画キャプチャ」ボタンを押すと、現在の映像を一時停止してゆっくり調整できます。\n"
                "・プレビュー画像上の目的の色（緑・黄・赤ランプ部）をクリックすると、\n"
                "  そのピクセルのHSV値 (H:色相, S:彩度, V:明度) が取得表示されます。\n"
                "  この数値を参考にスピンボックスを設定してください。"
            ),
            "サイクル動作フロー (新仕様)": (
                "■ 1. 待機中 (IDLE)\n"
                "・画面色が【緑色】の状態で新規QRコード（直近10回と異なるコード）を読み取るとサイクルが開始されます。\n"
                "・【重要】待機中に画面色が「緑色以外 (黄・赤・黒等)」の状態でQRを検出した場合、\n"
                "  開始手順異常として即座にNGエラーを発報します。\n"
                "・QRコード先頭4桁が「正解コミット番号」として保持されます。\n\n"
                "■ 2. 作業中 (黄色画面への遷移 & 即時コミット照合)\n"
                "・作業が始まって画面色が【黄色】に遷移したタイミングでコミットOCRを実行し、正解と照合します。\n"
                "・コミット番号が不一致の場合は即座にNG発報します。\n"
                "・黄色画面の期間中にQRコードが消失 (一定フレーム以上未検出) した場合、持ち出し異常NGとなります。\n"
                "・黄色画面から緑に戻るまでに照合が完了しなかった場合もNGとなります。\n\n"
                "■ 3. 完了 (OK / NG)\n"
                "・コミット照合が一致した状態で画面が【緑色】に復帰するとサイクル正常完了 (OK) となります。\n"
                "・OK判定時はOK出力信号を出し、CSVログに記録して設定秒数後に待機へ自動復帰します。\n"
                "・NG判定時はNG出力信号・ライン別ブザー音を発報し、停止ボタンが押されるまで停止します。"
            ),
            "GPIO信号出力・ブザー設定": (
                "■ GPIO信号出力時間\n"
                "・OK出力時間: OK判定時に信号ピンをONにする時間(秒)です (デフォルト: 0.5秒)。\n"
                "・NG出力時間: NG判定時に信号ピンをONにする時間(秒)です。空欄の場合は停止ボタンまで保持します。\n"
                "・NG信号保持モード: チェックを付けると、指定秒数に関わらず作業者が画面ボタンまたは\n"
                "  リセットボタンを押すまでNG出力を継続します。\n\n"
                "■ ライン別ブザー音声ファイル (.wav / .mp3)\n"
                "・ライン 1 NG: ライン1のみが異常停止した際にループ再生する音声です。\n"
                "・ライン 2 NG: ライン2のみが異常停止した際にループ再生する音声です。\n"
                "・両ライン NG: ライン1とライン2が双方同時に異常となった際に再生する音声です。\n"
                "・「試聴」ボタンを押すとその場で音声のテスト再生・音量確認が可能です。"
            ),
            "ラズパイ超軽量化・画素数設定": (
                "■ USB帯域枯渇対策 (MJPG圧縮キャプチャ)\n"
                "・ラズパイにUSBカメラを4台接続すると、通常の非圧縮(YUYV)ではUSBホストコントローラの帯域不足により\n"
                "  カメラが起動しない、またはフレーム落ちが発生します。\n"
                "・「MJPG圧縮キャプチャ」をONにすることで帯域を1/10以下に抑え、4台安定稼働を実現します。\n\n"
                "■ カメラ取得FPS & 判定FPSの最適化\n"
                "・カメラ取得FPS: カメラからの画像取得頻度 (推奨: 3〜5fps)。\n"
                "・判定FPS: QR読取・OCR・色判定の実行頻度 (推奨: 2〜5fps)。\n"
                "・コミットOCRは黄色画面中のみ間引き実行されるため、待機中や通常時のCPU負荷は最小限に抑えられます。\n"
                "・プレビュー解像度: メイン画面表示用解像度 (推奨: 320x240 または 640x480)。"
            ),
            "トラブルシューティング": (
                "■ カメラ映像が表示されない\n"
                "・「画素数設定」タブで「MJPG圧縮キャプチャ」がONになっているか確認してください。\n"
                "・USBハブ経由ではなく、セルフパワーハブを使用するか本体ポートへ分散接続してください。\n\n"
                "■ QRコードが読み取りにくい\n"
                "・「カメラ設定」タブでAFをOFFにし、手動フォーカススピンボックスでピントを調整してください。\n"
                "・ROIタブでQR読取枠 (赤枠) がQRコード全体を十分囲んでいるか確認してください。\n\n"
                "■ コミット番号OCRが認識しない\n"
                "・カメラが斜めになっている場合は「4点台形補正モード」で画面の四隅を正確に指定してください。\n"
                "・Tesseract-OCR パスが正しく設定されているか「システム設定」タブの「検証」で確認してください。\n\n"
                "■ ブザー音が鳴らない\n"
                "・指定した音声ファイルパスが存在するか確認し、「試聴」ボタンでテスト再生してください。"
            ),
        }
        HelpWindow(self, "詳細設定ヘルプ・ガイド", help_data)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py - メインアプリケーション (ECU書き込みサイクル判定システム)
inspection_app に完全準拠したGUIレイアウト & ダーク配色。
スレッド完全分離（UI描画ループと非同期検査ワーカースレッド）により超軽快な動作を実現。
"""

import os
import sys
import time
import csv
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

import cv2
import numpy as np
import tkinter as tk
from tkinter import messagebox, ttk
from PIL import Image, ImageTk

try:
    import pygame
    PYGAME_AVAILABLE = True
except ImportError:
    PYGAME_AVAILABLE = False

from .constants import (
    APP_NAME, VERSION,
    RESULTS_DIR, RESULTS_SUBDIR_CSV, RESULTS_SUBDIR_LOGS,
    COLOR_BG_MAIN, COLOR_BG_PANEL, COLOR_BG_INPUT,
    COLOR_TEXT_MAIN, COLOR_TEXT_SUB, COLOR_ACCENT,
    COLOR_OK, COLOR_NG, COLOR_WARNING, COLOR_BORDER,
    FONT_FAMILY, FONT_NORMAL, FONT_BOLD, FONT_LARGE, FONT_HUGE, FONT_SET_VAL,
    COLOR_STATE_GREEN, COLOR_STATE_YELLOW, COLOR_STATE_RED, COLOR_STATE_OTHER,
    DEFAULT_GPIO_OK_PIN_1, DEFAULT_GPIO_NG_PIN_1,
    DEFAULT_GPIO_OK_PIN_2, DEFAULT_GPIO_NG_PIN_2, DEFAULT_GPIO_RESET_PIN
)
from .hardware import DigitalInputDevice, OutputDevice, is_gpio_available, MockManager
from .settings import SettingsManager
from .qr_reader import QRReader
from .ocr_engine import OCREngine
from .color_detector import ColorDetector
from .cycle_manager import CycleManager, CycleStatus
from .widgets import create_card, Tooltip, HelpWindow, MockGPIOPanel
from .dialogs import SettingsDialog, resolve_camera_index_by_path, get_available_camera_list


class MainApplication:
    """メインアプリケーションクラス (1サイクル / 2サイクル並列監視対応)"""

    PREVIEW_W = 560
    PREVIEW_H = 420

    def __init__(self, is_test_mode: bool = False):
        self.is_test_mode = is_test_mode
        self.settings = SettingsManager()

        self.setup_dirs()
        self.setup_logging()

        # オーディオ初期化
        if PYGAME_AVAILABLE:
            try:
                pygame.mixer.init()
            except Exception as e:
                self.logger.warning(f"pygame.mixer初期化エラー: {e}")

        # 監視サイクル数 (1 または 2)
        self.monitor_count = int(self.settings.data.get("system", {}).get("monitor_count", 1))

        # エンジン初期化 (ライン1 & ライン2独立)
        self.qr_reader = QRReader()
        self.ocr_engine = OCREngine(self.settings.data.get("ocr", {}).get("tesseract_cmd"))
        self.color_detector_1 = ColorDetector(self.settings.data.get("color_thresholds"))
        self.color_detector_2 = ColorDetector(self.settings.data.get("color_thresholds"))
        self.color_detector = self.color_detector_1  # 互換用

        self.cycle_manager_1 = CycleManager(self.settings, line_id=1)
        self.cycle_manager_2 = CycleManager(self.settings, line_id=2)
        self.cycle_manager = self.cycle_manager_1    # 互換用

        self.running = True
        self.settings_open = False
        self.buzzer_active = False
        self.preview_fps = int(self.settings.data.get("storage", {}).get("preview_fps", 5))
        self.inspect_fps = int(self.settings.data.get("cycle", {}).get("inspect_fps", 5))
        self.camera_fps = int(self.settings.data.get("performance", {}).get("camera_fps", 5))

        self.caps: Dict[str, cv2.VideoCapture] = {}
        self.last_frames: Dict[str, np.ndarray] = {}
        self.camera_lock = threading.RLock()
        self.cam_running = True
        self.cam_threads: Dict[str, threading.Thread] = {}

        # 検査結果キャッシュ (スレッドセーフ受け渡し用)
        self.cache_lock = threading.Lock()
        self.csv_lock = threading.Lock()
        self.ocr_lock = threading.Lock()
        # ライン1キャッシュ
        self.cached_qr_text_1: Optional[str] = None
        self.cached_qr_pts_1: Optional[np.ndarray] = None
        self.cached_ocr_commit_1: Optional[str] = None
        self.cached_color_res_1: Dict[str, Any] = {"color": COLOR_STATE_OTHER}
        self.last_ocr_time_1 = 0.0

        # ライン2キャッシュ
        self.cached_qr_text_2: Optional[str] = None
        self.cached_qr_pts_2: Optional[np.ndarray] = None
        self.cached_ocr_commit_2: Optional[str] = None
        self.cached_color_res_2: Dict[str, Any] = {"color": COLOR_STATE_OTHER}
        self.last_ocr_time_2 = 0.0

        # 後方互換キャッシュ参照
        self.cached_qr_text = None
        self.cached_qr_pts = None
        self.cached_ocr_commit = None
        self.cached_color_res = {"color": COLOR_STATE_OTHER}
        self.commit_verified = False
        self.last_ocr_time = 0.0

        # NGオフ用タイマーID
        self.ng_off_timer_1 = None
        self.ng_off_timer_2 = None

        # ハードウェア
        self.setup_hardware()

        if self.is_test_mode:
            self._setup_test_stubs()
        else:
            self.setup_gui()
            if not is_gpio_available():
                self.setup_mock_panel()
            self.start_camera_threads()
            self.start_inspect_worker()
            self.update_previews()

    # -------------------------------------------------------------
    # 初期化・ディレクトリ・ログ
    # -------------------------------------------------------------
    def setup_dirs(self):
        stg = self.settings.data.get("storage", {})
        base_dir = Path(stg.get("results_dir", str(RESULTS_DIR)))
        self.results_dir = base_dir
        self.csv_dir = base_dir / RESULTS_SUBDIR_CSV
        self.log_dir = base_dir / RESULTS_SUBDIR_LOGS
        self.csv_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def setup_logging(self):
        log_file = self.log_dir / f"app_{datetime.now().strftime('%Y%m%d')}.log"
        self.logger = logging.getLogger("ECU_commit_verify")
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            fh = logging.FileHandler(str(log_file), encoding="utf-8")
            formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")
            fh.setFormatter(formatter)
            self.logger.addHandler(fh)
        self.logger.info(f"{APP_NAME} {VERSION} 起動")

    def setup_hardware(self):
        gpio_cfg = self.settings.data.get("gpio", {})
        ok_pin_1 = gpio_cfg.get("outputs", {}).get("ok", DEFAULT_GPIO_OK_PIN_1)
        ng_pin_1 = gpio_cfg.get("outputs", {}).get("ng", DEFAULT_GPIO_NG_PIN_1)
        ok_pin_2 = gpio_cfg.get("line2_outputs", {}).get("ok", DEFAULT_GPIO_OK_PIN_2)
        ng_pin_2 = gpio_cfg.get("line2_outputs", {}).get("ng", DEFAULT_GPIO_NG_PIN_2)
        reset_pin = gpio_cfg.get("reset_pin", DEFAULT_GPIO_RESET_PIN)

        self.out_ok_1 = OutputDevice(ok_pin_1)
        self.out_ng_1 = OutputDevice(ng_pin_1)
        self.out_ok_2 = OutputDevice(ok_pin_2)
        self.out_ng_2 = OutputDevice(ng_pin_2)

        # 後方互換用エイリアス
        self.out_ok = self.out_ok_1
        self.out_ng = self.out_ng_1

        self.in_reset = DigitalInputDevice(reset_pin, pull_up=True)
        self.in_reset.when_activated = self.handle_reset_signal

    def handle_reset_signal(self):
        """外部リセット信号受信"""
        if hasattr(self, "root") and hasattr(self.root, "after"):
            self.root.after(0, self.stop_buzzer)
        else:
            self.stop_buzzer()

    # -------------------------------------------------------------
    # テスト用スタブ
    # -------------------------------------------------------------
    def _setup_test_stubs(self):
        class _Stub:
            def __getattr__(self, name): return self
            def __call__(self, *a, **kw): return self
            def winfo_exists(self): return False
            def get(self): return ""
            def set(self, *a): pass

        self.root = _Stub()
        self.header = _Stub()
        self.lbl_status = _Stub()
        self.lbl_clock = _Stub()
        self.lbl_commit = _Stub()
        self.lbl_cam1_result = _Stub()
        self.lbl_cam2_result = _Stub()
        self.lbl_cam3_result = _Stub()
        self.lbl_cam4_result = _Stub()
        self.lbl_cam1_view = _Stub()
        self.lbl_cam2_view = _Stub()
        self.lbl_cam3_view = _Stub()
        self.lbl_cam4_view = _Stub()
        self.v_commit = _Stub()
        self.v_commit_1 = self.v_commit
        self.v_state = _Stub()
        self.v_state_1 = self.v_state
        self.lbl_state = _Stub()
        self.lbl_state_1 = self.lbl_state
        self.v_commit_2 = _Stub()
        self.v_state_2 = _Stub()
        self.lbl_state_2 = _Stub()
        self.lb_history = _Stub()

    # -------------------------------------------------------------
    # GUI構築 (inspection_app 完全準拠)
    # -------------------------------------------------------------
    def setup_gui(self):
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {VERSION}")
        self.root.geometry("1400x900")
        self.root.configure(bg=COLOR_BG_MAIN)
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # 白飛び防止二段階描画制御
        self.root.withdraw()

        # 最大化
        self.root.update_idletasks()
        try:
            self.root.state("zoomed")
        except tk.TclError:
            try:
                self.root.attributes("-zoomed", True)
            except tk.TclError:
                w = self.root.winfo_screenwidth()
                h = self.root.winfo_screenheight()
                self.root.geometry(f"{w}x{h}+0+0")

        # キーバインド
        self.root.bind("<space>", lambda e: self.stop_buzzer())
        self.root.bind("<b>", lambda e: self.stop_buzzer())
        self.root.bind("<B>", lambda e: self.stop_buzzer())
        self.root.bind("<Escape>", lambda e: self.stop_buzzer())
        self.root.bind("<s>", lambda e: self.open_settings())
        self.root.bind("<S>", lambda e: self.open_settings())
        self.root.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.is_fullscreen = False

        # --- ヘッダー (高さ80px, inspection_app 完全準拠) ---
        self.header = tk.Frame(self.root, bg=COLOR_BG_PANEL, height=80)
        self.header.pack(fill=tk.X)
        self.header.pack_propagate(False)

        self.lbl_status = tk.Label(
            self.header, text="システム待機中",
            font=FONT_LARGE, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT
        )
        self.lbl_status.pack(side=tk.LEFT, padx=30, pady=15)

        # 時計
        self.lbl_clock = tk.Label(
            self.header, text="", font=FONT_LARGE,
            bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN
        )
        self.lbl_clock.pack(side=tk.RIGHT, padx=30)
        self.update_clock()

        # ヘルプボタン
        btn_help = tk.Button(
            self.header, text="？", font=FONT_BOLD,
            bg=COLOR_BG_INPUT, fg=COLOR_ACCENT, relief="flat",
            width=3, command=self.show_main_help
        )
        btn_help.pack(side=tk.RIGHT, padx=10)
        Tooltip(btn_help, "操作ヘルプを表示します")

        # --- メインコンテンツ ---
        main = tk.Frame(self.root, bg=COLOR_BG_MAIN)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # 左側: カメラプレビューカード (inspection_app 完全準拠)
        card_title = "カメラプレビュー"
        self.v_frm_outer, v_frm_inner = create_card(main, card_title)
        self.v_frm_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.v_frm_outer.pack_propagate(False)

        self.v_frm = tk.Frame(v_frm_inner, bg=COLOR_BG_PANEL)
        self.v_frm.pack(fill=tk.BOTH, expand=True)

        cams = self.settings.data.get("cameras", [])
        cam_names = {c.get("id"): c.get("name", "").split(" (")[0].strip() or c.get("id") for c in cams}

        if self.monitor_count == 1:
            # 1ライン (2カメラ横並び)
            self.v_frm.rowconfigure(0, weight=1)
            self.v_frm.columnconfigure(0, weight=1, uniform="cam_col")
            self.v_frm.columnconfigure(1, weight=1, uniform="cam_col")

            # カメラ1枠
            f_cam1 = tk.Frame(self.v_frm, bg=COLOR_BG_PANEL, bd=1, relief="solid")
            f_cam1.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
            f_cam1_hdr = tk.Frame(f_cam1, bg=COLOR_BG_PANEL)
            f_cam1_hdr.pack(fill=tk.X, padx=8, pady=(4, 2))
            tk.Label(f_cam1_hdr, text=cam_names.get("cam_1", "カメラ 1"), font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT)
            self.lbl_cam1_result = tk.Label(f_cam1_hdr, text="QR: 待機中", font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT)
            self.lbl_cam1_result.pack(side=tk.RIGHT)
            self.lbl_cam1_view = tk.Label(f_cam1, bg="black")
            self.lbl_cam1_view.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

            # カメラ2枠
            f_cam2 = tk.Frame(self.v_frm, bg=COLOR_BG_PANEL, bd=1, relief="solid")
            f_cam2.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)
            f_cam2_hdr = tk.Frame(f_cam2, bg=COLOR_BG_PANEL)
            f_cam2_hdr.pack(fill=tk.X, padx=8, pady=(4, 2))
            tk.Label(f_cam2_hdr, text=cam_names.get("cam_2", "カメラ 2"), font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT)
            self.lbl_cam2_result = tk.Label(f_cam2_hdr, text="コミット: ----  画面色: ----", font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT)
            self.lbl_cam2_result.pack(side=tk.RIGHT)
            self.lbl_cam2_view = tk.Label(f_cam2, bg="black")
            self.lbl_cam2_view.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        else:
            # 2ライン並列 (4カメラ 2行2列)
            self.v_frm.rowconfigure(0, weight=1)
            self.v_frm.rowconfigure(1, weight=1)
            self.v_frm.columnconfigure(0, weight=1, uniform="cam_col")
            self.v_frm.columnconfigure(1, weight=1, uniform="cam_col")

            # カメラ1 (ライン1 QR)
            f_cam1 = tk.Frame(self.v_frm, bg=COLOR_BG_PANEL, bd=1, relief="solid")
            f_cam1.grid(row=0, column=0, sticky="nsew", padx=3, pady=3)
            f_cam1_hdr = tk.Frame(f_cam1, bg=COLOR_BG_PANEL)
            f_cam1_hdr.pack(fill=tk.X, padx=6, pady=2)
            tk.Label(f_cam1_hdr, text=cam_names.get("cam_1", "L1: カメラ 1"), font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT)
            self.lbl_cam1_result = tk.Label(f_cam1_hdr, text="QR: 待機中", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT)
            self.lbl_cam1_result.pack(side=tk.RIGHT)
            self.lbl_cam1_view = tk.Label(f_cam1, bg="black")
            self.lbl_cam1_view.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))

            # カメラ2 (ライン1 画面)
            f_cam2 = tk.Frame(self.v_frm, bg=COLOR_BG_PANEL, bd=1, relief="solid")
            f_cam2.grid(row=0, column=1, sticky="nsew", padx=3, pady=3)
            f_cam2_hdr = tk.Frame(f_cam2, bg=COLOR_BG_PANEL)
            f_cam2_hdr.pack(fill=tk.X, padx=6, pady=2)
            tk.Label(f_cam2_hdr, text=cam_names.get("cam_2", "L1: カメラ 2"), font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT)
            self.lbl_cam2_result = tk.Label(f_cam2_hdr, text="コミット: ----", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT)
            self.lbl_cam2_result.pack(side=tk.RIGHT)
            self.lbl_cam2_view = tk.Label(f_cam2, bg="black")
            self.lbl_cam2_view.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))

            # カメラ3 (ライン2 QR)
            f_cam3 = tk.Frame(self.v_frm, bg=COLOR_BG_PANEL, bd=1, relief="solid")
            f_cam3.grid(row=1, column=0, sticky="nsew", padx=3, pady=3)
            f_cam3_hdr = tk.Frame(f_cam3, bg=COLOR_BG_PANEL)
            f_cam3_hdr.pack(fill=tk.X, padx=6, pady=2)
            tk.Label(f_cam3_hdr, text=cam_names.get("cam_3", "L2: カメラ 3"), font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT)
            self.lbl_cam3_result = tk.Label(f_cam3_hdr, text="QR: 待機中", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT)
            self.lbl_cam3_result.pack(side=tk.RIGHT)
            self.lbl_cam3_view = tk.Label(f_cam3, bg="black")
            self.lbl_cam3_view.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))

            # カメラ4 (ライン2 画面)
            f_cam4 = tk.Frame(self.v_frm, bg=COLOR_BG_PANEL, bd=1, relief="solid")
            f_cam4.grid(row=1, column=1, sticky="nsew", padx=3, pady=3)
            f_cam4_hdr = tk.Frame(f_cam4, bg=COLOR_BG_PANEL)
            f_cam4_hdr.pack(fill=tk.X, padx=6, pady=2)
            tk.Label(f_cam4_hdr, text=cam_names.get("cam_4", "L2: カメラ 4"), font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT)
            self.lbl_cam4_result = tk.Label(f_cam4_hdr, text="コミット: ----", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT)
            self.lbl_cam4_result.pack(side=tk.RIGHT)
            self.lbl_cam4_view = tk.Label(f_cam4, bg="black")
            self.lbl_cam4_view.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))

        # 右側: 操作パネル (固定幅420px、inspection_app 完全準拠)
        pnl_outer, pnl = create_card(main, "操作パネル")
        pnl_outer.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))
        pnl_outer.config(width=420)
        pnl_outer.pack_propagate(False)

        if self.monitor_count == 1:
            # 1ライン用情報表示
            tk.Label(pnl, text="コミット番号", font=FONT_BOLD,
                     bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(pady=(6, 2))
            cf = tk.Frame(pnl, bg=COLOR_BG_PANEL)
            cf.pack(fill=tk.X, padx=10, pady=4)
            self.v_commit_1 = tk.StringVar(value="----")
            self.lbl_commit = tk.Label(
                cf, textvariable=self.v_commit_1,
                font=(FONT_FAMILY, 38, "bold"),
                bg=COLOR_BG_INPUT, fg=COLOR_ACCENT, pady=6
            )
            self.lbl_commit.pack(fill=tk.X)

            tk.Label(pnl, text="サイクル状態", font=FONT_BOLD,
                     bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(pady=(10, 2))
            self.v_state_1 = tk.StringVar(value="待機中")
            pat_frm = tk.Frame(pnl, bg=COLOR_BG_PANEL)
            pat_frm.pack(fill=tk.X, padx=10)
            self.lbl_state = tk.Label(
                pat_frm, textvariable=self.v_state_1, font=FONT_LARGE,
                bg=COLOR_BG_INPUT, fg=COLOR_ACCENT, pady=5
            )
            self.lbl_state.pack(side=tk.LEFT, fill=tk.X, expand=True)

            self.v_commit = self.v_commit_1
            self.v_state = self.v_state_1
            self.lbl_state_1 = self.lbl_state

        else:
            # 2ライン並列用情報表示 (ライン1 & ライン2独立カード)
            # ライン1 カード
            f_l1_card = tk.LabelFrame(pnl, text="ライン 1", font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT, padx=8, pady=4)
            f_l1_card.pack(fill=tk.X, padx=8, pady=(4, 6))

            f_l1_row = tk.Frame(f_l1_card, bg=COLOR_BG_PANEL)
            f_l1_row.pack(fill=tk.X)
            self.v_commit_1 = tk.StringVar(value="----")
            self.v_state_1 = tk.StringVar(value="待機中")

            tk.Label(f_l1_row, text="コミット:", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(side=tk.LEFT)
            self.lbl_commit_1 = tk.Label(f_l1_row, textvariable=self.v_commit_1, font=(FONT_FAMILY, 20, "bold"), bg=COLOR_BG_INPUT, fg=COLOR_ACCENT, padx=8)
            self.lbl_commit_1.pack(side=tk.LEFT, padx=6)

            self.lbl_state_1 = tk.Label(f_l1_card, textvariable=self.v_state_1, font=FONT_NORMAL, bg=COLOR_BG_INPUT, fg=COLOR_ACCENT, pady=2)
            self.lbl_state_1.pack(fill=tk.X, pady=(4, 2))

            # ライン2 カード
            f_l2_card = tk.LabelFrame(pnl, text="ライン 2", font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT, padx=8, pady=4)
            f_l2_card.pack(fill=tk.X, padx=8, pady=(2, 6))

            f_l2_row = tk.Frame(f_l2_card, bg=COLOR_BG_PANEL)
            f_l2_row.pack(fill=tk.X)
            self.v_commit_2 = tk.StringVar(value="----")
            self.v_state_2 = tk.StringVar(value="待機中")

            tk.Label(f_l2_row, text="コミット:", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(side=tk.LEFT)
            self.lbl_commit_2 = tk.Label(f_l2_row, textvariable=self.v_commit_2, font=(FONT_FAMILY, 20, "bold"), bg=COLOR_BG_INPUT, fg=COLOR_ACCENT, padx=8)
            self.lbl_commit_2.pack(side=tk.LEFT, padx=6)

            self.lbl_state_2 = tk.Label(f_l2_card, textvariable=self.v_state_2, font=FONT_NORMAL, bg=COLOR_BG_INPUT, fg=COLOR_ACCENT, pady=2)
            self.lbl_state_2.pack(fill=tk.X, pady=(4, 2))

            # 互換用
            self.v_commit = self.v_commit_1
            self.v_state = self.v_state_1
            self.lbl_state = self.lbl_state_1

        # 3. 操作ボタンエリア (下端固定配置、inspection_app 完全準拠)
        bottom_frm = tk.Frame(pnl, bg=COLOR_BG_PANEL)
        bottom_frm.pack(side=tk.BOTTOM, fill=tk.X, pady=(5, 5))

        # 詳細設定
        btn_settings = tk.Button(
            bottom_frm, text="詳細設定", font=FONT_BOLD, bg="#455A64",
            fg="white", height=2, relief="flat", command=self.open_settings
        )
        btn_settings.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)

        # ブザー停止
        self.btn_buzzer = tk.Button(
            bottom_frm, text="ブザー停止", font=FONT_BOLD, bg=COLOR_NG,
            fg="white", height=2, relief="flat", command=self.stop_buzzer
        )
        self.btn_buzzer.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(10, 5))

        # 履歴リセット・結果フォルダ
        hist_btn_frm = tk.Frame(bottom_frm, bg=COLOR_BG_PANEL)
        hist_btn_frm.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)

        tk.Button(
            hist_btn_frm, text="履歴リセット", font=FONT_NORMAL, bg="#546E7A",
            fg="white", relief="flat", command=self.clear_history
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        tk.Button(
            hist_btn_frm, text="結果フォルダ", font=FONT_NORMAL, bg="#546E7A",
            fg="white", relief="flat", command=self.open_results_folder
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        # 4. NG履歴エリア (残りスペース可変長、inspection_app 完全準拠)
        lbl_history_title = tk.Label(
            pnl, text="NG履歴", font=FONT_BOLD,
            bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB
        )
        lbl_history_title.pack(side=tk.TOP, pady=(10, 2))

        h_frm = tk.Frame(pnl, bg=COLOR_BG_PANEL)
        h_frm.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(0, 5))

        self.lb_history = tk.Listbox(
            h_frm, font=(FONT_FAMILY, 14),
            bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN,
            selectbackground=COLOR_ACCENT, selectforeground="black",
            relief="flat", bd=0
        )
        self.lb_history.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.lb_history.bind("<Double-Button-1>", self._on_history_detail)
        self.lb_history.bind("<Return>", self._on_history_detail)

        sb = tk.Scrollbar(h_frm, orient=tk.VERTICAL, command=self.lb_history.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.lb_history.config(yscrollcommand=sb.set)

        # 構築完了後に画面表示（白飛び防止）
        self.root.deiconify()

    def show_main_help(self):
        """メイン操作ヘルプ表示 (inspection_app 完全準拠)"""
        help_data = {
            "システム概要": (
                "本システムはカメラ2台を活用して作業サイクルの正否を自動判定・記録します。\n"
                "・カメラ 1: QRコードを読み取り、先頭4桁から正解コミット番号を取得して自動的にサイクルを開始します。\n"
                "・カメラ 2: 画面色シーケンス (緑→黄→緑) および4桁コミット番号のOCR照合を行います。"
            ),
            "サイクル開始条件": (
                "カメラ 1の読取枠内に新規QRコード（直近10回と異なるコード）が検出されると自動スタートします。\n"
                "過去10回と同一のコードは二重読み取り防止のためスキップされます。"
            ),
            "正常終了 (OK) 条件": (
                "1. 画面色が待機 (緑) から作業開始に伴い作業中 (黄) へ変化。\n"
                "2. 作業完了に伴い画面色が再び緑へ復帰。\n"
                "3. カメラ 2のコミット番号がQRコード先頭4桁と一致していること。"
            ),
            "異常終了 (NG) 条件": (
                "以下の場合に即座にNG判定となり、ブザー発報およびNG信号が出力されます。\n"
                "・画面色異常: サイクル途中で画面色が赤になった場合。\n"
                "・未完了持ち出し: 作業中 (黄) のままQRコードが画面外に持ち出された場合 (瞬間的な未読は除外されます)。\n"
                "・コミット不一致: カメラ 2のコミット番号がQRコード先頭4桁と異なる場合。"
            ),
            "操作・ショートカット": (
                "・ブザー停止: NG発報停止および待機復帰 (画面ボタンまたはSpaceキー / Bキー)\n"
                "・詳細設定: 各種設定ダイアログを開く (Sキー)\n"
                "・フルスクリーン: 全画面表示切り替え (F11キー)\n"
                "・履歴リセット: NG履歴リストのクリア\n"
                "・結果フォルダ: CSVログ保存先フォルダを開く"
            )
        }
        HelpWindow(self.root, "操作ヘルプ・ガイド", help_data)

    def setup_mock_panel(self):
        """Windowsデバッグ用仮想GPIOパネル"""
        try:
            self.mock_panel = MockGPIOPanel(self.root, self.settings)
        except Exception as e:
            self.logger.error(f"仮想GPIOパネル初期化エラー: {e}")

    # -------------------------------------------------------------
    # カメラ管理スレッド
    # -------------------------------------------------------------
    def _apply_camera_props(self, cap: cv2.VideoCapture, cam_cfg: dict):
        """カメラプロパティ (オートフォーカス / フォーカス値 / 露出等) を適用 (ClearView準拠)"""
        if cap is None or not cap.isOpened():
            return

        af = bool(cam_cfg.get("autofocus", True))
        try:
            cap.set(cv2.CAP_PROP_AUTOFOCUS, 1 if af else 0)
        except Exception:
            pass

        if not af:
            focus_val = int(cam_cfg.get("focus", -1))
            if focus_val != -1:
                try:
                    cap.set(cv2.CAP_PROP_FOCUS, focus_val)
                    self.logger.debug(f"{cam_cfg.get('id')}: 手動フォーカス {focus_val} を適用")
                except Exception as e:
                    self.logger.warning(f"フォーカス設定エラー ({cam_cfg.get('id')}): {e}")

    def _open_camera(self, cam_id: str, cam_cfg: dict) -> Optional[cv2.VideoCapture]:
        """設定に基づいてカメラをオープンし、解像度・MJPG・FPS・フォーカスを適用"""
        idx = cam_cfg.get("index", 0)
        by_path = cam_cfg.get("by_path", "")
        if by_path and sys.platform.startswith("linux"):
            resolved_idx = resolve_camera_index_by_path(by_path, idx)
            if resolved_idx != idx:
                self.logger.info(f"{cam_id} 物理ポートからインデックス解決: {idx} -> {resolved_idx} ({by_path})")
            idx = resolved_idx

        backend = cv2.CAP_DSHOW if sys.platform.startswith("win") else cv2.CAP_ANY
        cap = cv2.VideoCapture(idx, backend)
        if not cap.isOpened():
            return None

        # 1. MJPG圧縮キャプチャ (USB帯域節約 & ラズパイ4台接続時の必須対策)
        use_mjpg = bool(self.settings.data.get("performance", {}).get("use_mjpg", True))
        if use_mjpg:
            try:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            except Exception:
                pass

        # 2. 撮影解像度
        cap_res = self.settings.data.get("storage", {}).get("capture_res", "1280x720")
        try:
            w_str, h_str = cap_res.split("x")
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(w_str))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(h_str))
        except Exception:
            pass

        # 3. 取得FPS
        cam_fps = max(1, int(self.settings.data.get("performance", {}).get("camera_fps", 5)))
        try:
            cap.set(cv2.CAP_PROP_FPS, cam_fps)
        except Exception:
            pass

        # 4. フォーカスプロパティ適用
        self._apply_camera_props(cap, cam_cfg)
        return cap

    def start_camera_threads(self):
        # Linux環境: by_path が未設定の場合、現在接続されているカメラから自動補完・保存 (永続固定マイグレーション)
        if sys.platform.startswith("linux"):
            cams = self.settings.data.get("cameras", [])
            changed = False
            avail_cams = get_available_camera_list()
            for c in cams:
                if not c.get("by_path"):
                    c_idx = c.get("index", 0)
                    for dev in avail_cams:
                        if dev.get("index") == c_idx and dev.get("by_path"):
                            c["by_path"] = dev.get("by_path")
                            changed = True
                            self.logger.info(f"カメラ物理パス自動検出 ({c.get('id', 'cam')}: Index {c_idx} -> {dev.get('by_path')})")
                            break
            if changed:
                self.settings.save_settings()

        self.cam_running = True
        cams = self.settings.data.get("cameras", [])
        # monitor_count に応じて対象カメラを選定 (1ならcam_1, cam_2、2ならcam_1..cam_4)
        active_ids = ["cam_1", "cam_2"] if self.monitor_count == 1 else ["cam_1", "cam_2", "cam_3", "cam_4"]

        for c in cams:
            cid = c["id"]
            if cid in active_ids:
                t = threading.Thread(target=self._cam_worker, args=(cid,), daemon=True)
                self.cam_threads[cid] = t
                t.start()

    def _cam_worker(self, cam_id: str):
        cam_cfg = next((c for c in self.settings.data.get("cameras", []) if c.get("id") == cam_id), {})
        cap = self._open_camera(cam_id, cam_cfg)
        with self.camera_lock:
            self.caps[cam_id] = cap

        while self.running and self.cam_running:
            if cap is None or not cap.isOpened():
                time.sleep(1.0)
                cap = self._open_camera(cam_id, cam_cfg)
                with self.camera_lock:
                    self.caps[cam_id] = cap
                continue

            ret, frame = cap.read()
            if ret and frame is not None:
                with self.camera_lock:
                    self.last_frames[cam_id] = frame
            else:
                dummy = self._create_dummy_frame(cam_id)
                with self.camera_lock:
                    self.last_frames[cam_id] = dummy
                time.sleep(0.08)

            # カメラ取得FPSに基づくウェイト (CPUおよびUSBバス過負荷防止)
            cam_fps = max(1, int(self.settings.data.get("performance", {}).get("camera_fps", 5)))
            time.sleep(1.0 / cam_fps)

        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _create_dummy_frame(self, cam_id: str) -> np.ndarray:
        """カメラ未接続時のダミーフレーム"""
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        img[:] = (35, 35, 35)
        text = f"{cam_id} [NO SIGNAL]"
        cv2.putText(img, text, (140, 240), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (100, 100, 100), 2)
        return img

    # -------------------------------------------------------------
    # 非同期 検査・認識ワーカースレッド (CPU負荷分散・超高速化)
    # -------------------------------------------------------------
    def start_inspect_worker(self):
        t = threading.Thread(target=self._inspect_worker_loop, daemon=True)
        t.start()

    def _inspect_worker_loop(self):
        """バックグラウンドで高精度QRデコード・画面色判定・OCR照合を実行 (1または2ライン並列)"""
        while self.running:
            if self.settings_open:
                time.sleep(0.15)
                continue

            f1, f2, f3, f4 = None, None, None, None
            with self.camera_lock:
                if "cam_1" in self.last_frames:
                    f1 = self.last_frames["cam_1"].copy()
                if "cam_2" in self.last_frames:
                    f2 = self.last_frames["cam_2"].copy()
                if self.monitor_count == 2:
                    if "cam_3" in self.last_frames:
                        f3 = self.last_frames["cam_3"].copy()
                    if "cam_4" in self.last_frames:
                        f4 = self.last_frames["cam_4"].copy()

            # ライン1 検査ステップ
            if f1 is not None and f2 is not None:
                try:
                    self._run_inspection_step_line(1, f1, f2)
                except Exception as e:
                    self.logger.error(f"ライン1 検査処理エラー: {e}")

            # ライン2 検査ステップ (2サイクル並列時)
            if self.monitor_count == 2 and f3 is not None and f4 is not None:
                try:
                    self._run_inspection_step_line(2, f3, f4)
                except Exception as e:
                    self.logger.error(f"ライン2 検査処理エラー: {e}")

            fps = max(1, getattr(self, "inspect_fps", 5))
            time.sleep(1.0 / fps)

    def _run_inspection_step_line(self, line_id: int, frame_qr: np.ndarray, frame_screen: np.ndarray):
        """指定ラインのQR読取・画面色判定・黄色遷移時OCR照合・サイクルステートマシン処理"""
        roi_cfg = self.settings.data.get("roi", {})
        cycle_mgr = self.cycle_manager_1 if line_id == 1 else self.cycle_manager_2
        color_det = self.color_detector_1 if line_id == 1 else self.color_detector_2

        # ROIキー
        k_qr = "cam_1_qr" if line_id == 1 else "cam_3_qr"
        k_commit = "cam_2_commit" if line_id == 1 else "cam_4_commit"
        k_color = "cam_2_color" if line_id == 1 else "cam_4_color"

        roi_qr = roi_cfg.get(k_qr, {})
        roi_commit = roi_cfg.get(k_commit, {})
        roi_color = roi_cfg.get(k_color, {})

        # 1. QRコード読み取り (指定ROI内のみ)
        qr_text, _, qr_pts = self.qr_reader.read_qr(frame_qr, roi_qr)
        qr_detected = qr_text is not None

        # 2. 画面色判定 (指定ROI内のみ)
        debounce = self.settings.data.get("cycle", {}).get("color_debounce_frames", 2)
        color_res = color_det.detect_color(frame_screen, roi_color, debounce_frames=debounce)
        screen_color = color_res["color"]

        # 3. コミット番号OCR (黄色画面期間中かつ未照合時のみ間引き実行、それ以外は完全スキップで超軽量化)
        ocr_commit = None
        now = time.time()
        in_yellow = (cycle_mgr.status == CycleStatus.RUNNING_YELLOW)
        last_ocr = self.last_ocr_time_1 if line_id == 1 else self.last_ocr_time_2
        ocr_interval = float(self.settings.data.get("performance", {}).get("ocr_interval_sec", 0.3))

        if in_yellow and not cycle_mgr.commit_verified:
            if (now - last_ocr) >= ocr_interval:
                if line_id == 1: self.last_ocr_time_1 = now
                else: self.last_ocr_time_2 = now

                with self.ocr_lock:
                    ocr_res = self.ocr_engine.inspect_frame(frame_screen, roi_commit)
                val = ocr_res.get("commit_number")
                if val:
                    ocr_commit = val

        # 4. サイクルステートマシン処理
        old_status = cycle_mgr.status
        status, msg = cycle_mgr.process_frame(
            qr_detected, qr_text, screen_color, ocr_commit
        )

        # キャッシュ更新
        with self.cache_lock:
            if line_id == 1:
                self.cached_qr_text_1 = qr_text
                self.cached_qr_pts_1 = qr_pts
                self.cached_ocr_commit_1 = ocr_commit or cycle_mgr.ocr_commit
                self.cached_color_res_1 = color_res

                # 互換用
                self.cached_qr_text = qr_text
                self.cached_qr_pts = qr_pts
                self.cached_ocr_commit = self.cached_ocr_commit_1
                self.cached_color_res = color_res
            else:
                self.cached_qr_text_2 = qr_text
                self.cached_qr_pts_2 = qr_pts
                self.cached_ocr_commit_2 = ocr_commit or cycle_mgr.ocr_commit
                self.cached_color_res_2 = color_res

        # 状態変化があればメインスレッドでUI更新を呼び出し (引数を即時束縛してレースコンディション防止)
        if status != old_status:
            self.root.after(0, lambda lid=line_id, s=status, os_=old_status, m=msg: self._handle_status_change(lid, s, os_, m))

    # -------------------------------------------------------------
    # メインUI プレビューループ (描画のみ・FPS間引きで超軽快)
    # -------------------------------------------------------------
    def update_previews(self):
        if not self.running:
            return

        fps = max(1, getattr(self, "preview_fps", 5))
        delay_ms = max(20, int(1000 / fps))

        if self.settings_open:
            self.root.after(delay_ms, self.update_previews)
            return

        try:
            f1, f2, f3, f4 = None, None, None, None
            with self.camera_lock:
                if "cam_1" in self.last_frames:
                    f1 = self.last_frames["cam_1"]
                if "cam_2" in self.last_frames:
                    f2 = self.last_frames["cam_2"]
                if self.monitor_count == 2:
                    if "cam_3" in self.last_frames:
                        f3 = self.last_frames["cam_3"]
                    if "cam_4" in self.last_frames:
                        f4 = self.last_frames["cam_4"]

            roi_cfg = self.settings.data.get("roi", {})

            if self.monitor_count == 1:
                if f1 is not None and f2 is not None:
                    with self.cache_lock:
                        qr_text = self.cached_qr_text_1
                        qr_pts = self.cached_qr_pts_1
                        ocr_commit = self.cached_ocr_commit_1
                        color_res = self.cached_color_res_1

                    self._draw_previews_1line(
                        f1, f2, qr_text, qr_pts, ocr_commit, color_res,
                        roi_cfg.get("cam_1_qr", {}),
                        roi_cfg.get("cam_2_commit", {}),
                        roi_cfg.get("cam_2_color", {})
                    )
            else:
                with self.cache_lock:
                    q1, p1, o1, c1 = self.cached_qr_text_1, self.cached_qr_pts_1, self.cached_ocr_commit_1, self.cached_color_res_1
                    q2, p2, o2, c2 = self.cached_qr_text_2, self.cached_qr_pts_2, self.cached_ocr_commit_2, self.cached_color_res_2

                self._draw_previews_2line(f1, f2, f3, f4, q1, p1, o1, c1, q2, p2, o2, c2, roi_cfg)

        except Exception as e:
            self.logger.error(f"プレビュー更新エラー: {e}")

        self.root.after(delay_ms, self.update_previews)

    def _draw_previews_1line(
        self, f1: np.ndarray, f2: np.ndarray,
        qr_text: Optional[str], qr_pts: Optional[np.ndarray],
        ocr_commit: Optional[str], color_res: Dict[str, Any],
        roi_qr: dict, roi_commit: dict, roi_color: dict
    ):
        disp_f1 = f1.copy()
        disp_f2 = f2.copy()
        h1, w1 = disp_f1.shape[:2]
        h2, w2 = disp_f2.shape[:2]

        # カメラ1 ROI (赤枠)
        rx1, ry1 = int(roi_qr.get("rx1", 0)*w1), int(roi_qr.get("ry1", 0)*h1)
        rx2, ry2 = int(roi_qr.get("rx2", 1)*w1), int(roi_qr.get("ry2", 1)*h1)
        cv2.rectangle(disp_f1, (rx1, ry1), (rx2, ry2), (0, 0, 255), 2)

        # QRバウンディングボックス (緑枠)
        if qr_pts is not None and len(qr_pts) >= 4:
            pts_int = qr_pts.astype(np.int32).reshape((-1, 1, 2))
            cv2.polylines(disp_f1, [pts_int], True, (0, 255, 0), 3)

        # カメラ1 結果更新
        if qr_text:
            commit = self.cycle_manager_1.extract_commit_number(qr_text)
            disp_qr = commit if commit else qr_text.strip()[:4]
            self.lbl_cam1_result.configure(text=f"QR: {disp_qr}", fg=COLOR_OK)
        else:
            self.lbl_cam1_result.configure(text="QR: 待機中", fg=COLOR_TEXT_SUB)

        # カメラ2 コミットROI
        commit_pts = roi_commit.get("points")
        if isinstance(commit_pts, list) and len(commit_pts) == 4:
            poly_c = np.array([[int(p[0]*w2), int(p[1]*h2)] for p in commit_pts], dtype=np.int32)
            cv2.polylines(disp_f2, [poly_c.reshape((-1, 1, 2))], True, (0, 0, 255), 2)
        else:
            cx1, cy1 = int(roi_commit.get("rx1", 0)*w2), int(roi_commit.get("ry1", 0)*h2)
            cx2, cy2 = int(roi_commit.get("rx2", 1)*w2), int(roi_commit.get("ry2", 1)*h2)
            cv2.rectangle(disp_f2, (cx1, cy1), (cx2, cy2), (0, 0, 255), 2)

        # カメラ2 画面色ROI
        color_pts = roi_color.get("points")
        if isinstance(color_pts, list) and len(color_pts) == 4:
            poly_s = np.array([[int(p[0]*w2), int(p[1]*h2)] for p in color_pts], dtype=np.int32)
            cv2.polylines(disp_f2, [poly_s.reshape((-1, 1, 2))], True, (255, 255, 0), 2)
        else:
            sx1, sy1 = int(roi_color.get("rx1", 0)*w2), int(roi_color.get("ry1", 0)*h2)
            sx2, sy2 = int(roi_color.get("rx2", 1)*w2), int(roi_color.get("ry2", 1)*h2)
            cv2.rectangle(disp_f2, (sx1, sy1), (sx2, sy2), (255, 255, 0), 2)

        # カメラ2 結果更新
        sc = color_res.get("color", COLOR_STATE_OTHER)
        col_disp = {COLOR_STATE_GREEN: "緑", COLOR_STATE_YELLOW: "黄", COLOR_STATE_RED: "赤"}.get(sc, "----")
        col_fg = COLOR_OK if sc == COLOR_STATE_GREEN else (COLOR_WARNING if sc == COLOR_STATE_YELLOW else (COLOR_NG if sc == COLOR_STATE_RED else COLOR_TEXT_MAIN))
        c_txt = ocr_commit if ocr_commit else "----"
        self.lbl_cam2_result.configure(text=f"コミット: {c_txt}  画面色: {col_disp}", fg=col_fg)

        img1 = cv2.resize(disp_f1, (self.PREVIEW_W, self.PREVIEW_H), interpolation=cv2.INTER_NEAREST)
        img2 = cv2.resize(disp_f2, (self.PREVIEW_W, self.PREVIEW_H), interpolation=cv2.INTER_NEAREST)
        self.tk_img1 = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(img1, cv2.COLOR_BGR2RGB)))
        self.tk_img2 = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(img2, cv2.COLOR_BGR2RGB)))
        self.lbl_cam1_view.configure(image=self.tk_img1)
        self.lbl_cam2_view.configure(image=self.tk_img2)

    def _draw_previews_2line(
        self, f1, f2, f3, f4,
        q1, p1, o1, c1,
        q2, p2, o2, c2,
        roi_cfg: dict
    ):
        pw, ph = 380, 260
        # ライン1 (cam1, cam2)
        if f1 is not None and hasattr(self, "lbl_cam1_view"):
            d1 = f1.copy()
            h1, w1 = d1.shape[:2]
            r1 = roi_cfg.get("cam_1_qr", {})
            cv2.rectangle(d1, (int(r1.get("rx1",0)*w1), int(r1.get("ry1",0)*h1)), (int(r1.get("rx2",1)*w1), int(r1.get("ry2",1)*h1)), (0, 0, 255), 2)
            if p1 is not None and len(p1) >= 4:
                cv2.polylines(d1, [p1.astype(np.int32).reshape((-1, 1, 2))], True, (0, 255, 0), 2)
            c_disp = self.cycle_manager_1.extract_commit_number(q1) if q1 else (q1.strip()[:4] if q1 else "----")
            self.lbl_cam1_result.configure(text=f"QR: {c_disp}", fg=COLOR_OK if q1 else COLOR_TEXT_SUB)
            im1 = cv2.resize(d1, (pw, ph), interpolation=cv2.INTER_NEAREST)
            self.tk_img1 = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(im1, cv2.COLOR_BGR2RGB)))
            self.lbl_cam1_view.configure(image=self.tk_img1)

        if f2 is not None and hasattr(self, "lbl_cam2_view"):
            d2 = f2.copy()
            h2, w2 = d2.shape[:2]
            # コミットROI
            rc = roi_cfg.get("cam_2_commit", {})
            pts_c = rc.get("points")
            if isinstance(pts_c, list) and len(pts_c) == 4:
                cv2.polylines(d2, [np.array([[int(p[0]*w2), int(p[1]*h2)] for p in pts_c], dtype=np.int32).reshape((-1, 1, 2))], True, (0, 0, 255), 2)
            else:
                cv2.rectangle(d2, (int(rc.get("rx1",0)*w2), int(rc.get("ry1",0)*h2)), (int(rc.get("rx2",1)*w2), int(rc.get("ry2",1)*h2)), (0, 0, 255), 2)
            # 画面色ROI (シアン枠)
            r_col2 = roi_cfg.get("cam_2_color", {})
            pts_col2 = r_col2.get("points")
            if isinstance(pts_col2, list) and len(pts_col2) == 4:
                cv2.polylines(d2, [np.array([[int(p[0]*w2), int(p[1]*h2)] for p in pts_col2], dtype=np.int32).reshape((-1, 1, 2))], True, (255, 255, 0), 2)
            else:
                cv2.rectangle(d2, (int(r_col2.get("rx1",0)*w2), int(r_col2.get("ry1",0)*h2)), (int(r_col2.get("rx2",1)*w2), int(r_col2.get("ry2",1)*h2)), (255, 255, 0), 2)
            sc1 = c1.get("color", COLOR_STATE_OTHER)
            col_disp = {COLOR_STATE_GREEN: "緑", COLOR_STATE_YELLOW: "黄", COLOR_STATE_RED: "赤"}.get(sc1, "----")
            self.lbl_cam2_result.configure(text=f"コミット: {o1 or '----'}  色: {col_disp}", fg=COLOR_OK if sc1 == COLOR_STATE_GREEN else COLOR_WARNING)
            im2 = cv2.resize(d2, (pw, ph), interpolation=cv2.INTER_NEAREST)
            self.tk_img2 = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(im2, cv2.COLOR_BGR2RGB)))
            self.lbl_cam2_view.configure(image=self.tk_img2)

        # ライン2 (cam3, cam4)
        if f3 is not None and hasattr(self, "lbl_cam3_view"):
            d3 = f3.copy()
            h3, w3 = d3.shape[:2]
            r3 = roi_cfg.get("cam_3_qr", {})
            cv2.rectangle(d3, (int(r3.get("rx1",0)*w3), int(r3.get("ry1",0)*h3)), (int(r3.get("rx2",1)*w3), int(r3.get("ry2",1)*h3)), (0, 0, 255), 2)
            if p2 is not None and len(p2) >= 4:
                cv2.polylines(d3, [p2.astype(np.int32).reshape((-1, 1, 2))], True, (0, 255, 0), 2)
            c_disp2 = self.cycle_manager_2.extract_commit_number(q2) if q2 else (q2.strip()[:4] if q2 else "----")
            self.lbl_cam3_result.configure(text=f"QR: {c_disp2}", fg=COLOR_OK if q2 else COLOR_TEXT_SUB)
            im3 = cv2.resize(d3, (pw, ph), interpolation=cv2.INTER_NEAREST)
            self.tk_img3 = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(im3, cv2.COLOR_BGR2RGB)))
            self.lbl_cam3_view.configure(image=self.tk_img3)

        if f4 is not None and hasattr(self, "lbl_cam4_view"):
            d4 = f4.copy()
            h4, w4 = d4.shape[:2]
            # コミットROI
            rc4 = roi_cfg.get("cam_4_commit", {})
            pts_c4 = rc4.get("points")
            if isinstance(pts_c4, list) and len(pts_c4) == 4:
                cv2.polylines(d4, [np.array([[int(p[0]*w4), int(p[1]*h4)] for p in pts_c4], dtype=np.int32).reshape((-1, 1, 2))], True, (0, 0, 255), 2)
            else:
                cv2.rectangle(d4, (int(rc4.get("rx1",0)*w4), int(rc4.get("ry1",0)*h4)), (int(rc4.get("rx2",1)*w4), int(rc4.get("ry2",1)*h4)), (0, 0, 255), 2)
            # 画面色ROI (シアン枠)
            r_col4 = roi_cfg.get("cam_4_color", {})
            pts_col4 = r_col4.get("points")
            if isinstance(pts_col4, list) and len(pts_col4) == 4:
                cv2.polylines(d4, [np.array([[int(p[0]*w4), int(p[1]*h4)] for p in pts_col4], dtype=np.int32).reshape((-1, 1, 2))], True, (255, 255, 0), 2)
            else:
                cv2.rectangle(d4, (int(r_col4.get("rx1",0)*w4), int(r_col4.get("ry1",0)*h4)), (int(r_col4.get("rx2",1)*w4), int(r_col4.get("ry2",1)*h4)), (255, 255, 0), 2)
            sc2 = c2.get("color", COLOR_STATE_OTHER)
            col_disp2 = {COLOR_STATE_GREEN: "緑", COLOR_STATE_YELLOW: "黄", COLOR_STATE_RED: "赤"}.get(sc2, "----")
            self.lbl_cam4_result.configure(text=f"コミット: {o2 or '----'}  色: {col_disp2}", fg=COLOR_OK if sc2 == COLOR_STATE_GREEN else COLOR_WARNING)
            im4 = cv2.resize(d4, (pw, ph), interpolation=cv2.INTER_NEAREST)
            self.tk_img4 = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(im4, cv2.COLOR_BGR2RGB)))
            self.lbl_cam4_view.configure(image=self.tk_img4)

    def _handle_status_change(self, line_id_or_new: Any, new_status_or_old: Any = None, old_status_or_msg: Any = None, msg: Optional[str] = None):
        """ステータス遷移時の処理 (メインスレッドで実行)"""
        if isinstance(line_id_or_new, str):
            line_id = 1
            new_status = line_id_or_new
            old_status = new_status_or_old
            msg = old_status_or_msg
        else:
            line_id = int(line_id_or_new)
            new_status = str(new_status_or_old)
            old_status = str(old_status_or_msg)

        if msg:
            self.logger.info(f"[Line {line_id}] {msg}")

        cycle_mgr = self.cycle_manager_1 if line_id == 1 else self.cycle_manager_2
        v_commit = self.v_commit_1 if line_id == 1 else getattr(self, "v_commit_2", self.v_commit)
        v_state = self.v_state_1 if line_id == 1 else getattr(self, "v_state_2", self.v_state)
        lbl_state = self.lbl_state_1 if line_id == 1 else getattr(self, "lbl_state_2", self.lbl_state)
        col_det = self.color_detector_1 if line_id == 1 else self.color_detector_2

        if new_status == CycleStatus.RUNNING_START:
            col_det.reset()
            v_commit.set(cycle_mgr.target_commit or "----")
            v_state.set("サイクル開始 (緑)")
            lbl_state.configure(fg=COLOR_OK)
            self._trigger_ok_off(line_id)
            self._trigger_ng_off(line_id)

        elif new_status == CycleStatus.RUNNING_YELLOW:
            v_state.set("作業中 (黄)")
            lbl_state.configure(fg=COLOR_WARNING)

        elif new_status == CycleStatus.COMPLETED_OK:
            v_state.set("完了 (OK)")
            lbl_state.configure(fg=COLOR_OK)
            self._trigger_ok_output(line_id)
            self._save_csv_log(line_id, "OK", cycle_mgr.ok_message)

            disp_sec = self.settings.data.get("cycle", {}).get("result_display_sec", 2.5)
            self.root.after(int(disp_sec * 1000), lambda lid=line_id: self._auto_return_to_idle(lid))

        elif new_status == CycleStatus.FAILED_NG:
            v_state.set("異常停止 (NG)")
            lbl_state.configure(fg=COLOR_NG)
            self._trigger_ng_output(line_id)
            self._save_csv_log(line_id, "NG", cycle_mgr.ng_reason)
            if cycle_mgr.ng_history:
                self._add_ng_history_ui(line_id, cycle_mgr.ng_history[0])

        self._update_header_status()

    def _update_header_status(self):
        """ヘッダーステータスバーの統合更新"""
        s1 = self.cycle_manager_1.status
        s2 = self.cycle_manager_2.status if self.monitor_count == 2 else CycleStatus.IDLE

        if s1 == CycleStatus.FAILED_NG or s2 == CycleStatus.FAILED_NG:
            ng_lines = []
            if s1 == CycleStatus.FAILED_NG: ng_lines.append("L1")
            if s2 == CycleStatus.FAILED_NG: ng_lines.append("L2")
            self.set_ui_status(f"NG検出 ({'/'.join(ng_lines)})", COLOR_NG)
        elif s1 in (CycleStatus.RUNNING_START, CycleStatus.RUNNING_YELLOW) or s2 in (CycleStatus.RUNNING_START, CycleStatus.RUNNING_YELLOW):
            self.set_ui_status("サイクル実行中", COLOR_WARNING)
        elif s1 == CycleStatus.COMPLETED_OK or s2 == CycleStatus.COMPLETED_OK:
            self.set_ui_status("OK", COLOR_OK)
        else:
            self.set_ui_status("システム待機中", COLOR_ACCENT)

    def _trigger_ok_output(self, line_id: int):
        """OK出力信号 (タイマー制御)"""
        out_dev = self.out_ok_1 if line_id == 1 else getattr(self, "out_ok_2", self.out_ok_1)
        self._trigger_ng_off(line_id)
        if out_dev:
            try:
                out_dev.on()
                ok_sec = float(self.settings.data.get("gpio", {}).get("ok_output_time", 0.5))
                self.root.after(max(10, int(ok_sec * 1000)), lambda: out_dev.off() if out_dev else None)
            except Exception:
                pass

    def _trigger_ok_off(self, line_id: int):
        out_dev = self.out_ok_1 if line_id == 1 else getattr(self, "out_ok_2", self.out_ok_1)
        if out_dev:
            try: out_dev.off()
            except Exception: pass

    def _trigger_ng_output(self, line_id: int):
        """NG出力信号 & ブザー音声再生 (時間制御 & 保持対応)"""
        out_dev = self.out_ng_1 if line_id == 1 else getattr(self, "out_ng_2", self.out_ng_1)
        if out_dev:
            try: out_dev.on()
            except Exception: pass

        self.buzzer_active = True
        self._play_buzzer_for_state()

        gpio_cfg = self.settings.data.get("gpio", {})
        ng_hold = bool(gpio_cfg.get("ng_output_hold", False))
        ng_time_str = str(gpio_cfg.get("ng_output_time", "")).strip()

        # ng_hold が False かつ ng_time に有効な秒数が指定されている場合のみ自動OFFタイマー設定
        if not ng_hold and ng_time_str:
            try:
                ng_sec = float(ng_time_str)
                if ng_sec > 0:
                    def _timer_off():
                        if out_dev:
                            try: out_dev.off()
                            except Exception: pass
                        if line_id == 1: self.ng_off_timer_1 = None
                        else: self.ng_off_timer_2 = None
                        # タイマー満了時にブザー音を再評価 (他のラインもNGでなければ停止)
                        self._play_buzzer_for_state()
                    timer_id = self.root.after(max(10, int(ng_sec * 1000)), _timer_off)
                    if line_id == 1: self.ng_off_timer_1 = timer_id
                    else: self.ng_off_timer_2 = timer_id
            except ValueError:
                pass

    def _trigger_ng_off(self, line_id: int):
        out_dev = self.out_ng_1 if line_id == 1 else getattr(self, "out_ng_2", self.out_ng_1)
        if out_dev:
            try: out_dev.off()
            except Exception: pass

    def _play_buzzer_for_state(self):
        """ライン状態に応じたブザー音声ファイルのループ再生"""
        if not PYGAME_AVAILABLE:
            return

        gpio_cfg = self.settings.data.get("gpio", {})
        l1_ng = (self.cycle_manager_1.status == CycleStatus.FAILED_NG)
        l2_ng = (self.monitor_count == 2 and self.cycle_manager_2.status == CycleStatus.FAILED_NG)

        if not l1_ng and not l2_ng:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
            self.buzzer_active = False
            return

        if l1_ng and l2_ng:
            bp = gpio_cfg.get("buzzer_path_both") or gpio_cfg.get("buzzer_path_line1")
        elif l2_ng:
            bp = gpio_cfg.get("buzzer_path_line2") or gpio_cfg.get("buzzer_path_line1")
        else:
            bp = gpio_cfg.get("buzzer_path_line1")

        if bp and os.path.isfile(bp):
            try:
                pygame.mixer.music.load(bp)
                pygame.mixer.music.play(-1)
            except Exception as e:
                self.logger.warning(f"ブザー音声再生エラー: {e}")

    def _auto_return_to_idle(self, line_id: int = 1):
        """OK完了後の待機復帰"""
        cycle_mgr = self.cycle_manager_1 if line_id == 1 else self.cycle_manager_2
        col_det = self.color_detector_1 if line_id == 1 else self.color_detector_2
        v_commit = self.v_commit_1 if line_id == 1 else getattr(self, "v_commit_2", self.v_commit)
        v_state = self.v_state_1 if line_id == 1 else getattr(self, "v_state_2", self.v_state)
        lbl_state = self.lbl_state_1 if line_id == 1 else getattr(self, "lbl_state_2", self.lbl_state)

        if cycle_mgr.status == CycleStatus.COMPLETED_OK:
            cycle_mgr.reset_to_idle()
            col_det.reset()
            v_commit.set("----")
            v_state.set("待機中")
            lbl_state.configure(fg=COLOR_ACCENT)
            self._update_header_status()

    def _save_csv_log(self, line_id: int, result: str, detail: str):
        """CSVログ保存 (並列書き込み排他ロック保護)"""
        cycle_mgr = self.cycle_manager_1 if line_id == 1 else self.cycle_manager_2
        try:
            date_str = datetime.now().strftime("%Y%m%d")
            csv_path = self.csv_dir / f"inspection_results_{date_str}.csv"
            file_exists = csv_path.exists()

            with self.csv_lock:
                with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f)
                    if not file_exists:
                        writer.writerow(["日時", "ライン", "QRコード", "正解コミット", "読取コミット", "最終画面色", "判定結果", "判定詳細"])
                    writer.writerow([
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        f"ライン{line_id}",
                        cycle_mgr.target_commit or (cycle_mgr.current_qr[:4] if cycle_mgr.current_qr else "----"),
                        cycle_mgr.target_commit,
                        cycle_mgr.ocr_commit or "----",
                        cycle_mgr.last_screen_color,
                        result,
                        detail
                    ])
        except Exception as e:
            self.logger.error(f"CSV保存エラー: {e}")

    def _add_ng_history_ui(self, line_id: int, rec: Dict[str, Any]):
        """inspection_app 準拠の Listbox への履歴追加"""
        t = rec["time"].split(" ")[1] if " " in rec["time"] else rec["time"]
        commit = rec.get("target_commit", "----")
        reason = rec.get("reason", "")
        prefix = f"[L{line_id}]" if self.monitor_count == 2 else ""
        line_text = f"{prefix} [{t}] #{commit} {reason}"
        self.lb_history.insert(0, line_text)
        if self.lb_history.size() > 100:
            self.lb_history.delete(tk.END)

    def set_ui_status(self, text: str, color: str):
        """ステータス表示の更新 (inspection_app 完全準拠)"""
        self.lbl_status.configure(text=text, fg=color)
        if text == "システム待機中":
            bg_col = COLOR_BG_PANEL
        elif text == "OK":
            bg_col = "#1b4332"
        elif "NG" in text:
            bg_col = "#491212"
        else:
            bg_col = "#3a3010"

        self.header.configure(bg=bg_col)
        self.lbl_status.configure(bg=bg_col)
        self.lbl_clock.configure(bg=bg_col)

    def _on_history_detail(self, event=None):
        """NG履歴のダブルクリックまたはEnterキー押下時の詳細確認"""
        sel = self.lb_history.curselection()
        if not sel and event is not None and hasattr(event, "y"):
            idx = self.lb_history.nearest(event.y)
            if idx >= 0:
                sel = (idx,)
        if not sel:
            return
        idx = sel[0]
        try:
            line_text = self.lb_history.get(idx)
            if line_text:
                messagebox.showinfo("NG詳細情報", line_text, parent=self.root)
        except Exception:
            pass

    def stop_buzzer(self):
        """ブザー停止 / 全ラインの待機復帰"""
        if hasattr(self, "out_ng_1") and self.out_ng_1:
            try: self.out_ng_1.off()
            except Exception: pass
        if hasattr(self, "out_ng_2") and self.out_ng_2:
            try: self.out_ng_2.off()
            except Exception: pass

        if getattr(self, "ng_off_timer_1", None):
            try: self.root.after_cancel(self.ng_off_timer_1)
            except Exception: pass
            self.ng_off_timer_1 = None
        if getattr(self, "ng_off_timer_2", None):
            try: self.root.after_cancel(self.ng_off_timer_2)
            except Exception: pass
            self.ng_off_timer_2 = None

        if PYGAME_AVAILABLE:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass

        self.buzzer_active = False

        # NGだったラインを待機状態へ復帰
        for lid, cm, cd, vc, vs, ls in [
            (1, self.cycle_manager_1, self.color_detector_1, self.v_commit_1, self.v_state_1, self.lbl_state_1),
            (2, self.cycle_manager_2, self.color_detector_2, getattr(self, "v_commit_2", None), getattr(self, "v_state_2", None), getattr(self, "lbl_state_2", None))
        ]:
            if lid <= self.monitor_count and cm.status == CycleStatus.FAILED_NG:
                cm.reset_to_idle()
                cd.reset()
                if vc: vc.set("----")
                if vs: vs.set("待機中")
                if ls: ls.configure(fg=COLOR_ACCENT)

        self._update_header_status()
        self.logger.info("ブザー停止 & 待機復帰")

    def clear_history(self):
        """履歴リセット (inspection_app 準拠)"""
        if messagebox.askyesno("履歴リセット", "NG履歴をリセットしますか？", parent=self.root):
            self.cycle_manager_1.ng_history.clear()
            self.cycle_manager_2.ng_history.clear()
            self.lb_history.delete(0, tk.END)

    def open_results_folder(self):
        """結果フォルダ (inspection_app 準拠)"""
        try:
            path_str = str(self.csv_dir)
            if sys.platform.startswith("win"):
                os.startfile(path_str)
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path_str])
        except Exception as e:
            messagebox.showerror("フォルダオープン失敗", str(e), parent=self.root)

    def open_settings(self):
        if self.settings_open:
            return
        self.settings_open = True

        def _on_save():
            self.color_detector.update_thresholds(self.settings.data.get("color_thresholds"))
            self.ocr_engine.update_tesseract_cmd(self.settings.data.get("ocr", {}).get("tesseract_cmd"))
            self.preview_fps = int(self.settings.data.get("storage", {}).get("preview_fps", 10))
            self.inspect_fps = int(self.settings.data.get("cycle", {}).get("inspect_fps", 10))

        dlg = SettingsDialog(
            self.root, self.settings, self.qr_reader, self.ocr_engine, self.color_detector,
            on_save_callback=_on_save, app_instance=self
        )
        self.root.wait_window(dlg)
        self.settings_open = False

    def update_clock(self):
        now_str = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        self.lbl_clock.configure(text=now_str)
        self.root.after(1000, self.update_clock)

    def toggle_fullscreen(self):
        self.is_fullscreen = not self.is_fullscreen
        self.root.attributes("-fullscreen", self.is_fullscreen)

    def on_closing(self):
        self.running = False
        self.cam_running = False
        with self.camera_lock:
            for cid, cap in list(self.caps.items()):
                try:
                    if cap.isOpened():
                        cap.release()
                except Exception:
                    pass
            self.caps.clear()
        try:
            self.out_ok.off()
            self.out_ng.off()
        except Exception:
            pass
        self.root.destroy()

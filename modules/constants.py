#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
constants.py - フォント・カラー・パス定数 (inspection_app 完全準拠)
"""

import os
from pathlib import Path

# --- アプリケーション情報 ---
APP_NAME = "ECU書き込みサイクル判定システム"
VERSION = "v1.0.1"

# --- ファイル・パス ---
SETTINGS_FILE = "settings.json"
HISTORY_DIR = "history"
HISTORY_FILE_LINE1 = os.path.join(HISTORY_DIR, "history_line1.json")
HISTORY_FILE_LINE2 = os.path.join(HISTORY_DIR, "history_line2.json")
HISTORY_FILE = HISTORY_FILE_LINE1  # 後方互換用エイリアス
RESULTS_DIR = Path(os.path.expanduser("~")) / "results"
RESULTS_SUBDIR_CSV = "csv"
RESULTS_SUBDIR_LOGS = "logs"

# --- 有効なBCMピン番号 (Raspberry Pi 40ピンヘッダ) ---
VALID_BCM_PINS = {
    2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27
}
DEFAULT_GPIO_OK_PIN_1 = 16
DEFAULT_GPIO_NG_PIN_1 = 20
DEFAULT_GPIO_OK_PIN_2 = 12
DEFAULT_GPIO_NG_PIN_2 = 21
DEFAULT_GPIO_RESET_PIN = 23

# --- カメラフォーカス設定範囲 ---
FOCUS_MIN = 0
FOCUS_MAX = 1024
FOCUS_DEFAULT = -1

# --- 解像度オプション ---
RES_OPTIONS = ["320x240", "640x480", "1280x720", "1920x1080", "3840x2160"]
RES_OPTIONS_PREVIEW = ["プレビューなし", "320x240", "640x480", "1280x720"]

# --- フォント定義 (inspection_app 完全一致) ---
FONT_FAMILY = "Meiryo UI"
FONT_NORMAL = (FONT_FAMILY, 14)
FONT_BOLD = (FONT_FAMILY, 16, "bold")
FONT_LARGE = (FONT_FAMILY, 24, "bold")
FONT_HUGE = (FONT_FAMILY, 48, "bold")

# 設定画面用フォント (inspection_app 完全一致)
FONT_SET_TAB = (FONT_FAMILY, 18, "bold")
FONT_SET_LBL = (FONT_FAMILY, 16, "bold")
FONT_SET_VAL = (FONT_FAMILY, 16)
FONT_BTN_LARGE = (FONT_FAMILY, 16, "bold")

# --- カラーパレット (Dark Gray Theme - inspection_app 完全一致) ---
COLOR_BG_MAIN = "#2b2b2b"       # 背景全体
COLOR_BG_PANEL = "#3c3f41"      # パネル、カード背景
COLOR_BG_INPUT = "#45494a"      # 入力フィールド背景
COLOR_TEXT_MAIN = "#FFFFFF"     # メインテキスト
COLOR_TEXT_SUB = "#B0BEC5"      # サブテキスト
COLOR_ACCENT = "#4FC3F7"        # 水色（ボタン、強調）
COLOR_ACCENT_HOVER = "#81D4FA"

# ステータスカラー
COLOR_OK = "#66BB6A"            # マイルドな緑
COLOR_NG = "#FF5252"            # マイルドな赤
COLOR_NG_MUTED = "#B06666"      # さらに彩度を落とした赤
COLOR_WARNING = "#FFB74D"       # 視認性の高いオレンジ

# ボーダー色
COLOR_BORDER = "#505050"

# サイクル画面色定義名
COLOR_STATE_GREEN = "GREEN"
COLOR_STATE_YELLOW = "YELLOW"
COLOR_STATE_RED = "RED"
COLOR_STATE_OTHER = "OTHER"

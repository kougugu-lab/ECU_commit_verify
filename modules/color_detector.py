#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
color_detector.py - 画面色(緑/黄/赤)HSV解析およびデバウンス判定
設定画面から動的に閾値を変更可能。4点台形補正ROI対応。
"""

from typing import Dict, Any, Optional, Tuple
import cv2
import numpy as np

from .constants import (
    COLOR_STATE_GREEN, COLOR_STATE_YELLOW, COLOR_STATE_RED, COLOR_STATE_OTHER
)
from .ocr_engine import extract_roi_image


class ColorDetector:
    """指定ROI内の画面色（緑/黄/赤）をHSV色空間で解析・判定するクラス"""

    def __init__(self, thresholds: Optional[Dict[str, Any]] = None):
        self.thresholds = thresholds or self.default_thresholds()
        self.history = []
        self.confirmed_color = COLOR_STATE_OTHER

    @staticmethod
    def default_thresholds() -> Dict[str, Any]:
        return {
            "green": {
                "h_min": 35, "h_max": 85,
                "s_min": 50, "s_max": 255,
                "v_min": 50, "v_max": 255
            },
            "yellow": {
                "h_min": 18, "h_max": 35,
                "s_min": 70, "s_max": 255,
                "v_min": 70, "v_max": 255
            },
            "red": {
                "h_min1": 0, "h_max1": 10,
                "h_min2": 165, "h_max2": 180,
                "s_min": 70, "s_max": 255,
                "v_min": 60, "v_max": 255
            }
        }

    def update_thresholds(self, thresholds: Dict[str, Any]):
        self.thresholds = thresholds

    def reset(self):
        """デバウンス履歴および確定色状態をクリアする"""
        self.history = []
        self.confirmed_color = COLOR_STATE_OTHER

    def detect_color(
        self,
        frame: np.ndarray,
        roi: Optional[Dict[str, Any]] = None,
        debounce_frames: int = 2
    ) -> Dict[str, Any]:
        """
        フレーム内のROI領域から色を検出し、デバウンス判定を行う。
        roi に 'points' (4点) が指定されている場合は台形補正後の画像で判定する。
        戻り値:
        {
            "color": "GREEN" | "YELLOW" | "RED" | "OTHER",
            "raw_color": "GREEN" | "YELLOW" | "RED" | "OTHER",
            "hsv_mean": (h, s, v),
            "ratios": {"GREEN": float, "YELLOW": float, "RED": float},
            "cropped_img": np.ndarray
        }
        """
        res = {
            "color": self.confirmed_color,
            "raw_color": COLOR_STATE_OTHER,
            "hsv_mean": (0.0, 0.0, 0.0),
            "ratios": {
                COLOR_STATE_GREEN: 0.0,
                COLOR_STATE_YELLOW: 0.0,
                COLOR_STATE_RED: 0.0,
                COLOR_STATE_OTHER: 1.0
            },
            "cropped_img": None
        }

        if frame is None or frame.size == 0:
            return res

        crop, _ = extract_roi_image(frame, roi)

        if crop is None or crop.size == 0:
            return res

        res["cropped_img"] = crop
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        total_pixels = crop.shape[0] * crop.shape[1]

        # 平均HSV
        mean_hsv = cv2.mean(hsv)[:3]
        res["hsv_mean"] = (round(mean_hsv[0], 1), round(mean_hsv[1], 1), round(mean_hsv[2], 1))

        # マスク作成
        # 1. 緑
        g_cfg = self.thresholds.get("green", {})
        g_lower = np.array([g_cfg.get("h_min", 35), g_cfg.get("s_min", 50), g_cfg.get("v_min", 50)], dtype=np.uint8)
        g_upper = np.array([g_cfg.get("h_max", 85), g_cfg.get("s_max", 255), g_cfg.get("v_max", 255)], dtype=np.uint8)
        mask_g = cv2.inRange(hsv, g_lower, g_upper)

        # 2. 黄
        y_cfg = self.thresholds.get("yellow", {})
        y_lower = np.array([y_cfg.get("h_min", 18), y_cfg.get("s_min", 70), y_cfg.get("v_min", 70)], dtype=np.uint8)
        y_upper = np.array([y_cfg.get("h_max", 35), y_cfg.get("s_max", 255), y_cfg.get("v_max", 255)], dtype=np.uint8)
        mask_y = cv2.inRange(hsv, y_lower, y_upper)

        # 3. 赤 (色相が0付近と180付近の両端にまたがる)
        r_cfg = self.thresholds.get("red", {})
        r_lower1 = np.array([r_cfg.get("h_min1", 0), r_cfg.get("s_min", 70), r_cfg.get("v_min", 60)], dtype=np.uint8)
        r_upper1 = np.array([r_cfg.get("h_max1", 10), r_cfg.get("s_max", 255), r_cfg.get("v_max", 255)], dtype=np.uint8)
        r_lower2 = np.array([r_cfg.get("h_min2", 165), r_cfg.get("s_min", 70), r_cfg.get("v_min", 60)], dtype=np.uint8)
        r_upper2 = np.array([r_cfg.get("h_max2", 180), r_cfg.get("s_max", 255), r_cfg.get("v_max", 255)], dtype=np.uint8)
        mask_r1 = cv2.inRange(hsv, r_lower1, r_upper1)
        mask_r2 = cv2.inRange(hsv, r_lower2, r_upper2)
        mask_r = cv2.bitwise_or(mask_r1, mask_r2)

        ratio_g = float(np.count_nonzero(mask_g)) / total_pixels
        ratio_y = float(np.count_nonzero(mask_y)) / total_pixels
        ratio_r = float(np.count_nonzero(mask_r)) / total_pixels

        res["ratios"] = {
            COLOR_STATE_GREEN: round(ratio_g, 3),
            COLOR_STATE_YELLOW: round(ratio_y, 3),
            COLOR_STATE_RED: round(ratio_r, 3),
            COLOR_STATE_OTHER: round(max(0.0, 1.0 - (ratio_g + ratio_y + ratio_r)), 3)
        }

        # 判定（占有比率が最も高く、かつ閾値40%以上）
        dominant_color = COLOR_STATE_OTHER
        max_ratio = 0.40  # 最低40%以上のピクセル占有率を要求
        for c_name, ratio in [(COLOR_STATE_RED, ratio_r), (COLOR_STATE_YELLOW, ratio_y), (COLOR_STATE_GREEN, ratio_g)]:
            # 赤色判定を最優先（安全重視）
            if ratio > max_ratio:
                dominant_color = c_name
                max_ratio = ratio

        res["raw_color"] = dominant_color

        # デバウンス処理
        self.history.append(dominant_color)
        if len(self.history) > max(1, debounce_frames):
            self.history.pop(0)

        # 直近 debounce_frames フレームが全て同一色なら確定
        if len(self.history) >= debounce_frames and len(set(self.history)) == 1:
            self.confirmed_color = self.history[0]

        res["color"] = self.confirmed_color
        return res

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ocr_engine.py - 画像前処理・コミット番号(数字4桁)OCR読み取りエンジン
04P_commit_verify 準拠の実装。
"""

import os
import shutil
import re
from typing import Tuple, Optional, Dict, Any, List

import cv2
import numpy as np

try:
    import pytesseract
    PYTESSERACT_AVAILABLE = True
except ImportError:
    PYTESSERACT_AVAILABLE = False


def setup_tesseract_path(custom_cmd: Optional[str] = None) -> bool:
    """Tesseractの実行パスを設定・確認する"""
    if not PYTESSERACT_AVAILABLE:
        return False

    if custom_cmd and os.path.isfile(custom_cmd):
        pytesseract.pytesseract.tesseract_cmd = custom_cmd
        return True

    # Windowsの代表的なインストール候補
    default_win_paths = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
    ]
    for p in default_win_paths:
        if os.path.isfile(p):
            pytesseract.pytesseract.tesseract_cmd = p
            return True

    if shutil.which("tesseract"):
        return True

    return False


def order_points(pts: np.ndarray) -> np.ndarray:
    """4点を [左上, 右上, 右下, 左下] の順序に整流する"""
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]  # 左上: x + y が最小
    rect[2] = pts[np.argmax(s)]  # 右下: x + y が最大

    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]  # 右上: y - x が最小
    rect[3] = pts[np.argmax(diff)]  # 左下: y - x が最大
    return rect


def extract_roi_image(
    img: np.ndarray,
    roi: Optional[Dict[str, Any]]
) -> Tuple[np.ndarray, Optional[np.ndarray]]:
    """
    フレーム画像からROI領域を抽出する。
    roi に 'points' (4点正規化座標 [[x1, y1], [x2, y2], [x3, y3], [x4, y4]]) が指定されている場合は
    4点台形補正 (透視変換 / Perspective Transform) を行い正面化画像を返す。
    指定されていない場合は従来の rx1, ry1, rx2, ry2 による矩形クロップを返す。
    戻り値: (正面化またはクロップされた画像, 4点の実ピクセル座標 [4, 2] または None)
    """
    if img is None or img.size == 0:
        return np.array([]), None

    h_orig, w_orig = img.shape[:2]
    if not roi:
        return img.copy(), None

    points = roi.get("points")
    if points and len(points) == 4:
        try:
            raw_pts = np.array([
                [float(p[0]) * w_orig, float(p[1]) * h_orig] for p in points
            ], dtype=np.float32)
            rect = order_points(raw_pts)
            (tl, tr, br, bl) = rect

            width_a = np.hypot(br[0] - bl[0], br[1] - bl[1])
            width_b = np.hypot(tr[0] - tl[0], tr[1] - tl[1])
            max_w = max(int(max(width_a, width_b)), 16)

            height_a = np.hypot(tr[0] - br[0], tr[1] - br[1])
            height_b = np.hypot(tl[0] - bl[0], tl[1] - bl[1])
            max_h = max(int(max(height_a, height_b)), 16)

            dst = np.array([
                [0, 0],
                [max_w - 1, 0],
                [max_w - 1, max_h - 1],
                [0, max_h - 1]
            ], dtype=np.float32)

            M = cv2.getPerspectiveTransform(rect, dst)
            warped = cv2.warpPerspective(img, M, (max_w, max_h), flags=cv2.INTER_LINEAR)
            return warped, rect
        except Exception:
            pass

    # 従来の矩形クロップ
    rx1 = float(roi.get("rx1", 0.0)) * w_orig
    ry1 = float(roi.get("ry1", 0.0)) * h_orig
    rx2 = float(roi.get("rx2", 1.0)) * w_orig
    ry2 = float(roi.get("ry2", 1.0)) * h_orig

    x1 = max(0, min(int(min(rx1, rx2)), w_orig - 1))
    y1 = max(0, min(int(min(ry1, ry2)), h_orig - 1))
    x2 = max(x1 + 1, min(int(max(rx1, rx2)), w_orig))
    y2 = max(y1 + 1, min(int(max(ry1, ry2)), h_orig))

    cropped = img[y1:y2, x1:x2].copy()
    box_pts = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], dtype=np.float32)
    return cropped, box_pts


class OCREngine:
    """コミット番号（数字4桁）のOCR読み取りおよび照合を行うクラス"""

    def __init__(self, tesseract_cmd: Optional[str] = None):
        self.is_ready = setup_tesseract_path(custom_cmd=tesseract_cmd)

    def update_tesseract_cmd(self, custom_cmd: Optional[str] = None):
        self.is_ready = setup_tesseract_path(custom_cmd=custom_cmd)

    def preprocess_image(
        self,
        img: np.ndarray,
        roi: Optional[Dict[str, Any]] = None,
        kernel_size: int = 2,
        margin: int = 20,
        enable_tight_crop: bool = False,
        enable_clahe: bool = True
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        OCR用画像前処理 (台形補正/クロップ + CLAHEコントラスト強調 + 大津の二値化 + モルフォロジー + 外周サンプリング反転)
        """
        if img is None or img.size == 0:
            return np.array([]), np.array([])

        trimmed, _ = extract_roi_image(img, roi)

        if trimmed.size == 0 or trimmed.shape[0] < 3 or trimmed.shape[1] < 3:
            return trimmed, np.array([])

        # グレースケール化
        gray = cv2.cvtColor(trimmed, cv2.COLOR_BGR2GRAY)

        # コントラスト強調 (CLAHE)
        if enable_clahe:
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            gray = clahe.apply(gray)

        # 大津の二値化
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # ノイズ除去 (オープニング処理)
        if kernel_size > 0:
            k = np.ones((kernel_size, kernel_size), np.uint8)
            binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, k)

        # 外周エッジサンプリングによる白黒反転判定
        bh, bw = binary.shape[:2]
        pad = max(1, min(4, bh // 8, bw // 8))
        top_edge = binary[:pad, :]
        bottom_edge = binary[-pad:, :]
        left_edge = binary[:, :pad]
        right_edge = binary[:, -pad:]
        border_pixels = np.concatenate([top_edge.ravel(), bottom_edge.ravel(), left_edge.ravel(), right_edge.ravel()])
        border_black_count = np.count_nonzero(border_pixels == 0)

        if border_black_count > (len(border_pixels) / 2):
            processed = cv2.bitwise_not(binary)
        else:
            processed = binary

        # タイトクロップ
        if enable_tight_crop:
            inv = cv2.bitwise_not(processed)
            contours, _ = cv2.findContours(inv, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            valid_boxes = []
            for cnt in contours:
                area = cv2.contourArea(cnt)
                if area > 15:
                    bx, by, bw_c, bh_c = cv2.boundingRect(cnt)
                    if bw_c < bw * 0.95 or bh_c < bh * 0.95:
                        valid_boxes.append((bx, by, bx + bw_c, by + bh_c))

            if valid_boxes:
                min_x = min(b[0] for b in valid_boxes)
                min_y = min(b[1] for b in valid_boxes)
                max_x = max(b[2] for b in valid_boxes)
                max_y = max(b[3] for b in valid_boxes)
                if (max_x - min_x) >= 5 and (max_y - min_y) >= 5:
                    processed = processed[min_y:max_y, min_x:max_x]

        # 余白追加 (Tesseract精度向上)
        if margin > 0:
            processed = cv2.copyMakeBorder(
                processed, margin, margin, margin, margin,
                cv2.BORDER_CONSTANT, value=[255, 255, 255]
            )

        return trimmed, processed

    def read_commit_number(
        self,
        processed_img: np.ndarray,
        psm: int = 8,
        oem: int = 1,
        tessdata_dir: Optional[str] = None
    ) -> Dict[str, Any]:
        """前処理済み画像から数字4桁のコミット番号を読み取る"""
        res = {
            "commit_number": None,
            "raw_text": "",
            "confidence": 0.0,
            "digits_detail": [],
            "error": None
        }

        if not PYTESSERACT_AVAILABLE or not self.is_ready:
            res["error"] = "Tesseract-OCR が初期化されていません"
            return res

        if processed_img is None or processed_img.size == 0:
            res["error"] = "入力画像が無効です"
            return res

        config_parts = [f"--oem {oem}", f"--psm {psm}", "-c tessedit_char_whitelist=0123456789"]
        if tessdata_dir and os.path.isdir(tessdata_dir):
            config_parts.append(f'--tessdata-dir "{tessdata_dir}"')
        custom_config = " ".join(config_parts)

        try:
            data = pytesseract.image_to_data(
                processed_img,
                lang="eng",
                config=custom_config,
                output_type=pytesseract.Output.DICT
            )

            word_items = []
            all_valid_confs = []

            for j in range(len(data.get("text", []))):
                txt = str(data["text"][j]).strip()
                try:
                    c = float(data["conf"][j])
                except (ValueError, TypeError):
                    c = -1.0

                if txt:
                    word_items.append((txt, c))
                    if c >= 0:
                        all_valid_confs.append(c)

            if word_items:
                raw_text = " ".join([w[0] for w in word_items]).strip()
            else:
                raw_text = pytesseract.image_to_string(
                    processed_img,
                    lang="eng",
                    config=custom_config
                ).strip()

            res["raw_text"] = raw_text
            overall_conf = round(sum(all_valid_confs) / len(all_valid_confs), 1) if all_valid_confs else 0.0

            # 4桁数字の抽出
            matches = re.findall(r"\b\d{4}\b", raw_text)
            if not matches:
                cleaned = re.sub(r"[^\w]", "", raw_text)
                matches = re.findall(r"\d{4}", cleaned)

            if matches:
                commit_num = matches[0]
                res["commit_number"] = commit_num
                matched_confs = [c for txt, c in word_items if commit_num in txt and c >= 0]
                res["confidence"] = round(sum(matched_confs) / len(matched_confs), 1) if matched_confs else (overall_conf or 50.0)
            else:
                res["commit_number"] = None
                res["confidence"] = overall_conf
                res["error"] = f"4桁数字不一致 (生文字: '{raw_text}')"

        except Exception as e:
            res["error"] = str(e)

        return res

    def inspect_frame(
        self,
        frame: np.ndarray,
        roi: Optional[Dict[str, Any]] = None,
        kernel_size: int = 2,
        margin: int = 20,
        enable_tight_crop: bool = False,
        enable_clahe: bool = True,
        psm: int = 8,
        oem: int = 1,
        tessdata_dir: Optional[str] = None
    ) -> Dict[str, Any]:
        """ROI前処理とOCR読み取りを一括実行 (4点台形補正 / 矩形クロップ両対応)"""
        trimmed, processed = self.preprocess_image(
            frame, roi, kernel_size, margin, enable_tight_crop, enable_clahe
        )
        res = self.read_commit_number(processed, psm, oem, tessdata_dir)
        res["trimmed_img"] = trimmed
        res["processed_img"] = processed
        return res

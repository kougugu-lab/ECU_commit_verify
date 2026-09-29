#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qr_reader.py - 作業指示プロジェクション準拠・超高速高補正QRコードリーダー
多段階画像補正（CLAHE・大津・適応二値化・拡大・パディング）を備えつつ、
高速パス（Fast-path）と軽量全画面探索によりCPU負荷を最小化。
"""

import logging
from typing import List, Optional, Tuple, Dict, Any

import cv2
import numpy as np

import os
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# pyzbar の可用性チェック
try:
    from pyzbar import pyzbar
    from pyzbar.pyzbar import ZBarSymbol
    HAS_PYZBAR = True
except ImportError:
    HAS_PYZBAR = False

# WeChatQRCode の可用性チェック
HAS_WECHAT = hasattr(cv2, "wechat_qrcode_WeChatQRCode")


import threading

_stderr_lock = threading.Lock()

@contextmanager
def suppress_c_stderr():
    """C言語ライブラリ (zbar等) の stderr 出力を一時的に抑制する (スレッドセーフ)"""
    with _stderr_lock:
        try:
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            stderr_fd = 2
            saved_stderr = os.dup(stderr_fd)
            os.dup2(devnull_fd, stderr_fd)
            os.close(devnull_fd)
            try:
                yield
            finally:
                os.dup2(saved_stderr, stderr_fd)
                os.close(saved_stderr)
        except Exception:
            yield


class QRReader:
    """
    ROI内のQRコードを検出し、テキストとバウンディングボックスを返すクラス。
    高速パスと深層補正の段階的実行により、高い補正能力と低CPU負荷（リアルタイム性）を両立。
    """

    def __init__(self):
        self.detector = cv2.QRCodeDetector()
        self.wechat_detector = None
        if HAS_WECHAT:
            try:
                self.wechat_detector = cv2.wechat_qrcode_WeChatQRCode()
            except Exception as e:
                logger.debug(f"WeChatQRCode 初期化スキップ: {e}")

    def _try_decode(self, image: np.ndarray) -> Optional[Tuple[str, Optional[np.ndarray]]]:
        """単一画像に対して高速デコードを試行する"""
        if image is None or image.size == 0:
            return None

        # 1. pyzbar (最も高速・高耐性、シンボルをQRコードに限定)
        if HAS_PYZBAR:
            try:
                with suppress_c_stderr():
                    # symbols=[ZBarSymbol.QRCODE] で PDF417 等の不要なデコードをスキップ
                    # これにより pdf417.c のアサーション警告を防止し、デコードも大幅に高速化
                    barcodes = pyzbar.decode(image, symbols=[ZBarSymbol.QRCODE])
                for barcode in barcodes:
                    text = barcode.data.decode("utf-8", errors="ignore").strip()
                    if text:
                        poly = barcode.polygon
                        if poly and len(poly) >= 4:
                            pts = np.array([[p.x, p.y] for p in poly], dtype=np.float32)
                        else:
                            r = barcode.rect
                            pts = np.array([
                                [r.left, r.top],
                                [r.left + r.width, r.top],
                                [r.left + r.width, r.top + r.height],
                                [r.left, r.top + r.height],
                            ], dtype=np.float32)
                        return text, pts
            except Exception:
                pass

        # 2. WeChatQRCode (利用可能な場合)
        if self.wechat_detector is not None:
            try:
                res, points = self.wechat_detector.detectAndDecode(image)
                if res and len(res) > 0 and res[0].strip():
                    pts = points[0] if points and len(points) > 0 else None
                    if pts is not None and len(pts) >= 4:
                        pts = np.array(pts, dtype=np.float32).reshape(-1, 2)
                    return res[0].strip(), pts
            except Exception:
                pass

        # 3. OpenCV QRCodeDetector
        try:
            data, points, _ = self.detector.detectAndDecode(image)
            if data and data.strip():
                pts = points[0] if points is not None and len(points) > 0 else None
                if pts is not None and len(pts) >= 4:
                    pts = np.array(pts, dtype=np.float32).reshape(-1, 2)
                return data.strip(), pts
        except Exception:
            pass

        return None

    def _decode_pipeline(self, crop: np.ndarray) -> Optional[Tuple[str, Optional[np.ndarray]]]:
        """
        クロップ領域に対して多段階補正を段階的に適用。
        大半のケースはステージ1〜3の高速パス（数ミリ秒）で即完了。
        """
        if crop is None or crop.size == 0 or crop.shape[0] < 10 or crop.shape[1] < 10:
            return None

        # サイズが巨大な場合はダウンサンプリングして高速化
        h, w = crop.shape[:2]
        scale = 1.0
        work_crop = crop
        if max(h, w) > 600:
            scale = 600.0 / max(h, w)
            work_crop = cv2.resize(crop, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_LINEAR)

        # グレースケール変換
        if len(work_crop.shape) == 3:
            gray = cv2.cvtColor(work_crop, cv2.COLOR_BGR2GRAY)
        else:
            gray = work_crop

        # -------------------------------------------------------------
        # 高速パス (Fast Path): わずか数ミリ秒で完了
        # -------------------------------------------------------------
        # ステージ 1: グレースケール原画
        res = self._try_decode(gray)
        if res:
            text, pts = res
            return text, (pts / scale if (pts is not None and scale != 1.0) else pts)

        # ステージ 2: 白枠パディング付加 (クワイエットゾーン補正)
        pad = 16
        padded = cv2.copyMakeBorder(gray, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
        res = self._try_decode(padded)
        if res:
            text, pts = res
            if pts is not None:
                pts = pts - [pad, pad]
                if scale != 1.0:
                    pts = pts / scale
            return text, pts

        # ステージ 3: CLAHE (局所コントラスト補正)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        res = self._try_decode(enhanced)
        if res:
            text, pts = res
            return text, (pts / scale if (pts is not None and scale != 1.0) else pts)

        # ステージ 4: 大津の二値化 & 反転二値化
        _, otsu = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        res = self._try_decode(otsu)
        if res:
            text, pts = res
            return text, (pts / scale if (pts is not None and scale != 1.0) else pts)

        _, otsu_inv = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        res = self._try_decode(otsu_inv)
        if res:
            text, pts = res
            return text, (pts / scale if (pts is not None and scale != 1.0) else pts)

        # -------------------------------------------------------------
        # 深層補正パス (Deep Path): 悪条件時のみ実行
        # -------------------------------------------------------------
        # ステージ 5: メディアンブラー (モアレ除去)
        median = cv2.medianBlur(enhanced, 3)
        res = self._try_decode(median)
        if res:
            text, pts = res
            return text, (pts / scale if (pts is not None and scale != 1.0) else pts)

        # ステージ 6: 適応的二値化 (Gaussian)
        adaptive_gauss = cv2.adaptiveThreshold(
            enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 5
        )
        res = self._try_decode(adaptive_gauss)
        if res:
            text, pts = res
            return text, (pts / scale if (pts is not None and scale != 1.0) else pts)

        # ステージ 7: 微小QRコード拡大 (サイズが小さい場合のみ)
        ch, cw = gray.shape[:2]
        if ch < 250 and cw < 250:
            up_scale = 2.0
            upscaled = cv2.resize(enhanced, (int(cw * up_scale), int(ch * up_scale)), interpolation=cv2.INTER_CUBIC)
            up_adapt = cv2.adaptiveThreshold(
                upscaled, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5
            )
            res = self._try_decode(up_adapt)
            if res:
                text, pts = res
                if pts is not None:
                    pts = pts / up_scale
                    if scale != 1.0:
                        pts = pts / scale
                return text, pts

        return None

    def read_qr(
        self, frame: np.ndarray, roi: Optional[Dict[str, float]] = None
    ) -> Tuple[Optional[str], Optional[np.ndarray], Optional[np.ndarray]]:
        """
        フレームからROI領域を切り出し、高速・高補正でQRコードを読み取る。
        全画面多重走査を排除し、CPU負荷を最小化。
        戻り値: (デコード文字列, クロップ画像, 検出枠座標[フレーム全体座標系])
        """
        if frame is None or frame.size == 0:
            return None, None, None

        h, w = frame.shape[:2]
        if roi:
            rx1 = float(roi.get("rx1", 0.0))
            ry1 = float(roi.get("ry1", 0.0))
            rx2 = float(roi.get("rx2", 1.0))
            ry2 = float(roi.get("ry2", 1.0))
        else:
            rx1, ry1, rx2, ry2 = 0.0, 0.0, 1.0, 1.0

        x1 = max(0, min(w - 1, int(min(rx1, rx2) * w)))
        y1 = max(0, min(h - 1, int(min(ry1, ry2) * h)))
        x2 = max(x1 + 1, min(w, int(max(rx1, rx2) * w)))
        y2 = max(y1 + 1, min(h, int(max(ry1, ry2) * h)))

        crop_strict = frame[y1:y2, x1:x2]

        # 1. 拡張ROI（マージン25%外側まで包含してはみ出し対応）
        rw = x2 - x1
        rh = y2 - y1
        margin_x = int(rw * 0.25)
        margin_y = int(rh * 0.25)
        ex1 = max(0, x1 - margin_x)
        ey1 = max(0, y1 - margin_y)
        ex2 = min(w, x2 + margin_x)
        ey2 = min(h, y2 + margin_y)

        crop_exp = frame[ey1:ey2, ex1:ex2]
        res = self._decode_pipeline(crop_exp)
        if res:
            text, pts = res
            frame_pts = pts + [ex1, ey1] if pts is not None else None
            return text, crop_strict, frame_pts

        # 2. 軽量全画面探索 (ダウンサンプリングして1回だけ高速検出)
        # フルHD画像に対して重い補正を回すのではなく、縮小画像で瞬時に探索
        target_w = 640
        if w > target_w:
            scale_full = target_w / float(w)
            small_frame = cv2.resize(frame, (target_w, int(h * scale_full)), interpolation=cv2.INTER_AREA)
        else:
            scale_full = 1.0
            small_frame = frame

        if len(small_frame.shape) == 3:
            gray_small = cv2.cvtColor(small_frame, cv2.COLOR_BGR2GRAY)
        else:
            gray_small = small_frame

        res_full = self._try_decode(gray_small)
        if res_full:
            text, pts = res_full
            if pts is not None and len(pts) >= 4:
                pts_orig = pts / scale_full
                cx = float(np.mean(pts_orig[:, 0]))
                cy = float(np.mean(pts_orig[:, 1]))
                bx1, by1 = float(np.min(pts_orig[:, 0])), float(np.min(pts_orig[:, 1]))
                bx2, by2 = float(np.max(pts_orig[:, 0])), float(np.max(pts_orig[:, 1]))

                center_in_roi = (ex1 <= cx <= ex2) and (ey1 <= cy <= ey2)
                overlap = not (bx2 < x1 or bx1 > x2 or by2 < y1 or by1 > y2)

                if center_in_roi or overlap:
                    return text, crop_strict, pts_orig

        return None, crop_strict, None

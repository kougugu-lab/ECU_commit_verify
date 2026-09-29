#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
settings.py - 設定管理 (SettingsManager)
"""

import json
import os
import shutil
from pathlib import Path
from typing import Dict, Any

from .constants import SETTINGS_FILE


class SettingsManager:
    """アプリケーション設定の読み込み・保存・マイグレーションを担うクラス"""

    def __init__(self):
        home = os.path.expanduser("~")
        default_results_dir = os.path.join(home, "results")

        self.defaults: Dict[str, Any] = {
            "system": {
                "monitor_count": 1  # 監視サイクル数 (1 または 2)
            },
            "cameras": [
                {"id": "cam_1", "name": "カメラ 1", "index": 0, "autofocus": True, "focus": -1, "fourcc": "MJPG"},
                {"id": "cam_2", "name": "カメラ 2", "index": 1, "autofocus": True, "focus": -1, "fourcc": "MJPG"},
                {"id": "cam_3", "name": "カメラ 3", "index": 2, "autofocus": True, "focus": -1, "fourcc": "MJPG"},
                {"id": "cam_4", "name": "カメラ 4", "index": 3, "autofocus": True, "focus": -1, "fourcc": "MJPG"}
            ],
            "roi": {
                "cam_1_qr": {"rx1": 0.15, "ry1": 0.15, "rx2": 0.85, "ry2": 0.85},
                "cam_2_commit": {"rx1": 0.10, "ry1": 0.10, "rx2": 0.60, "ry2": 0.40, "points": None},
                "cam_2_color": {"rx1": 0.65, "ry1": 0.10, "rx2": 0.90, "ry2": 0.40, "points": None},
                "cam_3_qr": {"rx1": 0.15, "ry1": 0.15, "rx2": 0.85, "ry2": 0.85},
                "cam_4_commit": {"rx1": 0.10, "ry1": 0.10, "rx2": 0.60, "ry2": 0.40, "points": None},
                "cam_4_color": {"rx1": 0.65, "ry1": 0.10, "rx2": 0.90, "ry2": 0.40, "points": None}
            },
            "color_thresholds": {
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
            },
            "cycle": {
                "lost_frames_threshold": 10,     # QR消失（持ち出し）と判定する連続未読フレーム数
                "color_debounce_frames": 2,      # 色確定に必要な連続フレーム数
                "cycle_timeout_sec": 60.0,       # サイクル全体のタイムアウト(秒)
                "result_display_sec": 2.5,       # 判定結果表示保持時間(秒)
                "max_history_count": 10,         # 過去QR履歴件数
                "inspect_fps": 5                 # 判定FPS (1〜20fps, ラズパイ軽量化推奨 5fps)
            },
            "ocr": {
                "tesseract_cmd": "",
                "tessdata_dir": "",
                "psm": 8,
                "oem": 1,
                "kernel_size": 2,
                "margin": 20,
                "enable_tight_crop": False,
                "enable_clahe": True
            },
            "gpio": {
                "reset_pin": 23,
                "outputs": {
                    "ok": 16,
                    "ng": 20
                },
                "line2_outputs": {
                    "ok": 12,
                    "ng": 21
                },
                "ok_output_time": 0.5,
                "ng_output_time": "",
                "ng_output_hold": False,
                "buzzer_path_line1": "",
                "buzzer_path_line2": "",
                "buzzer_path_both": ""
            },
            "performance": {
                "camera_fps": 5,                 # カメラ取得FPS (1〜30fps, ラズパイ軽量化推奨 5〜10fps)
                "use_mjpg": True,                # MJPG圧縮キャプチャ優先 (USB帯域節約)
                "ocr_interval_sec": 0.3          # コミットOCR最小実行間隔(秒)
            },
            "storage": {
                "results_dir": default_results_dir,
                "auto_delete_enabled": True,
                "max_results_gb": round(max(0.1, (shutil.disk_usage(home).total / (1024**3)) - 1.0), 1),
                "capture_res": "1280x720",
                "preview_res": "640x480",
                "preview_fps": 5                 # 表示FPS (1〜30fps, ラズパイ軽量化推奨 5〜10fps)
            }
        }
        self.data = self.load_settings()

    def load_settings(self) -> Dict[str, Any]:
        """設定ファイルを読み込み、デフォルト値とマージする"""
        def merge(target: dict, source: dict) -> dict:
            for k, v in source.items():
                if isinstance(v, dict):
                    target[k] = merge(target.get(k, {}), v)
                else:
                    if k not in target:
                        target[k] = v
            return target

        try:
            if Path(SETTINGS_FILE).exists():
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)

                # カメラ数補正 (1ライン時はcam_1, cam_2、2ライン時はcam_1..cam_4)
                sys_cfg = loaded.get("system", {})
                mon_cnt = sys_cfg.get("monitor_count", 1)
                req_cam_ids = ["cam_1", "cam_2"] if mon_cnt == 1 else ["cam_1", "cam_2", "cam_3", "cam_4"]

                existing_cams = {c.get("id"): c for c in loaded.get("cameras", [])}
                def_cams = {c["id"]: c for c in self.defaults["cameras"]}

                final_cams = []
                for cid in req_cam_ids:
                    if cid in existing_cams:
                        cam = existing_cams[cid]
                        # カメラ名に括弧表記が含まれていれば除去
                        c_name = cam.get("name", "")
                        if " (" in c_name:
                            cam["name"] = c_name.split(" (")[0].strip()
                        # 欠落している新プロパティを補完
                        for k, v in def_cams[cid].items():
                            if k not in cam:
                                cam[k] = v
                        final_cams.append(cam)
                    else:
                        final_cams.append(def_cams[cid].copy())
                loaded["cameras"] = final_cams

                # パス補正
                home = os.path.expanduser("~")
                if "storage" in loaded and "results_dir" in loaded["storage"]:
                    p = loaded["storage"]["results_dir"]
                    if p.startswith("/home/pi/"):
                        loaded["storage"]["results_dir"] = p.replace("/home/pi", home, 1)

                return merge(loaded, self.defaults)
            else:
                return json.loads(json.dumps(self.defaults))
        except Exception as e:
            print(f"設定読み込みエラー: {e}")
            return json.loads(json.dumps(self.defaults))

    def save_settings(self) -> bool:
        """設定を settings.json に保存する"""
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"設定保存エラー: {e}")
            return False

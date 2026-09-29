#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cycle_manager.py - サイクル判定ステートマシンおよび履歴管理
- 新規QR検出 (過去10回と不一致) によるサイクル開始
- QRコード先頭4桁を正解コミット番号として保持
- 画面色シーケンス (緑 -> 黄 -> 緑) の遷移監視
- 瞬間未読の許容 (指定フレーム連続消失で持ち出しNG)
- 赤色検知・持ち出し・コミット不一致によるNG即時判定
- 直近10回QR履歴の永続化
"""

import os
import json
import time
import shutil
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

from .constants import (
    HISTORY_DIR, HISTORY_FILE,
    COLOR_STATE_GREEN, COLOR_STATE_YELLOW, COLOR_STATE_RED, COLOR_STATE_OTHER
)


class CycleStatus:
    IDLE = "IDLE"                                   # 待機中
    RUNNING_START = "RUNNING_START"                 # サイクル開始 (初期緑)
    RUNNING_YELLOW = "RUNNING_YELLOW"               # 作業中 (画面色: 黄色)
    COMPLETED_OK = "COMPLETED_OK"                   # 正常完了
    FAILED_NG = "FAILED_NG"                         # 異常終了 (NG)


class CycleManager:
    """サイクル判定ステートマシンと履歴を統括するクラス"""

    def __init__(self, settings_mgr, line_id: int = 1):
        self.settings_mgr = settings_mgr
        self.line_id = line_id
        self.status = CycleStatus.IDLE
        self.current_qr = ""
        self.target_commit = ""
        self.ocr_commit = ""
        self.commit_verified = False                # 黄色画面期間中に正常照合が完了したか
        self.last_screen_color = COLOR_STATE_OTHER
        
        self.cycle_start_time = 0.0
        self.lost_frames_count = 0                  # QR未検出の連続フレーム数
        self.ng_reason = ""
        self.ok_message = ""

        # 履歴フォルダ・ファイル名 (history フォルダ内に history_line1.json, history_line2.json として対称配置)
        self.history_dir = HISTORY_DIR
        fname = f"history_line{line_id}.json"
        self.history_file = os.path.join(self.history_dir, fname)

        # 旧配置・旧名称からの自動移行 (マイグレーション)
        os.makedirs(self.history_dir, exist_ok=True)
        if line_id == 1:
            # 旧 history/history.json から history/history_line1.json への移行
            old_h_json = os.path.join(self.history_dir, "history.json")
            if os.path.isfile(old_h_json) and not os.path.isfile(self.history_file):
                try:
                    shutil.move(old_h_json, self.history_file)
                except Exception:
                    pass
            # ルート直下の旧 history.json からの移行
            if os.path.isfile("history.json") and not os.path.isfile(self.history_file):
                try:
                    shutil.move("history.json", self.history_file)
                except Exception:
                    pass
        else:
            # ルート直下の旧 history_lineN.json からの移行
            old_root_file = f"history_line{line_id}.json"
            if os.path.isfile(old_root_file) and not os.path.isfile(self.history_file):
                try:
                    shutil.move(old_root_file, self.history_file)
                except Exception:
                    pass

        # 過去10回QR履歴 (直近10件)
        self.qr_history: List[str] = self.load_history()

        # NG履歴 (メモリ上 + CSV/ログ出力用)
        self.ng_history: List[Dict[str, Any]] = []

    def load_history(self) -> List[str]:
        """過去QR履歴をロードする"""
        try:
            if os.path.isfile(self.history_file):
                with open(self.history_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        max_cnt = self.settings_mgr.data.get("cycle", {}).get("max_history_count", 10)
                        return data[-max_cnt:]
        except Exception as e:
            print(f"QR履歴ロードエラー (Line {self.line_id}): {e}")
        return []

    def save_history(self):
        """過去QR履歴を保存する"""
        try:
            os.makedirs(self.history_dir, exist_ok=True)
            max_cnt = self.settings_mgr.data.get("cycle", {}).get("max_history_count", 10)
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(self.qr_history[-max_cnt:], f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"QR履歴保存エラー (Line {self.line_id}): {e}")

    def clear_qr_history(self):
        """QR履歴をクリアする"""
        self.qr_history.clear()
        self.save_history()

    def is_new_qr(self, qr_text: str) -> bool:
        """過去10回のQR履歴と合致しない新規QRコードか判定"""
        if not qr_text:
            return False
        return qr_text not in self.qr_history

    def extract_commit_number(self, qr_text: str) -> Optional[str]:
        """QRコードの先頭4桁から正解コミット番号を抽出"""
        if not qr_text:
            return None
        clean = qr_text.strip()
        if len(clean) < 4:
            return None
        head = clean[:4]
        if head.isdigit() and len(head) == 4:
            return head
        return None

    def reset_to_idle(self):
        """待機状態へ初期化"""
        self.status = CycleStatus.IDLE
        self.current_qr = ""
        self.target_commit = ""
        self.ocr_commit = ""
        self.commit_verified = False
        self.lost_frames_count = 0
        self.ng_reason = ""
        self.ok_message = ""
        self.cycle_start_time = 0.0

    def start_cycle(self, qr_text: str) -> bool:
        """新規QRコード検出によるサイクル開始"""
        commit = self.extract_commit_number(qr_text)
        if not commit:
            return False

        self.status = CycleStatus.RUNNING_START
        self.current_qr = qr_text
        self.target_commit = commit
        self.ocr_commit = ""
        self.commit_verified = False
        self.lost_frames_count = 0
        self.ng_reason = ""
        self.ok_message = ""
        self.cycle_start_time = time.time()
        return True

    def process_frame(
        self,
        qr_detected: bool,
        current_qr_text: Optional[str],
        screen_color: str,
        detected_commit: Optional[str]
    ) -> Tuple[str, Optional[str]]:
        """
        フレームごとの状態更新と判定を実行。
        戻り値: (現在のステータス, イベントメッセージまたはNone)
        """
        self.last_screen_color = screen_color
        cycle_cfg = self.settings_mgr.data.get("cycle", {})
        lost_thresh = cycle_cfg.get("lost_frames_threshold", 10)
        timeout_sec = cycle_cfg.get("cycle_timeout_sec", 60.0)

        # -------------------------------------------------------------
        # 1. 待機中 (IDLE): 新規QR検出でサイクル開始
        # -------------------------------------------------------------
        if self.status == CycleStatus.IDLE:
            if qr_detected and current_qr_text:
                if self.is_new_qr(current_qr_text):
                    # サイクル開始時に画面色が緑色でなければ即座にエラーNG
                    if screen_color != COLOR_STATE_GREEN:
                        col_disp = {"YELLOW": "黄色", "RED": "赤色", "OTHER": "未検知/暗黒"}.get(screen_color, screen_color)
                        self._trigger_ng(f"サイクル開始エラー: 画面色が緑色ではありません (現在: {col_disp})")
                        return self.status, self.ng_reason

                    if self.start_cycle(current_qr_text):
                        return self.status, f"サイクル開始: コミット {self.target_commit}"
                # 既出QRの場合は待機継続
            return self.status, None

        # -------------------------------------------------------------
        # 完了またはNG表示中の場合 (一定時間待機または外部リセット)
        # -------------------------------------------------------------
        if self.status in (CycleStatus.COMPLETED_OK, CycleStatus.FAILED_NG):
            return self.status, None

        # -------------------------------------------------------------
        # 2. サイクル実行中 (RUNNING_START / RUNNING_YELLOW)
        # -------------------------------------------------------------
        # (A) タイムアウト判定
        if timeout_sec > 0 and (time.time() - self.cycle_start_time) > timeout_sec:
            self._trigger_ng("サイクルタイムアウト超過")
            return self.status, self.ng_reason

        # (B) 赤色検知判定 (即時NG)
        if screen_color == COLOR_STATE_RED:
            self._trigger_ng("画面色異常 (赤色検知)")
            return self.status, self.ng_reason

        # (C) 黄色画面遷移時のコミット番号照合
        if detected_commit:
            self.ocr_commit = detected_commit
            if detected_commit != self.target_commit:
                self._trigger_ng(f"コミット不一致 (正解: {self.target_commit}, 読取: {detected_commit})")
                return self.status, self.ng_reason
            else:
                self.commit_verified = True

        # (D) QRコード消失監視 (瞬間未読の許容)
        # サイクル中 (開始緑・作業中黄) のQR存在確認
        if self.status in (CycleStatus.RUNNING_START, CycleStatus.RUNNING_YELLOW):
            # 提示されているQRコードが現在のサイクルQRと一致しているか
            if qr_detected and current_qr_text == self.current_qr:
                self.lost_frames_count = 0  # 検出されたので未読カウンタをリセット
            else:
                self.lost_frames_count += 1
                if self.lost_frames_count >= lost_thresh:
                    # 連続未読が閾値を超えた場合のみ「持ち出し」と判定
                    self._trigger_ng(f"作業未完了QR持ち出し (連続{self.lost_frames_count}フレーム未検出)")
                    return self.status, self.ng_reason

        # (E) 画面色のシーケンス遷移
        if self.status == CycleStatus.RUNNING_START:
            # 初期状態から黄色への変化を検知 -> 作業中へ遷移
            if screen_color == COLOR_STATE_YELLOW:
                self.status = CycleStatus.RUNNING_YELLOW
                self.lost_frames_count = 0
                return self.status, "作業中 (黄色検知: コミット照合開始)"

        elif self.status == CycleStatus.RUNNING_YELLOW:
            # 黄色から最後の緑色への復帰を検知 -> サイクル正常終了判定
            if screen_color == COLOR_STATE_GREEN:
                # 黄色画面期間中にコミット番号が正常照合されたかを確認
                if not self.commit_verified or not self.ocr_commit:
                    self._trigger_ng("コミット未確認 (黄色画面中にコミット番号が照合されませんでした)")
                    return self.status, self.ng_reason
                elif self.ocr_commit != self.target_commit:
                    self._trigger_ng(f"コミット不一致 (正解: {self.target_commit}, 読取: {self.ocr_commit})")
                    return self.status, self.ng_reason

                self._trigger_ok()
                return self.status, self.ok_message

        return self.status, None

    def _trigger_ok(self):
        """サイクル正常完了 (OK)"""
        self.status = CycleStatus.COMPLETED_OK
        self.ok_message = f"サイクル完了 OK (コミット: {self.target_commit})"

        # 過去QR履歴に追加 (直近10件)
        if self.current_qr and self.current_qr not in self.qr_history:
            self.qr_history.append(self.current_qr)
            max_cnt = self.settings_mgr.data.get("cycle", {}).get("max_history_count", 10)
            if len(self.qr_history) > max_cnt:
                self.qr_history.pop(0)
            self.save_history()

    def _trigger_ng(self, reason: str):
        """異常発生 (NG)"""
        self.status = CycleStatus.FAILED_NG
        self.ng_reason = reason

        # NG履歴に記録
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "qr": self.current_qr,
            "target_commit": self.target_commit or "----",
            "ocr_commit": self.ocr_commit or "----",
            "reason": reason
        }
        self.ng_history.insert(0, record)
        if len(self.ng_history) > 100:
            self.ng_history.pop()

        # NGのQRコードも再度読み直すかどうかに応じて履歴に加えるか選べるが、
        # 未完了なので過去履歴には含めず、再作業できるようにする

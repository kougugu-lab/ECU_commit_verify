#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
widgets.py - 共通UI部品 (カード, ツールチップ, 操作ヘルプ, 仮想GPIOパネル)
inspection_app 完全準拠。
"""

import sys
import time
import threading
import tkinter as tk
from tkinter import ttk
from typing import Tuple, Optional, Dict

from .constants import (
    COLOR_BG_MAIN, COLOR_BG_PANEL, COLOR_BG_INPUT,
    COLOR_TEXT_MAIN, COLOR_TEXT_SUB, COLOR_ACCENT,
    COLOR_OK, COLOR_NG, COLOR_WARNING, COLOR_BORDER,
    FONT_FAMILY, FONT_NORMAL, FONT_BOLD, FONT_LARGE, FONT_HUGE,
    FONT_SET_VAL, VERSION
)
from .hardware import MockManager, is_gpio_available


def create_card(parent: tk.Widget, title: Optional[str] = None) -> Tuple[tk.Frame, tk.Frame]:
    """共通デザインのカードフレームを作成 (inspection_app 完全一致)"""
    frame = tk.Frame(parent, bg=COLOR_BG_PANEL, bd=1, relief="flat")
    inner = tk.Frame(frame, bg=COLOR_BG_PANEL, padx=10, pady=10,
                     highlightbackground=COLOR_BORDER, highlightthickness=1)
    inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

    if title:
        lbl = tk.Label(inner, text=title, font=FONT_BOLD,
                       bg=COLOR_BG_PANEL, fg=COLOR_ACCENT, anchor="w")
        lbl.pack(fill=tk.X, pady=(0, 10))
    return frame, inner


class Tooltip:
    """ウィジェットにマウスホバーで説明を表示するツールチップ (inspection_app 完全準拠)"""

    def __init__(self, widget: tk.Widget, text: str):
        self.widget = widget
        self.text = text
        self.tip_window: Optional[tk.Toplevel] = None
        self._after_id = None
        self.widget.bind("<Enter>", self._on_enter)
        self.widget.bind("<Leave>", self.hide_tip)

    def _on_enter(self, event=None):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
        self._after_id = self.widget.after(400, self.show_tip)

    def show_tip(self, event=None):
        if self.tip_window or not self.text:
            return
        try:
            x, y, _, _ = self.widget.bbox("insert")
        except Exception:
            x, y = 0, 0
        x = x + self.widget.winfo_rootx() + 25
        y = y + self.widget.winfo_rooty() + 25

        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.configure(bg="#222")

        label = tk.Label(
            tw, text=self.text, justify=tk.LEFT,
            background="#2d3748", foreground="#f7fafc",
            relief=tk.SOLID, borderwidth=1,
            font=(FONT_FAMILY, 10), padx=8, pady=4
        )
        label.pack(ipadx=1)

    def hide_tip(self, event=None):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


def configure_modal_toplevel(win: tk.Toplevel, parent=None):
    """モーダル Toplevel を安全に表示・フォーカス・grab する (inspection_app 完全一致)"""
    if parent is not None:
        try:
            win.transient(parent)
        except tk.TclError:
            pass

    def _drop_topmost():
        if win.winfo_exists():
            try:
                win.attributes("-topmost", False)
            except tk.TclError:
                pass

    def _activate():
        if not win.winfo_exists() or getattr(win, "_modal_grab_active", False):
            return
        win.update_idletasks()
        try:
            if not win.winfo_viewable():
                return
        except tk.TclError:
            return
        try:
            if win.state() == "iconic":
                win.deiconify()
        except tk.TclError:
            pass
        try:
            win.lift()
            win.attributes("-topmost", True)
            win.after(80, _drop_topmost)
        except tk.TclError:
            pass
        try:
            win.focus_force()
        except tk.TclError:
            pass
        try:
            win.grab_set()
            win._modal_grab_active = True
        except tk.TclError:
            pass

    def _on_map(event=None):
        if event is not None and event.widget != win:
            return
        win.after_idle(_activate)

    try:
        win.attributes("-type", "dialog")
    except tk.TclError:
        pass

    win._modal_grab_active = False
    win.bind("<Map>", _on_map, add="+")
    win.after_idle(_on_map)
    win.after(500, _activate)


class HelpWindow(tk.Toplevel):
    """操作ヘルプ画面 (inspection_app 完全準拠)"""

    def __init__(self, parent, title: str, help_dict: Dict[str, str]):
        super().__init__(parent)
        self.title(title)
        self.geometry("680x640")
        self.configure(bg=COLOR_BG_MAIN)
        self.transient(parent)

        header = tk.Frame(self, bg=COLOR_BG_PANEL, pady=15)
        header.pack(fill=tk.X)
        tk.Label(
            header, text=title, font=FONT_BOLD,
            bg=COLOR_BG_PANEL, fg=COLOR_ACCENT
        ).pack()

        container = tk.Frame(self, bg=COLOR_BG_MAIN, padx=20, pady=20)
        container.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(container, bg=COLOR_BG_MAIN, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=COLOR_BG_MAIN)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for section, content in help_dict.items():
            tk.Label(
                scrollable_frame, text=f"■ {section}", font=FONT_BOLD,
                bg=COLOR_BG_MAIN, fg=COLOR_ACCENT, anchor="w",
                justify=tk.LEFT
            ).pack(fill=tk.X, pady=(10, 5))
            tk.Label(
                scrollable_frame, text=content, font=FONT_NORMAL,
                bg=COLOR_BG_MAIN, fg=COLOR_TEXT_MAIN, anchor="w",
                justify=tk.LEFT, wraplength=580
            ).pack(fill=tk.X, pady=(0, 15))

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_mouse(event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_mouse(event):
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", _bind_mouse)
        canvas.bind("<Leave>", _unbind_mouse)

        tk.Button(
            self, text="閉じる", font=FONT_BOLD, bg=COLOR_BG_INPUT,
            fg=COLOR_TEXT_MAIN, relief="flat", pady=10,
            command=self.destroy
        ).pack(fill=tk.X)


class MockGPIOPanel:
    """Windowsデバッグ用仮想GPIO操作パネル (inspection_app 完全準拠)"""

    def __init__(self, root, settings_mgr):
        self.root = root
        self.settings = settings_mgr
        self.top = tk.Toplevel(root)
        self.top.title("仮想GPIOパネル")
        self.top.geometry("380x300+10+10")
        self.top.configure(bg=COLOR_BG_MAIN)
        self.top.attributes("-topmost", True)

        c_out, c_in = create_card(self.top, "仮想GPIO制御")
        c_out.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # OK/NG 出力状態
        tk.Label(c_in, text="仮想出力状態", font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(anchor="w", pady=(4, 2))
        f_leds = tk.Frame(c_in, bg=COLOR_BG_PANEL)
        f_leds.pack(fill=tk.X, pady=4)

        self.led_ok = tk.Label(f_leds, text="OK出力", bg="#444", fg="white", font=FONT_NORMAL, padx=10, pady=4)
        self.led_ok.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
        Tooltip(self.led_ok, "OK出力信号 (Pin 16)")

        self.led_ng = tk.Label(f_leds, text="NG出力", bg="#444", fg="white", font=FONT_NORMAL, padx=10, pady=4)
        self.led_ng.pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
        Tooltip(self.led_ng, "NG出力信号 (Pin 20)")

        # リセットボタン
        tk.Label(c_in, text="仮想入力", font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(anchor="w", pady=(12, 2))
        btn_reset = tk.Button(
            c_in, text="リセットパルス送信", font=FONT_NORMAL,
            bg=COLOR_ACCENT, fg="white", relief="flat",
            command=self._pulse_reset
        )
        btn_reset.pack(fill=tk.X, pady=4)
        Tooltip(btn_reset, "リセットパルス信号 (Pin 23) を送信します")

        self._update_loop()

    def _pulse_reset(self):
        reset_pin = self.settings.data.get("gpio", {}).get("reset_pin", 23)
        MockManager.set_input(reset_pin, True)
        self.top.after(200, lambda: MockManager.set_input(reset_pin, False))

    def _update_loop(self):
        try:
            if not self.top.winfo_exists():
                return
            ok_pin = self.settings.data.get("gpio", {}).get("outputs", {}).get("ok", 16)
            ng_pin = self.settings.data.get("gpio", {}).get("outputs", {}).get("ng", 20)
            is_ok = MockManager.get_output_state(ok_pin)
            is_ng = MockManager.get_output_state(ng_pin)

            self.led_ok.configure(bg=COLOR_OK if is_ok else "#444")
            self.led_ng.configure(bg=COLOR_NG if is_ng else "#444")

            self.top.after(150, self._update_loop)
        except Exception:
            pass

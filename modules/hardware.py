#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hardware.py - GPIO ラッパー・モック・ハードウェア制御
"""

import sys
import time
import threading
from typing import Callable, Optional


class MockManager:
    """全モックデバイスの状態を管理するクラス"""
    inputs = {}   # pin -> MockInput
    outputs = {}  # pin -> MockDevice

    @classmethod
    def register_input(cls, device):
        cls.inputs[str(device.pin)] = device

    @classmethod
    def register_output(cls, device):
        cls.outputs[str(device.pin)] = device

    @classmethod
    def set_input(cls, pin, value):
        """外部から仮想入力を変更する"""
        p = str(pin)
        if p in cls.inputs:
            dev = cls.inputs[p]
            old_val = dev._value
            dev._value = bool(value)
            if not old_val and dev._value and dev.when_activated:
                dev.when_activated()
            elif old_val and not dev._value and dev.when_deactivated:
                dev.when_deactivated()

    @classmethod
    def get_input_state(cls, pin):
        p = str(pin)
        if p in cls.inputs:
            return cls.inputs[p]._value
        return False

    @classmethod
    def get_output_state(cls, pin):
        p = str(pin)
        if p in cls.outputs:
            return cls.outputs[p].is_active
        return False


class MockDevice:
    """出力デバイスのモック"""
    def __init__(self, pin, *args, **kwargs):
        self.pin = pin
        self._active = False
        MockManager.register_output(self)

    def on(self):
        self._active = True

    def off(self):
        self._active = False

    @property
    def is_active(self):
        return self._active

    def close(self):
        pass


class MockInput(MockDevice):
    """入力デバイスのモック"""
    def __init__(self, pin, *args, **kwargs):
        self.pin = pin
        self._value = False
        self.when_activated: Optional[Callable] = None
        self.when_deactivated: Optional[Callable] = None
        MockManager.register_input(self)

    @property
    def is_active(self):
        return self._value

    def on(self):
        self._value = True
        if self.when_activated:
            self.when_activated()

    def off(self):
        self._value = False
        if self.when_deactivated:
            self.when_deactivated()


# プラットフォームごとのGPIO初期化
if not sys.platform.startswith("linux"):
    GPIO_AVAILABLE = False
    DigitalInputDevice = MockInput
    OutputDevice = MockDevice
else:
    try:
        from gpiozero import DigitalInputDevice as _DigitalInputDevice
        from gpiozero import OutputDevice as _OutputDevice
        GPIO_AVAILABLE = True

        def DigitalInputDevice(pin, *args, **kwargs):
            global GPIO_AVAILABLE
            try:
                return _DigitalInputDevice(pin, *args, **kwargs)
            except Exception as e:
                print(f"ピン {pin} で代替モック(MockInput)を使用します: {e}")
                GPIO_AVAILABLE = False
                return MockInput(pin, *args, **kwargs)

        def OutputDevice(pin, *args, **kwargs):
            global GPIO_AVAILABLE
            try:
                return _OutputDevice(pin, *args, **kwargs)
            except Exception as e:
                print(f"ピン {pin} で代替モック(MockDevice)を使用します: {e}")
                GPIO_AVAILABLE = False
                return MockDevice(pin, *args, **kwargs)

    except ImportError:
        GPIO_AVAILABLE = False
        DigitalInputDevice = MockInput
        OutputDevice = MockDevice


def is_gpio_available() -> bool:
    """現在のGPIOの有効状態を返す"""
    return GPIO_AVAILABLE

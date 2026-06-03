"""蜂鸣器设备骨架 —— 用于咬手指提醒等场景。

⚠️ 骨架阶段。区分两类:
    - 有源蜂鸣器: 给电就响,用 OutputDevice / Buzzer
    - 无源蜂鸣器: 要送方波,需要 PWMOutputDevice / TonalBuzzer

当前实现按"有源蜂鸣器"写,因为它最简单,适合作为占位。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from .base import Device, DeviceError


logger = logging.getLogger("devices.buzzer")


class BuzzerDevice(Device):
    """有源蜂鸣器(给电就响)。

    配置示例:
        device: buzzer
        pin: 18
        beeps: 2          # 默认响几声
        on_ms: 120        # 每声多长
        off_ms: 80        # 间隔
    """

    kind = "buzzer"

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)
        self.pin: int = int(self.config.get("pin", -1))
        self.default_beeps: int = int(self.config.get("beeps", 2))
        self.default_on_ms: int = int(self.config.get("on_ms", 120))
        self.default_off_ms: int = int(self.config.get("off_ms", 80))

        if self.pin < 0:
            raise DeviceError(f"BuzzerDevice {name}: 必须配置 pin")

        self._gpio = None
        self._setup_gpio()

    def _setup_gpio(self) -> None:
        try:
            from gpiozero import Buzzer  # type: ignore
            self._gpio = Buzzer(self.pin)
            logger.info(f"[buzzer:{self.name}] GPIO {self.pin} 已就绪")
        except Exception as exc:
            logger.warning(
                f"[buzzer:{self.name}] gpiozero 不可用,降级为 dummy 模式: {exc}"
            )
            self._gpio = None

    def _beep_once(self, on_s: float, off_s: float) -> None:
        if self._gpio is not None:
            self._gpio.on()
            time.sleep(on_s)
            self._gpio.off()
        else:
            logger.info(f"[buzzer:{self.name}] [dummy] BEEP {on_s*1000:.0f}ms")
            time.sleep(on_s)
        time.sleep(off_s)

    def trigger(self, payload: Optional[Dict[str, Any]] = None) -> None:
        beeps = int(self._param(payload, "beeps", self.default_beeps))
        on_ms = int(self._param(payload, "on_ms", self.default_on_ms))
        off_ms = int(self._param(payload, "off_ms", self.default_off_ms))
        beeps = max(1, min(beeps, 10))
        for _ in range(beeps):
            self._beep_once(on_ms / 1000.0, off_ms / 1000.0)

    def close(self) -> None:
        try:
            if self._gpio is not None:
                self._gpio.off()
                self._gpio.close()
        except Exception:
            logger.exception(f"[buzzer:{self.name}] 关闭失败")
        super().close()

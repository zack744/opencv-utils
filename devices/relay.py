"""继电器设备骨架 —— 用于电击模块、电磁锁、灯等开关型外设。

⚠️ 当前是骨架,trigger() 内部只是占位日志。
   等硬件型号确定后,把 _on() / _off() 改成真实 GPIO 调用即可,
   trigger() 的脉冲时序逻辑已经写好,不需要改。

典型接线:
    GPIO pin → 继电器模块 IN
    GND      → 继电器模块 GND
    5V/3V3   → 继电器模块 VCC
    继电器 COM/NO 接到要控制的电路上
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from .base import Device, DeviceError


logger = logging.getLogger("devices.relay")


class RelayDevice(Device):
    """开关型继电器(高/低电平触发)。

    配置示例(config/devices.yaml):
        device: relay
        pin: 17
        pulse: 0.3        # 默认脉冲时长,可被 payload 覆盖
        active_high: true # 触发时电平。多数继电器模块是 active_low,要看模组型号
    """

    kind = "relay"

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)
        self.pin: int = int(self.config.get("pin", -1))
        self.default_pulse: float = float(self.config.get("pulse", 0.3))
        self.active_high: bool = bool(self.config.get("active_high", True))

        if self.pin < 0:
            raise DeviceError(f"RelayDevice {name}: 必须配置 pin")

        self._gpio = None
        self._setup_gpio()

    def _setup_gpio(self) -> None:
        """初始化 GPIO。Windows 上自动降级。

        TODO(树莓派接线后):
            from gpiozero import OutputDevice
            self._gpio = OutputDevice(self.pin, active_high=self.active_high, initial_value=False)
        """
        try:
            from gpiozero import OutputDevice  # type: ignore
            self._gpio = OutputDevice(
                self.pin,
                active_high=self.active_high,
                initial_value=False,
            )
            logger.info(f"[relay:{self.name}] GPIO {self.pin} 已就绪")
        except Exception as exc:
            logger.warning(
                f"[relay:{self.name}] gpiozero 不可用,降级为 dummy 模式: {exc}"
            )
            self._gpio = None

    def _on(self) -> None:
        if self._gpio is not None:
            self._gpio.on()
        else:
            logger.info(f"[relay:{self.name}] [dummy] ON  (pin={self.pin})")

    def _off(self) -> None:
        if self._gpio is not None:
            self._gpio.off()
        else:
            logger.info(f"[relay:{self.name}] [dummy] OFF (pin={self.pin})")

    def trigger(self, payload: Optional[Dict[str, Any]] = None) -> None:
        """高电平 N 秒,然后低电平。"""
        pulse = float(self._param(payload, "pulse", self.default_pulse))
        pulse = max(0.0, min(pulse, 10.0))  # 安全限幅
        self._on()
        try:
            time.sleep(pulse)
        finally:
            self._off()

    def close(self) -> None:
        try:
            self._off()
            if self._gpio is not None:
                self._gpio.close()
        except Exception:
            logger.exception(f"[relay:{self.name}] 关闭失败")
        super().close()

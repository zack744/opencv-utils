"""舵机门栓骨架 —— 用于俯卧撑开锁场景。

⚠️ 骨架阶段。等硬件确定后微调参数即可。
   开锁动作 = 转到 open_angle → 保持 hold 秒 → 转回 close_angle。

典型接线 (SG90 / MG996 等):
    舵机橙线 (信号) → GPIO (建议 GPIO 12/13/18/19 这些硬件 PWM 引脚)
    舵机红线 (VCC)  → 5V 外接电源 (舵机峰值电流大,别从树莓派 5V 拉)
    舵机棕线 (GND)  → 共地

⚠️ 树莓派 PWM 抖动:
    软件 PWM 抖,舵机会嗡嗡响 + 不准。Pi 5 上 gpiozero 默认走 lgpio,
    精度可以。要更稳可以外接 PCA9685 舵机驱动板。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from .base import Device, DeviceError


logger = logging.getLogger("devices.servo_lock")


class ServoLockDevice(Device):
    """单舵机推动门栓的开锁器。

    配置示例:
        device: servo_lock
        pin: 12
        close_angle: 0     # 闭锁时舵机角度(-90 ~ 90)
        open_angle: 90     # 开锁时舵机角度
        hold: 5            # 开锁后保持多少秒再回闭锁位
    """

    kind = "servo_lock"

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)
        self.pin: int = int(self.config.get("pin", -1))
        self.close_angle: float = float(self.config.get("close_angle", 0))
        self.open_angle: float = float(self.config.get("open_angle", 90))
        self.default_hold: float = float(self.config.get("hold", 5.0))

        if self.pin < 0:
            raise DeviceError(f"ServoLockDevice {name}: 必须配置 pin")

        self._servo = None
        self._setup_gpio()

    def _setup_gpio(self) -> None:
        try:
            from gpiozero import AngularServo  # type: ignore
            # min/max_angle 是逻辑角度,min/max_pulse_width 是物理脉宽
            # 默认值适合 SG90,如果舵机抖请按数据手册微调
            self._servo = AngularServo(
                self.pin,
                min_angle=-90, max_angle=90,
                min_pulse_width=0.0005, max_pulse_width=0.0025,
            )
            self._servo.angle = self.close_angle
            logger.info(f"[servo_lock:{self.name}] GPIO {self.pin} 已就绪,初始角度 {self.close_angle}°")
        except Exception as exc:
            logger.warning(
                f"[servo_lock:{self.name}] gpiozero 不可用,降级为 dummy 模式: {exc}"
            )
            self._servo = None

    def _move_to(self, angle: float) -> None:
        if self._servo is not None:
            self._servo.angle = angle
        else:
            logger.info(f"[servo_lock:{self.name}] [dummy] move to {angle}°")

    def trigger(self, payload: Optional[Dict[str, Any]] = None) -> None:
        hold = float(self._param(payload, "hold", self.default_hold))
        hold = max(0.5, min(hold, 30.0))

        logger.info(f"[servo_lock:{self.name}] 开锁 {hold}s")
        self._move_to(self.open_angle)
        time.sleep(hold)
        self._move_to(self.close_angle)
        logger.info(f"[servo_lock:{self.name}] 闭锁")

    def close(self) -> None:
        try:
            if self._servo is not None:
                self._servo.angle = self.close_angle
                time.sleep(0.3)
                self._servo.close()
        except Exception:
            logger.exception(f"[servo_lock:{self.name}] 关闭失败")
        super().close()

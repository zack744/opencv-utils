"""占位设备 —— 不依赖任何硬件。

用途：
    1. Windows / Mac 开发机上没有 GPIO 时的兜底
    2. 配置里写了未知设备类型时的安全降级
    3. 单元测试 / 调试 dispatcher 时使用
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .base import Device


logger = logging.getLogger("devices.dummy")


class DummyDevice(Device):
    """只打日志、不接触任何硬件的设备。"""

    kind = "dummy"

    def trigger(self, payload: Optional[Dict[str, Any]] = None) -> None:
        logger.info(f"[DUMMY {self.name}] trigger payload={payload}")

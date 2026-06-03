"""外设接入层。

业务层（closed_eye / bite_finger / pushup）只通过 BaseCameraApp.emit_action()
发出事件，不直接接触硬件。事件由 ActionDispatcher 路由到具体 Device 实例执行。

当前主用: ArduinoDevice —— Pi 通过 USB 串口给 Arduino Nano 发指令,
            Nano 拉数字脚 → 继电器 → 外设。

Windows 开发机上：自动降级为 DummyDevice，只打印日志，不报错。
树莓派上：根据 config/devices.yaml 加载实际设备（arduino / GPIO / I2C / HTTP / MQTT 等）。
"""

from .base import Device, DeviceError
from .dummy import DummyDevice
from .dispatcher import ActionDispatcher, build_dispatcher_from_config

__all__ = [
    "Device",
    "DeviceError",
    "DummyDevice",
    "ActionDispatcher",
    "build_dispatcher_from_config",
]

"""Arduino Nano 串口设备 - Pi 通过 USB 串口给 Nano 发指令,Nano 控制继电器。

协议(文本,易调试):
    Pi → Nano:   "PIN 3 HIGH 2000\n"   pin=D3 高电平 2000ms 后自动恢复
                 "SET 3 HIGH\n"        pin=D3 保持高电平,直到 STOP
                 "STOP 3\n"            释放 D3
    Nano → Pi:   "OK\n"  /  "ERR ...\n"

同一块 Nano 可以挂多个外设 - yaml 里两条路由用同一 port、不同 pin 即可,
串口实例自动复用,锁保护并发写入。
"""

from __future__ import annotations
import atexit
import logging
import threading
import time
from typing import Any, Dict, Optional

from .base import Device, DeviceError

logger = logging.getLogger("devices.arduino")

# 串口实例 + 写锁按 port 缓存(同一根 USB 线被多个 ArduinoDevice 复用)
_SERIAL_CACHE: Dict[str, Any] = {}
_PORT_LOCKS: Dict[str, threading.Lock] = {}


def _port_lock(port: str) -> threading.Lock:
    if port not in _PORT_LOCKS:
        _PORT_LOCKS[port] = threading.Lock()
    return _PORT_LOCKS[port]


def _open_serial(port: str, baud: int):
    ser = _SERIAL_CACHE.get(port)
    if ser is not None and getattr(ser, "is_open", False):
        return ser
    try:
        import serial  # type: ignore
    except ImportError as exc:
        raise DeviceError("pyserial 未安装: pip install pyserial") from exc
    try:
        ser = serial.Serial(port, baudrate=baud, timeout=1.0)
        time.sleep(2.0)  # Nano 上电会复位,等它跑起来
        # 读掉 READY
        try:
            ser.readline()
        except Exception:
            pass
        _SERIAL_CACHE[port] = ser
        return ser
    except Exception as exc:
        raise DeviceError(f"打开串口 {port} 失败: {exc}") from exc


@atexit.register
def _close_all_serials():
    for port, ser in list(_SERIAL_CACHE.items()):
        try:
            ser.close()
        except Exception:
            pass


class ArduinoDevice(Device):
    """通过 USB 串口控制 Arduino Nano 上挂的外设。

    配置示例(config/devices.yaml):
        device: arduino
        port: /dev/ttyUSB0     # Windows: COM3 ; Linux/Pi: /dev/ttyUSB0 或 /dev/ttyACM0
        baud: 9600
        pin: 3                 # Nano 的数字脚号(不是 Pi GPIO!)
        pulse: 2.0             # 高电平时长,秒
        active_high: false     # 多数继电器模块是 active_low
    """

    kind = "arduino"

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)
        self.port: str = str(self.config.get("port", "/dev/ttyUSB0"))
        self.baud: int = int(self.config.get("baud", 9600))
        self.pin: int = int(self.config.get("pin", -1))
        self.default_pulse: float = float(self.config.get("pulse", 0.3))
        self.active_high: bool = bool(self.config.get("active_high", True))

        if self.pin < 0:
            raise DeviceError(f"ArduinoDevice {name}: 必须配置 pin")

        self._ser = None
        try:
            self._ser = _open_serial(self.port, self.baud)
            logger.info(f"[arduino:{self.name}] {self.port}@{self.baud} ready, pin={self.pin}")
        except DeviceError as exc:
            logger.warning(f"[arduino:{self.name}] 串口不可用,降级 dummy: {exc}")
            self._ser = None

    def _send(self, cmd: str) -> str:
        if self._ser is None:
            logger.info(f"[arduino:{self.name}] [dummy] {cmd!r}")
            return "DUMMY"
        with _port_lock(self.port):
            try:
                self._ser.reset_input_buffer()
                self._ser.write((cmd + "\n").encode("ascii"))
                self._ser.flush()
                resp = self._ser.readline().decode("ascii", errors="ignore").strip()
                if resp.startswith("ERR"):
                    logger.warning(f"[arduino:{self.name}] Nano: {resp}")
                else:
                    logger.info(f"[arduino:{self.name}] Nano: {resp}")
                return resp or "<no resp>"
            except Exception as exc:
                logger.error(f"[arduino:{self.name}] 串口写失败: {exc}")
                return "EXC"

    def _active_level(self) -> str:
        return "HIGH" if self.active_high else "LOW"

    def trigger(self, payload: Optional[Dict[str, Any]] = None) -> None:
        state = self._param(payload, "state", None)
        if isinstance(state, bool):
            state = "on" if state else "off"
        if state is not None:
            state_text = str(state).strip().lower()
            if state_text in {"on", "1", "true", "enable", "enabled"}:
                resp = self._send(f"SET {self.pin} {self._active_level()}")
                if resp.startswith("ERR UNKNOWN"):
                    # 兼容未升级到 SET 协议的旧固件：最多保持 60 秒。
                    self._send(f"PIN {self.pin} {self._active_level()} 60000")
                return
            if state_text in {"off", "0", "false", "disable", "disabled"}:
                self._send(f"STOP {self.pin}")
                return

        pulse = float(self._param(payload, "pulse", self.default_pulse))
        pulse = max(0.0, min(pulse, 60.0))  # 上限 60s
        level = self._active_level()
        ms = int(pulse * 1000)
        # Nano v1.1+ 用 millis() 调度脉冲,这里发完命令 Nano 立即回 OK (<100ms)
        # 实际的 pulse 时长在 Nano 端后台跑,worker 线程立即释放
        # 如果有同 pin 短间隔连发,新脉冲会覆盖同脚的旧脉冲(cooldown 已在外层拦)
        self._send(f"PIN {self.pin} {level} {ms}")

    def close(self) -> None:
        # 串口实例由 atexit 统一关,这里不动
        super().close()

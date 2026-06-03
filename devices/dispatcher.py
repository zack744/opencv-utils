"""事件路由分发器 —— 业务层和设备层的中介。

职责：
    1. 把 app 发出的 (mode, action, payload) 路由到对应的 Device.trigger()
    2. 在独立 worker 线程执行 trigger,避免阻塞摄像头/检测线程
    3. 每条路由独立的冷却时间,防止误触发
    4. 失败隔离 —— 一个设备坏了不影响其他模式

调用关系：
    ┌─ closed_eye_app.process_frame()
    │     └── self.emit_action("shock", duration=0.3)
    │            └── BaseCameraApp.emit_action 调用本类的 dispatch()
    │                   └── 入 worker 队列 (非阻塞)
    └─ worker 线程从队列取事件 → 找路由 → 调 Device.trigger()
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .base import Device, DeviceError
from .dummy import DummyDevice


logger = logging.getLogger("devices.dispatcher")


@dataclass
class Route:
    """一条触发路由 —— 把 (mode, action) 映射到一个设备。"""

    mode: str                    # 业务模式: closed_eye / bite_finger / pushup
    action: str                  # 动作名: shock / alert / unlock ...
    device: Device               # 实际执行的设备实例
    cooldown: float = 1.0        # 同一路由两次触发的最小间隔(秒)
    default_payload: Dict[str, Any] = field(default_factory=dict)
    last_fire: float = 0.0       # 上次触发时间戳(内部用)

    @property
    def key(self) -> str:
        return f"{self.mode}.{self.action}"


@dataclass
class _Event:
    mode: str
    action: str
    payload: Dict[str, Any]
    ts: float


class ActionDispatcher:
    """事件分发器。线程安全,可被任意线程调用 dispatch()。"""

    def __init__(self, queue_size: int = 32):
        self._routes: Dict[str, Route] = {}
        self._devices: Dict[str, Device] = {}  # 用于统一 close()
        self._queue: "queue.Queue[Optional[_Event]]" = queue.Queue(maxsize=queue_size)
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name="dispatcher-worker", daemon=True
        )
        self._worker.start()
        logger.info("ActionDispatcher 启动")

    # ---------- 注册 ---------- #
    def register_device(self, name: str, device: Device) -> None:
        """登记一个设备实例,便于统一 close。"""
        self._devices[name] = device

    def register_route(self, route: Route) -> None:
        """注册一条触发路由。同 key 会覆盖。"""
        self._routes[route.key] = route
        logger.info(
            f"路由注册: {route.key} → {route.device} (cooldown={route.cooldown}s)"
        )

    # ---------- 业务调用入口 ---------- #
    def dispatch(self, mode: str, action: str, /, **payload: Any) -> bool:
        """业务层调用。立即返回(不阻塞业务线程)。

        Args:
            mode: 业务模式 —— positional-only,避免和 payload 里同名 key 冲突
            action: 动作名 —— positional-only,同上
            **payload: 透传给设备的运行时参数;允许携带名为 "mode"/"action" 的键

        Returns:
            True 表示事件已入队等待执行
            False 表示无路由 / 队列满 / 处于冷却中
        """
        key = f"{mode}.{action}"
        route = self._routes.get(key)
        if route is None:
            logger.debug(f"无路由: {key} (忽略)")
            return False

        now = time.time()
        if now - route.last_fire < route.cooldown:
            logger.debug(
                f"路由 {key} 冷却中 (剩余 {route.cooldown - (now - route.last_fire):.2f}s)"
            )
            return False
        route.last_fire = now

        ev = _Event(mode=mode, action=action, payload=dict(payload), ts=now)
        try:
            self._queue.put_nowait(ev)
            return True
        except queue.Full:
            logger.warning(f"事件队列已满,丢弃 {key}")
            return False

    # ---------- worker ---------- #
    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                ev = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if ev is None:
                break  # 关闭信号
            self._exec(ev)

    def _exec(self, ev: _Event) -> None:
        key = f"{ev.mode}.{ev.action}"
        route = self._routes.get(key)
        if route is None:
            return
        merged: Dict[str, Any] = dict(route.default_payload)
        merged.update(ev.payload)
        try:
            logger.info(f"执行 {key} → {route.device} payload={merged}")
            route.device.trigger(merged)
        except DeviceError as exc:
            logger.error(f"[{key}] 设备错误: {exc}")
        except Exception as exc:  # 不让任何异常逃逸 worker
            logger.exception(f"[{key}] 未预期异常: {exc}")

    # ---------- 生命周期 ---------- #
    def close(self) -> None:
        """停止 worker,关闭所有设备。"""
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._worker.join(timeout=1.0)
        for name, dev in self._devices.items():
            try:
                dev.close()
            except Exception:
                logger.exception(f"关闭设备 {name} 失败")
        logger.info("ActionDispatcher 已关闭")


# ---------- 从配置构建 ---------- #
def build_dispatcher_from_config(config_path: Optional[str] = None) -> ActionDispatcher:
    """根据 YAML 配置创建并初始化一个 dispatcher。

    yaml 缺失 / 解析失败 / 设备初始化失败时,自动降级到 dummy,保证服务能起来。
    具体加载逻辑见 devices/factory.py。
    """
    from .factory import load_dispatcher

    return load_dispatcher(config_path)

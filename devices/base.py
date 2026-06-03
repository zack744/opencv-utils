"""设备抽象基类。

所有具体设备（继电器、蜂鸣器、舵机锁、HTTP/MQTT 远程设备...）都继承 Device，
实现 trigger(payload) 接口即可被 ActionDispatcher 调用。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


logger = logging.getLogger("devices")


class DeviceError(Exception):
    """设备相关错误（初始化失败、GPIO 异常、HTTP 不通等）。"""


class Device(ABC):
    """设备抽象基类。

    子类需要：
        - 在 __init__ 里完成硬件初始化（GPIO setup / 串口打开 / HTTP session ...）
        - 实现 trigger(payload) —— 一次触发动作（高电平 N 毫秒 / 转角 N 度 / 发请求...）
        - 可选：重写 close() 做资源清理

    约束：
        - trigger() 应该是“一次完整动作”而不是“开始 + 等用户停止”
          脉冲时长、保持时间等参数都通过 payload 传入或在 __init__ 配置
        - trigger() 允许阻塞，因为它在 dispatcher 的 worker 线程里执行，
          不会卡住摄像头线程
        - trigger() 内部不要抛异常逃逸；捕获后转 DeviceError 或 log.error
    """

    #: 子类可重写，用于日志和配置匹配
    kind: str = "abstract"

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        self.name = name
        self.config = config or {}
        logger.info(f"[{self.kind}:{self.name}] 初始化")

    @abstractmethod
    def trigger(self, payload: Optional[Dict[str, Any]] = None) -> None:
        """执行一次设备动作。

        Args:
            payload: 业务层传过来的运行时参数（如 duration、angle、message）。
                     可与 self.config 合并；payload 优先级更高。
        """

    def close(self) -> None:
        """释放硬件资源（GPIO cleanup / 关串口 / 关 session）。"""
        logger.info(f"[{self.kind}:{self.name}] 关闭")

    # ---------- 工具方法 ---------- #
    def _param(self, payload: Optional[Dict[str, Any]], key: str, default: Any) -> Any:
        """优先取 payload 里的参数，其次取 config 里的，最后回退到 default。"""
        if payload and key in payload:
            return payload[key]
        if key in self.config:
            return self.config[key]
        return default

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.name}>"

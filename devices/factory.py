"""设备工厂 + YAML 配置加载。

把 config/devices.yaml 这种声明式配置变成一个可用的 ActionDispatcher。

整体策略:**配置缺失/解析失败/单个设备初始化失败 都不让服务起不来**,
直接降级到 DummyDevice,日志里能看到原因。这样开发机和缺硬件的测试机都能跑。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .base import Device, DeviceError
from .dummy import DummyDevice
from .dispatcher import ActionDispatcher, Route


logger = logging.getLogger("devices.factory")


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "devices.yaml"


# 已知设备类型注册表。新增设备类型,在这里加一行即可。
def _registry() -> Dict[str, type]:
    from .relay import RelayDevice
    from .buzzer import BuzzerDevice
    from .servo_lock import ServoLockDevice
    from .arduino import ArduinoDevice
    from .ewelink_relay import EwelinkRelay
    return {
        "dummy": DummyDevice,
        "relay": RelayDevice,
        "buzzer": BuzzerDevice,
        "servo_lock": ServoLockDevice,
        "arduino": ArduinoDevice,
        "ewelink_relay": EwelinkRelay,
        # TODO: "http": HttpDevice, "mqtt": MqttDevice ...
    }


def _build_device(name: str, kind: str, config: Dict[str, Any]) -> Device:
    """根据 kind 字符串实例化对应设备类,失败时降级为 dummy。"""
    registry = _registry()
    cls = registry.get(kind)
    if cls is None:
        logger.warning(f"未知设备类型 '{kind}' (device={name}), 降级为 dummy")
        return DummyDevice(name=name, config=config)
    try:
        return cls(name=name, config=config)
    except DeviceError as exc:
        logger.error(f"设备 {name}({kind}) 初始化失败: {exc} → 降级为 dummy")
        return DummyDevice(name=name, config=config)
    except Exception as exc:
        logger.exception(f"设备 {name}({kind}) 异常: {exc} → 降级为 dummy")
        return DummyDevice(name=name, config=config)


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        logger.info(f"配置文件不存在: {path} (所有路由都跑空,不会触发任何外设)")
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        logger.warning("pyyaml 未安装,跳过外设配置 (pip install pyyaml)")
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            logger.warning(f"配置文件根节点不是 mapping: {path}")
            return {}
        return data
    except Exception as exc:
        logger.exception(f"读取配置失败 {path}: {exc}")
        return {}


def load_dispatcher(config_path: Optional[str] = None) -> ActionDispatcher:
    """从 YAML 配置创建 ActionDispatcher。

    配置格式 (config/devices.yaml):

        # 顶层 key = 模式名 (closed_eye / bite_finger / pushup)
        # 二级 key = 动作名 (业务里 emit_action(action_name) 用)
        closed_eye:
          shock:
            device: relay       # 设备类型
            name: shock_relay   # 可选,日志用
            pin: 17
            pulse: 0.3
            cooldown: 2.0       # 这条路由的冷却

        pushup:
          unlock:
            device: servo_lock
            pin: 12
            open_angle: 90
            hold: 5
            cooldown: 10
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    cfg = _load_yaml(path)

    dispatcher = ActionDispatcher()
    if not cfg:
        return dispatcher

    # 同一个设备实例如果被多个路由复用,按 name 缓存
    device_cache: Dict[str, Device] = {}

    for mode, actions in cfg.items():
        if not isinstance(actions, dict):
            logger.warning(f"模式 {mode} 配置异常 (不是 mapping),跳过")
            continue
        for action, params in actions.items():
            if not isinstance(params, dict):
                logger.warning(f"路由 {mode}.{action} 配置异常,跳过")
                continue

            kind = str(params.get("device", "dummy"))
            dev_name = str(params.get("name") or f"{mode}_{action}")
            cooldown = float(params.get("cooldown", 1.0))

            # 路由级参数(传给 trigger 的 default payload)和设备级参数分离
            # 这里偷懒不分,全部一起塞进 device config
            if dev_name not in device_cache:
                device = _build_device(dev_name, kind, dict(params))
                device_cache[dev_name] = device
                dispatcher.register_device(dev_name, device)
            else:
                device = device_cache[dev_name]

            dispatcher.register_route(Route(
                mode=mode,
                action=action,
                device=device,
                cooldown=cooldown,
                default_payload=dict(params),
            ))

    if not dispatcher._routes:
        logger.info("没有任何路由被注册,所有 emit_action 都会被忽略")
    return dispatcher

"""eWeLink 智能继电器 - 走云端 HTTP API 控制 SDY-002 / CK-BL602 系列板子。

适用板子:
    SDY-002_V1.2 单路/多路 蓝牙+WIFI 点动自锁模块 DC 5V Pro
    固件: CK-BL602-4SW-HS-03 (4 路, uiid 138)

为什么不直接用 projects 里的 relay.py (gpiozero)?
    这块板子的 MCU (BL602) 已经吃掉了继电器线圈,
    板子没暴露外部触发脚给 Pi 用。所以"Pi 控继电器"
    只能走 WiFi/蓝牙,云端 HTTP 是最稳的方案。

依赖: pip install requests
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional

import requests

from .base import Device, DeviceError


logger = logging.getLogger("devices.ewelink_relay")


# eWeLink 各区域 API 入口 (v2, -apia 后缀)
REGION_API: Dict[str, str] = {
    "cn": "https://cn-apia.coolkit.cn",
    "us": "https://us-apia.coolkit.cc",
    "eu": "https://eu-apia.coolkit.cc",
    "as": "https://as-apia.coolkit.cc",
}

# 公开 appid / appsecret (从 SonoffLAN 项目反向得到的当前可用凭证)
EWELINK_APPID = "EWELINK_APPID_REDACTED"
EWELINK_APPSECRET = "EWELINK_APPSECRET_REDACTED"

# 4 路板子: outlet 取值 0~3
VALID_OUTLETS = (0, 1, 2, 3)


class EwelinkRelay(Device):
    """eWeLink 智能继电器 - 走云端 HTTP 控制单路 / 多路板子。

    配置示例 (config/devices.yaml):
        device: ewelink_relay
        name: door_lock
        phone: "182xxxxxxxx"        # 纯手机号, +86 前缀自动加
        password: "你的eWeLink密码"
        device_id: "1002xxxxxxxxxx"  # 易微联 App 设备信息里查
        region: cn                  # cn / us / eu / as
        outlet: 0                   # 4 路板子: 0~3, 单路板子填 0
        pulse: 2.0                  # ON 持续秒数, 然后自动 OFF
    """

    kind = "ewelink_relay"

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)

        # 凭证 + 设备定位
        self.phone: str = str(self.config.get("phone", "")).strip()
        self.password: str = str(self.config.get("password", "")).strip()
        self.device_id: str = str(self.config.get("device_id", "")).strip()
        self.region: str = str(self.config.get("region", "cn")).strip().lower()
        self.outlet: int = int(self.config.get("outlet", 0))
        self.default_pulse: float = float(self.config.get("pulse", 0.3))

        # 配置校验 (factory 捕获 DeviceError 后会降级 dummy)
        if not self.phone or not self.password:
            raise DeviceError(f"[{name}] 缺少 phone/password")
        if not self.device_id:
            raise DeviceError(f"[{name}] 缺少 device_id (在 eWeLink App 里查)")
        if self.region not in REGION_API:
            raise DeviceError(
                f"[{name}] region={self.region!r} 不支持, 可选: {list(REGION_API)}"
            )
        if self.outlet not in VALID_OUTLETS:
            raise DeviceError(
                f"[{name}] outlet={self.outlet} 不在 {VALID_OUTLETS} 范围"
            )

        self.base_url: str = REGION_API[self.region]
        self._token: Optional[str] = None
        self._token_exp: float = 0.0

        # 启动时主动登录一次;失败不抛,降级为 dummy (符合 factory 约定)
        try:
            self._login()
        except DeviceError as exc:
            logger.warning(
                f"[ewelink:{name}] 初始化登录失败,降级为 dummy: {exc}"
            )
            self._token = None

    # ---------- 签名 + 认证 ---------- #
    @staticmethod
    def _sign(body_bytes: bytes) -> str:
        """eWeLink v2 登录签名: base64(HMAC-SHA256(appSecret, body_bytes))"""
        sig = hmac.new(
            EWELINK_APPSECRET.encode("utf-8"),
            body_bytes,
            hashlib.sha256,
        ).digest()
        return base64.b64encode(sig).decode("utf-8")

    def _login(self) -> None:
        # phoneNumber 必带 +86 前缀, countryCode 单独填
        phone_with_prefix = (
            self.phone if self.phone.startswith("+") else f"+86{self.phone}"
        )

        body: Dict[str, Any] = {
            "countryCode": "+86",
            "phoneNumber": phone_with_prefix,
            "password": self.password,
            "version": 8,
            "nonce": str(int(time.time() * 1000)),
            "ts": int(time.time()),
            "appid": EWELINK_APPID,
        }
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        sign = self._sign(body_bytes)

        try:
            r = requests.post(
                f"{self.base_url}/v2/user/login",
                data=body_bytes,
                headers={
                    "X-CK-Appid": EWELINK_APPID,
                    "Authorization": f"Sign {sign}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as exc:
            raise DeviceError(f"eWeLink 登录 HTTP 失败: {exc}") from exc
        except ValueError as exc:
            raise DeviceError(f"eWeLink 登录返回非 JSON: {exc}") from exc

        err = data.get("error")
        if err not in (0, None):
            raise DeviceError(
                f"eWeLink 登录失败: error={err} msg={data.get('msg')!r}"
            )

        token = (data.get("data") or {}).get("at")
        if not token:
            raise DeviceError("eWeLink 登录返回无 token")

        self._token = token
        # eWeLink access_token 默认 30 天,这里设 1 小时保险起见自动重登
        self._token_exp = time.time() + 3600 - 120
        logger.info(
            f"[ewelink:{self.name}] 登录成功 region={self.region} "
            f"outlet={self.outlet} device_id={self.device_id}"
        )

    def _ensure_token(self) -> None:
        if self._token is None or time.time() >= self._token_exp:
            self._login()

    # ---------- 控制 ---------- #
    def _control(self, switch_on: bool) -> None:
        """单路控制: ON 或 OFF."""
        # dummy 模式: 不发请求,只打日志
        if self._token is None:
            logger.info(
                f"[ewelink:{self.name}] [dummy] outlet={self.outlet} "
                f"{'ON' if switch_on else 'OFF'}"
            )
            return

        self._ensure_token()

        body: Dict[str, Any] = {
            "type": 1,
            "id": self.device_id,
            "params": {
                "switches": [
                    {"switch": "on" if switch_on else "off", "outlet": self.outlet}
                ]
            },
        }
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")

        try:
            r = requests.post(
                f"{self.base_url}/v2/device/thing/status",
                data=body_bytes,
                headers={
                    "X-CK-Appid": EWELINK_APPID,
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as exc:
            raise DeviceError(f"eWeLink 控制 HTTP 失败: {exc}") from exc
        except ValueError as exc:
            raise DeviceError(f"eWeLink 控制返回非 JSON: {exc}") from exc

        err = data.get("error")
        if err != 0:
            err_msg = data.get("msg", "")
            # token 失效重试一次
            if err == 401:
                self._token = None
                self._ensure_token()
                # 重试一次
                return self._control(switch_on)
            raise DeviceError(
                f"eWeLink 控制失败: error={err} msg={err_msg!r}"
            )

        logger.info(
            f"[ewelink:{self.name}] outlet={self.outlet} → "
            f"{'ON' if switch_on else 'OFF'}  (设备 {self.device_id})"
        )

    # ---------- Device 接口实现 ---------- #
    def trigger(self, payload: Optional[Dict[str, Any]] = None) -> None:
        """业务层入口: ON 一段时间,然后 OFF.

        pulse 参数 (秒):
            - payload 里的 pulse 优先
            - 否则用 __init__ 配置的 default_pulse
            - 限幅 0~60s
        """
        pulse = float(self._param(payload, "pulse", self.default_pulse))
        pulse = max(0.0, min(pulse, 60.0))

        self._control(True)
        try:
            time.sleep(pulse)
        finally:
            self._control(False)

    def close(self) -> None:
        """服务关闭时复位到 OFF 状态。"""
        try:
            if self._token is not None:
                self._control(False)
        except Exception:
            logger.exception(f"[ewelink:{self.name}] 关闭时复位失败")
        super().close()

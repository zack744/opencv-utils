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
import os
import time
from pathlib import Path
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

# 4 路板子: outlet 取值 0~3
VALID_OUTLETS = (0, 1, 2, 3)


def _load_dotenv() -> None:
    """从项目根的 .env 加载环境变量(简单 KEY=VALUE 解析,不依赖 python-dotenv)。

    仅当变量尚未设置时才覆盖,避免覆盖进程已注入的环境变量。
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError as exc:
        logger.warning(f"读取 {env_path} 失败: {exc}")


def _get_credential(env_key: str) -> Optional[str]:
    """读 .env / 系统环境变量里的 eWeLink 凭证。"""
    val = os.environ.get(env_key, "").strip()
    return val or None


# 模块加载时尝试读 .env(优先;命令行/系统环境变量已在 os.environ 里的更高优先)
_load_dotenv()


class EwelinkRelay(Device):
    """eWeLink 智能继电器 - 走云端 HTTP 控制单路 / 多路板子。

    凭证(appid / appsecret / phone / password)统一从项目根的 .env 文件
    或系统环境变量读取,不要硬编码或写到 config/*.yaml 里(会被推到 GitHub)。

    必需环境变量(参考 .env.example):
        EWELINK_APPID
        EWELINK_APPSECRET
        EWELINK_PHONE         (纯手机号, +86 前缀自动加)
        EWELINK_PASSWORD
        EWELINK_DEVICE_ID     (易微联 App 设备信息里查)
        EWELINK_REGION        (cn / us / eu / as, 默认 cn)

    配置示例 (config/devices.yaml) —— 只放非敏感字段:
        device: ewelink_relay
        name: door_lock
        device_id: "1002xxxxxxxxxx"
        region: cn
        outlet: 0
        pulse: 2.0
    """

    kind = "ewelink_relay"

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)

        # 凭证 + 设备定位
        # 优先: config 显式传 → .env / 系统环境变量
        self.phone: str = str(
            self.config.get("phone") or _get_credential("EWELINK_PHONE") or ""
        ).strip()
        self.password: str = str(
            self.config.get("password") or _get_credential("EWELINK_PASSWORD") or ""
        ).strip()
        self.device_id: str = str(
            self.config.get("device_id") or _get_credential("EWELINK_DEVICE_ID") or ""
        ).strip()
        self.region: str = str(
            self.config.get("region") or _get_credential("EWELINK_REGION") or "cn"
        ).strip().lower()
        self.outlet: int = int(self.config.get("outlet", 0))
        self.default_pulse: float = float(self.config.get("pulse", 0.3))

        # appid / appsecret 仅从 .env 或环境变量读(永不放在 yaml / 配置文件里)
        self._appid: Optional[str] = _get_credential("EWELINK_APPID")
        self._appsecret: Optional[str] = _get_credential("EWELINK_APPSECRET")

        # 配置校验 (factory 捕获 DeviceError 后会降级 dummy)
        if not self.phone or not self.password:
            raise DeviceError(
                f"[{name}] 缺少 phone/password (检查 config/*.yaml 或 .env 里的 "
                f"EWELINK_PHONE / EWELINK_PASSWORD)"
            )
        if not self.device_id:
            raise DeviceError(
                f"[{name}] 缺少 device_id (检查 config/*.yaml 或 .env 里的 "
                f"EWELINK_DEVICE_ID)"
            )
        if not self._appid or not self._appsecret:
            raise DeviceError(
                f"[{name}] 缺少 EWELINK_APPID / EWELINK_APPSECRET "
                f"(从项目根的 .env 或系统环境变量注入)"
            )
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
    def _sign(self, body_bytes: bytes) -> str:
        """eWeLink v2 登录签名: base64(HMAC-SHA256(appSecret, body_bytes))"""
        assert self._appsecret, "appsecret 未加载 (构造函数应已校验)"
        sig = hmac.new(
            self._appsecret.encode("utf-8"),
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
            "appid": self._appid,
        }
        body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
        sign = self._sign(body_bytes)

        try:
            r = requests.post(
                f"{self.base_url}/v2/user/login",
                data=body_bytes,
                headers={
                    "X-CK-Appid": self._appid,
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
                    "X-CK-Appid": self._appid,
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
        """业务层入口: 业务触发时 锁断电(板子 OFF, NC 端子断开)→ 锁芯收回 → 门开,
        pulse 秒后 锁通电(板子 ON, NC 端子闭合)→ 锁芯弹出 → 门上锁.

        适用场景: 锁是 fail-safe (通电锁住, 断电开) —— 你家的电子锁就是这种.

        pulse 参数 (秒):
            - payload 里的 pulse 优先
            - 否则用 __init__ 配置的 default_pulse
            - 限幅 0.5~15s (上限 15s: eWeLink 云端对同 outlet 短间隔连发会 4002 节流)

        ⚠️ 复位的 _control(True) 如果失败 (常见: eWeLink 4002 节流),
            不会向上抛异常 —— 业务事件已经成功 (门已经开过),不复位的后果
            只是锁多保持一会儿"门开"状态,下次业务触发时再覆盖。
            若真需要"上锁",让用户手动在易微联 App 里点一下。
        """
        pulse = float(self._param(payload, "pulse", self.default_pulse))
        pulse = max(0.5, min(pulse, 15.0))  # 上限收紧到 15s,避开云端 4002 节流

        self._control(False)  # 业务触发 = 板子 OFF = 锁断电 = 锁芯收回 = 门开
        try:
            time.sleep(pulse)
        finally:
            # 复位失败容忍: 不抛异常,只打 ERROR,避免覆盖上层业务日志
            try:
                self._control(True)   # 复位 = 板子 ON = 锁通电 = 锁芯弹出 = 门锁住
            except DeviceError as exc:
                logger.error(
                    f"[ewelink:{self.name}] 复位上锁失败 (云端可能节流): {exc} "
                    f"—— 锁暂时保持门开状态,下次触发或手动在易微联 App 点 ON 即可恢复"
                )

    def close(self) -> None:
        """服务关闭时复位到 ON 状态 (锁通电上锁, fail-safe 锁的常态)."""
        try:
            if self._token is not None:
                self._control(True)
        except Exception:
            logger.exception(f"[ewelink:{self.name}] 关闭时复位失败")
        super().close()

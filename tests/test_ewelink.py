"""eWeLink 智能继电器 - 云端连通性测试 (v2 API)。

不依赖项目其他模块(requests 除外)。拿到设备/账号后第一时间验证:
    1. 云端 API 登录通不通
    2. 设备在不在线
    3. ON / OFF 能不能真正触发 (继电器响不响)

========================================================================
重要信息 (基于对当前 eWeLink v2 API 的实际探测):
========================================================================
  - 域名: https://cn-apia.coolkit.cn   (注意是 -apia, 不是 -api)
  - appid: EWELINK_APPID_REDACTED
  - 控制接口: /v2/device/thing/status   (老的 /v2/device/control 已废弃)
  - 登录接口: /v2/user/login
  - 登录 Sign 算法: base64(HMAC-SHA256(appSecret, body_bytes))
  - 你的设备是 4 路板子 (CK-BL602-4SW-HS-03, uiid 138)
    控制格式: params: {switches: [{switch: "on", "off", outlet: 0..3}]}

========================================================================
跑法 1: 命令行参数
========================================================================
    cd D:\\project\\OpenCV
    E:\\anaconda\\envs\\opencv-web\\python.exe tests\\test_ewelink.py `
        --phone 182xxxxxxxx `
        --password "你的密码" `
        --device-id 1002b42d5b `
        --outlet 0 `
        --pulse 1.0

========================================================================
跑法 2: 环境变量
========================================================================
    $env:EWELINK_PHONE='182xxxxxxxx'
    $env:EWELINK_PASSWORD='你的密码'
    $env:EWELINK_DEVICE_ID='1002b42d5b'
    E:\\anaconda\\envs\\opencv-web\\python.exe tests\\test_ewelink.py

========================================================================
期望输出 (成功时)
========================================================================
    [步骤 1] 登录 eWeLink (cn 区)
      ✓ 登录成功
    [步骤 2] 查询设备状态
      ✓ 设备在线, 当前所有开关: ['off', 'off', 'off', 'off']
    [步骤 3] 触发 outlet 0 ON
      ✓ 服务器已接收, 继电器应该已吸合
    [步骤 4] 等待 1.0 秒
    [步骤 5] 触发 outlet 0 OFF
      ✓ 服务器已接收, 继电器应该已释放
    全部通过 - 云端链路 OK
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests


# ---------- eWeLink 公共配置 ---------- #
APPID = "EWELINK_APPID_REDACTED"
APPSECRET = "EWELINK_APPSECRET_REDACTED"

REGION_API: Dict[str, str] = {
    "cn": "https://cn-apia.coolkit.cn",
    "us": "https://us-apia.coolkit.cc",
    "eu": "https://eu-apia.coolkit.cc",
    "as": "https://as-apia.coolkit.cc",
}


# ---------- 打印工具 ---------- #
def step(n: int, title: str) -> None:
    print()
    print("=" * 60)
    print(f"[步骤 {n}] {title}")
    print("=" * 60)


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")


# ---------- eWeLink API ---------- #
def _sign(body_bytes: bytes) -> str:
    """eWeLink v2 登录签名: base64(HMAC-SHA256(secret, body_bytes))"""
    sig = hmac.new(APPSECRET.encode("utf-8"), body_bytes, hashlib.sha256).digest()
    return base64.b64encode(sig).decode("utf-8")


def api_login(phone: str, password: str, region: str) -> Dict[str, Any]:
    """登录拿 token。失败抛 RuntimeError, 错误消息已带排错建议。"""
    base_url = REGION_API[region]

    # 注意: phoneNumber 必须带 + 前缀, countryCode 只是国家码 (如 +86)
    phone_with_prefix = phone if phone.startswith("+") else f"+86{phone}"
    country_code = "+86"  # 中国用户固定 +86, 其它国家改这里或从 phone_with_prefix 拆

    body = {
        "countryCode": country_code,
        "phoneNumber": phone_with_prefix,
        "password": password,
        "version": 8,
        "nonce": str(int(time.time() * 1000)),
        "ts": int(time.time()),
        "appid": APPID,
    }
    body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
    sign = _sign(body_bytes)

    try:
        r = requests.post(
            f"{base_url}/v2/user/login",
            data=body_bytes,
            headers={
                "X-CK-Appid": APPID,
                "Authorization": f"Sign {sign}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"HTTP 失败 - 网络问题: {exc}\n"
            f"  排错: 检查树莓派能不能访问 {base_url}"
        ) from exc

    try:
        data = r.json()
    except ValueError as exc:
        raise RuntimeError(
            f"返回非 JSON - 服务器可能挂了或被防火墙拦了: HTTP {r.status_code}\n"
            f"  原始响应: {r.text[:200]}"
        ) from exc

    err = data.get("error")
    if err in (0, None):
        return data

    err_msg = data.get("msg", "")
    if err == 10004:
        hint = "账号区域不匹配, 试 --region us/eu/as 切换"
    elif err == 1001:
        hint = "手机号/邮箱格式错 - 检查 phoneNumber 要带 +86 前缀"
    elif err == 1004 or err == 101:
        hint = "密码错, 或账号有 2FA - 在 eWeLink App 里临时关掉 2FA"
    elif err == 1106:
        hint = "密码错误次数太多被封 - 等 30 分钟"
    else:
        hint = f"未知错误, 把这个 JSON 给我: {data}"
    raise RuntimeError(f"登录失败 error={err} msg='{err_msg}'\n  排错: {hint}")


def api_list_devices(token: str, region: str) -> List[Dict[str, Any]]:
    """查账号下所有设备。"""
    base_url = REGION_API[region]
    r = requests.get(
        f"{base_url}/v2/device/thing",
        params={"num": 0},
        headers={"Authorization": f"Bearer {token}", "X-CK-Appid": APPID},
        timeout=10,
    )
    data = r.json()
    if data.get("error") != 0:
        raise RuntimeError(f"查设备失败: {data}")
    things = data.get("data", {}).get("thingList", [])
    return [t.get("itemData", {}) for t in things if "deviceid" in t.get("itemData", {})]


def api_control(token: str, device_id: str, outlet: int, on: bool, region: str) -> Dict[str, Any]:
    """控制 4 路板子的某一路 (outlet 0~3)。"""
    base_url = REGION_API[region]
    body = {
        "type": 1,
        "id": device_id,
        "params": {"switches": [{"switch": "on" if on else "off", "outlet": outlet}]},
    }
    body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
    r = requests.post(
        f"{base_url}/v2/device/thing/status",
        data=body_bytes,
        headers={
            "X-CK-Appid": APPID,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    data = r.json()
    err = data.get("error")
    if err == 0:
        return data
    err_msg = data.get("msg", "")
    if err == 4002:
        hint = (
            f"控制失败 - 设备可能不在线, 或 appid 不被这个设备支持\n"
            f"  排错: 1) App 里看设备在不在线; 2) 试 --outlet 0~3 全部; "
            f"3) 重启板子"
        )
    elif err == 401:
        hint = "token 失效 - 重新登录 (本脚本每次跑都重新登录, 应该不会遇到)"
    else:
        hint = f"未知, 给我这个 JSON: {data}"
    raise RuntimeError(f"控制失败 error={err} msg='{err_msg}'\n  排错: {hint}")


# ---------- 主流程 ---------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="eWeLink 智能继电器 v2 API 连通性测试 (4 路板子专用)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "环境变量: EWELINK_PHONE / EWELINK_PASSWORD / EWELINK_DEVICE_ID / "
            "EWELINK_REGION / EWELINK_OUTLET"
        ),
    )
    parser.add_argument("--phone", help="eWeLink 登录手机号 (纯数字, 如 182xxxxxxxx)")
    parser.add_argument("--password", help="eWeLink 登录密码")
    parser.add_argument("--device-id", help="设备 ID (在易微联 App 设备信息里看)")
    parser.add_argument("--outlet", type=int, default=0, choices=[0, 1, 2, 3],
                        help="控制哪一路 (4 路板子, 0~3, 默认 0)")
    parser.add_argument("--region", default="cn", choices=list(REGION_API.keys()),
                        help="账号区域, 中国用户用 cn (默认)")
    parser.add_argument("--pulse", type=float, default=1.0,
                        help="ON → 等待 → OFF 的等待秒数 (默认 1.0)")
    parser.add_argument("--no-trigger", action="store_true",
                        help="只登录 + 查设备, 不触发继电器")
    args = parser.parse_args()

    # 合并命令行 / 环境变量
    phone = args.phone or os.environ.get("EWELINK_PHONE", "")
    password = args.password or os.environ.get("EWELINK_PASSWORD", "")
    device_id = args.device_id or os.environ.get("EWELINK_DEVICE_ID", "")
    region = args.region or os.environ.get("EWELINK_REGION", "cn")
    outlet = int(os.environ.get("EWELINK_OUTLET", args.outlet))

    # 参数检查
    missing = []
    if not phone:
        missing.append("--phone / EWELINK_PHONE")
    if not password:
        missing.append("--password / EWELINK_PASSWORD")
    if not args.no_trigger and not device_id:
        missing.append("--device-id / EWELINK_DEVICE_ID")
    if missing:
        print(f"ERROR: 缺少参数: {', '.join(missing)}", file=sys.stderr)
        parser.print_help()
        return 1

    # 脱敏打印
    phone_masked = phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else "***"
    print(f"账号: {phone_masked}  区域: {region}  设备ID: {device_id or '(跳过)'}")
    print(f"控制 outlet: {outlet}  脉冲时长: {args.pulse} 秒")

    # ---------- 步骤 1: 登录 ---------- #
    step(1, f"登录 eWeLink ({region} 区, appid={APPID[:8]}...)")
    try:
        data = api_login(phone, password, region)
    except RuntimeError as exc:
        fail(str(exc))
        return 2
    token = (data.get("data") or {}).get("at", "")
    if not token:
        fail(f"返回里没 token, 整包: {data}")
        return 2
    ok(f"登录成功, token 前 16 位: {token[:16]}...")

    if args.no_trigger:
        # 顺便查一下账号下所有设备
        step(2, "查询账号下所有设备")
        try:
            devices = api_list_devices(token, region)
        except RuntimeError as exc:
            fail(str(exc))
            return 2
        ok(f"找到 {len(devices)} 个设备:")
        for d in devices:
            extra = d.get("extra", {})
            params = d.get("params", {}) or {}
            switches = params.get("switches", [])
            state = [s.get("switch", "?") for s in switches] or ["(无开关)"]
            online = "在线" if d.get("online") else "离线"
            print(
                f"    - {d.get('deviceid')}  {d.get('name', '?')[:30]}\n"
                f"        模型: {extra.get('model', '?')}, 状态: {online}\n"
                f"        开关: {state}"
            )
        print()
        print("✅ 登录 + 设备查询通过, 账号密码 OK")
        return 0

    # ---------- 步骤 2: 查设备状态 ---------- #
    step(2, f"查询设备 {device_id} 状态")
    try:
        devices = api_list_devices(token, region)
    except RuntimeError as exc:
        fail(str(exc))
        return 2
    target = next((d for d in devices if d.get("deviceid") == device_id), None)
    if target is None:
        fail(f"设备 {device_id} 不在账号下, 现有设备: {[d.get('deviceid') for d in devices]}")
        return 2
    if not target.get("online"):
        fail("设备离线! 请检查 WiFi 和电源")
        return 2
    switches = (target.get("params", {}) or {}).get("switches", [])
    state = [s.get("switch", "?") for s in switches] or ["(无开关)"]
    ok(f"设备在线, 4 路开关当前状态: {state}")

    # ---------- 步骤 3: ON ---------- #
    step(3, f"触发 outlet {outlet} ON  (继电器应该'嗒'一声吸合)")
    try:
        api_control(token, device_id, outlet, True, region)
    except RuntimeError as exc:
        fail(str(exc))
        return 3
    ok("服务器已接收, 板子应该在 1~3 秒内吸合")

    # ---------- 步骤 4: 等待 ---------- #
    step(4, f"等待 {args.pulse} 秒")
    time.sleep(args.pulse)

    # ---------- 步骤 5: OFF ---------- #
    step(5, f"触发 outlet {outlet} OFF  (继电器应该'嗒'一声释放)")
    try:
        api_control(token, device_id, outlet, False, region)
    except RuntimeError as exc:
        fail(str(exc))
        return 4
    ok("服务器已接收, 板子应该已释放")

    print()
    print("=" * 60)
    print("✅ 全部通过 - 云端链路 OK, 继电器可以控制")
    print("=" * 60)
    print()
    print("下一步:")
    print("  1. 听一下板子有没有'嗒嗒'两声, App 里看状态有没有跟着变")
    print("  2. 测别的 outlet: --outlet 1 / 2 / 3")
    print("  3. 一切 OK 后, 物理接线 (NO/COM + 12V 电源 + 电磁锁)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

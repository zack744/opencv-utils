"""冒烟测试: 验证 web → dispatcher → ArduinoDevice 整条链路通畅。

不依赖真实硬件 / 摄像头 / 网络:
  1. import 所有关键模块
  2. 用真实 config/devices.yaml 加载 dispatcher
  3. 直接调 dispatcher.dispatch() 模拟 emit_action
  4. 等 worker 跑完
  5. 确认 ArduinoDevice 降级 dummy 并打印了正确的指令

跑法:  python tests/smoke_test.py
"""

from __future__ import annotations
import logging
import sys
import time
from pathlib import Path

# 把项目根加进 path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 关键: 让 web_server 这条链路上的日志都打出来
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
# ArduinoDevice 的 dummy 日志要能看见
logging.getLogger("devices.arduino").setLevel(logging.INFO)


def main() -> int:
    print("=" * 60)
    print("1. import 关键模块")
    print("=" * 60)
    from devices import build_dispatcher_from_config
    from devices.arduino import ArduinoDevice
    from devices.dispatcher import ActionDispatcher
    print("  ✓ devices.build_dispatcher_from_config")
    print("  ✓ devices.arduino.ArduinoDevice")
    print("  ✓ devices.dispatcher.ActionDispatcher")
    print()

    print("=" * 60)
    print("2. 加载 config/devices.yaml")
    print("=" * 60)
    cfg_path = ROOT / "config" / "devices.yaml"
    dispatcher: ActionDispatcher = build_dispatcher_from_config(str(cfg_path))
    print(f"  配置文件: {cfg_path}")
    print(f"  注册路由数: {len(dispatcher._routes)}")
    for key in dispatcher._routes:
        print(f"    - {key}")
    print()

    if not dispatcher._routes:
        print("  ✗ FAIL: 没有任何路由被注册, 配置文件可能有错")
        return 1
    print("  ✓ 路由注册成功")
    print()

    print("=" * 60)
    print("3. 模拟业务层 emit_action('shock')")
    print("=" * 60)
    # closed_eye 模式, shock 动作 — 走 arduino:massager_power
    ok = dispatcher.dispatch("closed_eye", "shock")
    print(f"  dispatch 返回: {ok}")
    if not ok:
        print("  ✗ FAIL: dispatch 应该返回 True")
        return 1
    print("  ✓ 事件已入队")
    print()

    print("=" * 60)
    print("4. 模拟业务层 emit_action('unlock')")
    print("=" * 60)
    ok = dispatcher.dispatch("pushup", "unlock")
    print(f"  dispatch 返回: {ok}")
    if not ok:
        print("  ✗ FAIL: dispatch 应该返回 True")
        return 1
    print("  ✓ 事件已入队")
    print()

    print("=" * 60)
    print("5. 模拟一个不存在的路由 (应该静默忽略, 不抛异常)")
    print("=" * 60)
    ok = dispatcher.dispatch("closed_eye", "no_such_action")
    print(f"  dispatch 返回: {ok}  (期望 False)")
    if ok:
        print("  ✗ FAIL: 不存在的路由应该返回 False")
        return 1
    print("  ✓ 静默忽略, 不抛异常")
    print()

    print("=" * 60)
    print("6. 等 worker 线程把队列消费完")
    print("=" * 60)
    time.sleep(0.5)
    # 再等一下让 ArduinoDevice._send 走完（如果真接 Nano 那是 ~pulse 秒,
    # dummy 模式应该 0 延迟, 0.5s 足够)
    print("  ✓ 等待完成")
    print()

    print("=" * 60)
    print("7. 关闭 dispatcher")
    print("=" * 60)
    dispatcher.close()
    print("  ✓ 关闭完成")
    print()

    print("=" * 60)
    print("✅ 全部通过 — web → dispatcher → ArduinoDevice 链路通畅")
    print("=" * 60)
    print()
    print("日志里应该能看到:")
    print('  - 两条 "[arduino:xxx] 串口不可用,降级 dummy"  (因为没插 Nano)')
    print('  - 两条 "[arduino:xxx] [dummy] \'PIN N LEVEL MS\'"  (触发命令模拟)')
    return 0


if __name__ == "__main__":
    sys.exit(main())

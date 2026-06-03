# 外设接入说明 (DEVICES.md)

业务层(三个检测 app)和硬件层(GPIO/继电器/舵机...)之间通过事件总线解耦。
**app 只发事件**, **dispatcher 负责路由**, **device 负责执行**。

```
检测 app                    dispatcher                     设备
─────────                  ───────────                  ─────────
self.emit_action(            event queue                  GPIO 17 高电平 0.3s
    "shock", duration=...)   ↓ worker 线程                舵机转 90°
        │                    ↓ 找路由 + 冷却              蜂鸣器响 2 声
        └─→ 立即返回         ↓ 调 device.trigger()        ...
            (不阻塞)                                      (在 worker 线程
                                                          阻塞,不影响摄像头)
```

---

## 目录结构

```
OpenCV/
├── devices/                          # ← 新增,外设接入层
│   ├── __init__.py
│   ├── base.py                       # Device 抽象类
│   ├── dummy.py                      # 占位设备(Windows/未配置时兜底)
│   ├── dispatcher.py                 # 事件路由 + 异步执行 + 冷却
│   ├── factory.py                    # 从 YAML 创建 dispatcher
│   ├── relay.py                      # 继电器骨架 (电击/电磁锁)
│   ├── buzzer.py                     # 蜂鸣器骨架
│   └── servo_lock.py                 # 舵机锁骨架
├── config/
│   └── devices.yaml                  # ← 路由表(哪个动作触发哪个设备)
├── common/base_app.py                # 加了 emit_action() + dispatcher 注入
└── (三个 app 各加了 1~2 行 emit_action 调用)
```

---

## 树莓派环境准备

### 1. 系统层依赖

```bash
sudo apt update
sudo apt install -y libgl1 libglib2.0-0 libatlas-base-dev \
                    libcamera-tools rpicam-apps v4l-utils
```

### 2. Python 环境 (用 miniforge3 装 Python 3.11)

```bash
# 装 miniforge3 (ARM64 conda)
cd ~/Downloads
wget https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh
bash Miniforge3-Linux-aarch64.sh -b -p ~/miniforge3
~/miniforge3/bin/conda init bash
source ~/.bashrc

# 创建项目环境
conda create -n worldcup python=3.11 -y
conda activate worldcup
```

### 3. Python 依赖

```bash
cd ~/Zihan_ws/World_Cup
pip install --upgrade pip
pip install -r requirements.txt
```

requirements.txt 里已经声明:

| 包 | 用途 | 平台限制 |
|---|---|---|
| `flask` | Web 服务 | 全平台 |
| `opencv-python` | 摄像头 + 图像处理 | 全平台 |
| `mediapipe` | 检测模型(只到 Python 3.12) | 全平台,但 Python 必须 ≤ 3.12 |
| `numpy` | 基础 | 全平台 |
| `pyyaml` | 读 devices.yaml | 全平台 |
| **`gpiozero`** | GPIO 高层封装 | **仅 Linux** |
| **`lgpio`** | gpiozero 在 Pi 5 上的底层后端 | **仅 Linux** |

> `gpiozero` 和 `lgpio` 在 Windows 上 pip 安装会失败,这是预期行为 ——
> `requirements.txt` 里用 `; platform_system == "Linux"` 标记了平台限制,
> Windows 上 pip 会自动跳过这两个包。Windows 测试时所有设备都自动是 dummy 模式。

### 4. 验证

```bash
python -c "import cv2, mediapipe, flask, yaml; print('OK')"
python -c "from gpiozero import LED; print('gpiozero OK')"   # 只在树莓派上
```

### 5. 启动

```bash
# 树莓派 CSI 摄像头需要 libcamerify 包装
libcamerify python web_server.py --host 0.0.0.0 --port 8000 --source 0

# IP 摄像头无需 libcamerify
python web_server.py --host 0.0.0.0 --port 8000 \
    --source rtsp://user:pass@192.168.1.110/stream1
```

---

## 配置外设 (config/devices.yaml)

格式:

```yaml
<模式名>:                # closed_eye / bite_finger / pushup
  <动作名>:              # shock / alert / unlock,业务代码里 emit_action(...) 用
    device: <类型>       # dummy / relay / buzzer / servo_lock
    name: <实例名>       # 可选,日志可读
    pin: <BCM 编号>      # GPIO 引脚(BCM 编号,不是物理引脚号)
    cooldown: <秒>       # 两次触发的最小间隔
    ...                  # 其它参数按设备类型
```

**默认值**: 三条路由的 `device` 都是 `dummy`,只打日志不动 GPIO。
等硬件接好,把 `device: dummy` 改成 `relay` / `buzzer` / `servo_lock` 即可。

### 示例: 闭眼时按一下电击模块按钮

```yaml
closed_eye:
  shock:
    device: relay
    pin: 17                # GPIO17 (物理引脚 11)
    pulse: 0.3             # 高电平 300ms ≈ 按一下按钮
    active_high: true      # 注意看你继电器模块的电平方向
    cooldown: 5.0          # 5 秒内只能电一次
```

### 示例: 俯卧撑达标后舵机推开门栓

```yaml
pushup:
  unlock:
    device: servo_lock
    pin: 12                # GPIO12 (硬件 PWM,舵机更稳)
    close_angle: 0
    open_angle: 90
    hold: 5                # 开锁后保持 5 秒再关
    cooldown: 10
```

### 示例: 咬手指时响蜂鸣器

```yaml
bite_finger:
  alert:
    device: buzzer
    pin: 18
    beeps: 2
    on_ms: 120
    off_ms: 80
    cooldown: 3.0
```

---

## 接线说明 (BCM 引脚)

| 设备 | 推荐 BCM 引脚 | 物理引脚 | 备注 |
|---|---|---|---|
| 电击继电器 | GPIO17 | 11 | 普通数字输出 |
| 蜂鸣器 | GPIO18 | 12 | 普通数字输出 |
| 舵机 | GPIO12 / 13 / 18 / 19 | 32/33/12/35 | 硬件 PWM,抖动更小 |
| GND (共地) | — | 6/9/14/20/25/30/34/39 | 任意 GND 都行 |
| 5V (继电器/舵机 VCC) | — | 2/4 | 注意舵机大电流别直接拉 |

⚠️ **舵机和继电器最好独立供电**(5V/2A 以上电源),共地即可。直接从树莓派 5V 拉容易在动作瞬间拉低电压,导致 Pi 重启或 SD 卡损坏。

---

## 工作流程示意

### 启动时

```
web_server.py 启动
   ↓
build_dispatcher_from_config("config/devices.yaml")
   ↓
读 yaml → 按 device 类型创建 RelayDevice/BuzzerDevice/ServoLockDevice
   ↓ (任一设备初始化失败 → 自动降级为 DummyDevice)
注册路由 closed_eye.shock → relay_xxx
        bite_finger.alert → buzzer_xxx
        pushup.unlock     → servo_xxx
   ↓
dispatcher worker 线程启动,等事件
```

### 检测触发时

```
ClosedEyeApp.process_frame()
   检测到闭眼 3 秒
   ↓
self.emit_action("shock", duration=3.2)
   ↓
dispatcher.dispatch("closed_eye", "shock", duration=3.2)
   ↓
查 cooldown,合并 payload,入队 (非阻塞,立即返回)
   ↓
摄像头线程继续下一帧 ✅

worker 线程:
   从队列拿 event
   ↓
   relay_xxx.trigger({pin: 17, pulse: 0.3, duration: 3.2, ...})
   ↓
   GPIO17 拉高 → sleep(0.3) → 拉低
```

---

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动日志看到 `gpiozero 不可用,降级为 dummy` | Windows 上 gpiozero 没装 | 预期行为,树莓派上才需要 |
| 启动看到 `pyyaml 未安装` | 没装 pyyaml | `pip install pyyaml` |
| 触发时 `无路由` 日志 | `mode_key` 或 action 名拼写不对 | 检查 yaml 顶层 key 和 emit_action 的字符串 |
| 触发了但 GPIO 没动 | yaml 里还是 `device: dummy` | 改成 `relay`/`buzzer`/`servo_lock` |
| 舵机抖、嗡嗡响、不准 | PWM 抖动 | 优先用硬件 PWM 引脚 12/13/18/19,或加 PCA9685 |
| 触发瞬间树莓派重启 | 外设瞬时电流把 5V 拉低 | 给舵机/继电器单独供电,共地即可 |
| Pi 5 上 `RPi.GPIO` 报错 | Pi 5 不支持 RPi.GPIO | 用 gpiozero (本项目已采用) |

---

## 添加新设备类型

假设要加 HTTP 远程触发(比如调用米家网关):

1. 新建 `devices/http_device.py`,继承 `Device`,实现 `trigger()`
2. 在 `devices/factory.py:_registry()` 加 `"http": HttpDevice`
3. 在 `config/devices.yaml` 用 `device: http`,加 URL/headers 等字段

**不需要改任何业务代码** —— 这就是分层解耦的意义。

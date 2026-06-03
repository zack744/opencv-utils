# 外设接入说明 (DEVICES.md)

业务层（三个检测 app）和硬件层（Arduino Nano → 继电器 → 外设）之间通过事件总线解耦。
**app 只发事件**，**dispatcher 负责路由**，**device 负责执行**。

```
检测 app                    dispatcher                     Arduino Nano
─────────                  ───────────                  ─────────
self.emit_action(            event queue                  收到 "PIN 3 LOW 8000"
    "shock", duration=...)   ↓ worker 线程                 ↓
        │                    ↓ 找路由 + 冷却                D3 拉低 8s
        └─→ 立即返回         ↓ 调 device.trigger()         ↓
            (不阻塞)            ↓                          继电器 A 吸合
                              ArduinoDevice._send()          ↓
                              发 "PIN N LEVEL MS\n"        按摩仪通电
                                  (在 worker 线程
                                   阻塞 ≈ pulse 时长,
                                   不影响摄像头)
```

---

## 硬件架构 (当前方案)

```
  Pi  USB  ──→  Arduino Nano  ──→  5V 继电器模块  ──→  外设
  (发串口指令)   (D2-D12 拉脚)     (IN1/IN2)            (按摩仪 / 电磁锁)
                    │
                    └─ USB 自带 5V/500mA 供电
```

- **Pi**: 跑 Python（OpenCV + MediaPipe + Flask），通过 USB 串口发文本协议
- **Nano**: 烧 `firmware/nano_relay.ino`，收指令后拉对应脚 N 毫秒
- **继电器模块**: IN 脚接 Nano 数字脚，VCC 接独立 5V，GND 共地
- **负载电源**: 5V 给按摩仪，12V/2A 给电磁锁，**所有 GND 必须共地**

详细接线见方案九 / 接线表。

---

## 目录结构

```
OpenCV/
├── devices/                          # 外设接入层
│   ├── __init__.py
│   ├── base.py                       # Device 抽象类
│   ├── dummy.py                      # 占位设备(没接硬件时兜底)
│   ├── dispatcher.py                 # 事件路由 + 异步执行 + 冷却
│   ├── factory.py                    # 从 YAML 创建 dispatcher
│   ├── arduino.py                    # ← 当前主用: Arduino Nano 串口设备
│   ├── relay.py                      # 继电器骨架 (Pi GPIO 直驱, 备选)
│   ├── buzzer.py                     # 蜂鸣器骨架 (Pi GPIO, 备选)
│   └── servo_lock.py                 # 舵机锁骨架 (Pi GPIO, 备选)
├── firmware/
│   └── nano_relay.ino                # ← 烧到 Nano 里的固件
├── config/
│   └── devices.yaml                  # 路由表(哪个动作触发 Nano 哪个脚)
├── common/base_app.py                # emit_action() + dispatcher 注入
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
cd ~/Downloads
wget https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh
bash Miniforge3-Linux-aarch64.sh -b -p ~/miniforge3
~/miniforge3/bin/conda init bash
source ~/.bashrc

conda create -n worldcup python=3.11 -y
conda activate worldcup
```

### 3. Python 依赖

```bash
cd ~/OpenCV
pip install --upgrade pip
pip install -r requirements.txt
```

requirements.txt 当前声明:

| 包 | 用途 | 平台限制 |
|---|---|---|
| `flask` | Web 服务 | 全平台 |
| `opencv-python` | 摄像头 + 图像处理 | 全平台 |
| `mediapipe` | 检测模型(只到 Python 3.12) | 全平台, Python ≤ 3.12 |
| `numpy` | 基础 | 全平台 |
| `pyyaml` | 读 devices.yaml | 全平台 |
| **`pyserial`** | **Pi USB → Nano 串口** | **全平台**（没插 Nano 自动降级 dummy） |
| `gpiozero` | 旧 GPIO 方案, 备选 | 仅 Linux |
| `lgpio` | gpiozero 在 Pi 5 上的后端 | 仅 Linux |

### 4. 串口权限

```bash
sudo usermod -aG dialout $USER
# 重新登录生效
```

### 5. 验证

```bash
python -c "import cv2, mediapipe, flask, yaml, serial; print('OK')"
ls -l /dev/ttyUSB* /dev/ttyACM*   # 看 Nano 在哪个串口
```

### 6. 启动

```bash
libcamerify python web_server.py --host 0.0.0.0 --port 8000 --source 0
# 或 IP 摄像头
python web_server.py --host 0.0.0.0 --port 8000 \
    --source rtsp://user:pass@192.168.1.110/stream1
```

---

## 配置外设 (config/devices.yaml)

格式:

```yaml
<模式名>:                # closed_eye / bite_finger / pushup
  <动作名>:              # shock / alert / unlock, 业务代码里 emit_action(...) 用
    device: arduino      # 当前主用 arduino; 也可临时切回 dummy / relay / buzzer
    name: <实例名>       # 可选, 日志可读
    port: /dev/ttyUSB0   # 串口路径 (Windows: COM3)
    baud: 9600           # 波特率
    pin: 3               # Nano 数字脚号 (D2-D12, PWM 用 D3/D5/D6/D9/D10/D11)
    pulse: 2.0           # 高电平时长, 秒
    active_high: false   # 多数继电器模块 active_low, 看模块丝印
    cooldown: 30.0       # 两次触发的最小间隔
```

**默认值**: 所有路由 `device: arduino`, 启动时尝试打开串口, 失败自动降级 dummy。

### 示例: 闭眼时按摩仪通电 8 秒

```yaml
closed_eye:
  shock:
    device: arduino
    name: massager_power
    port: /dev/ttyUSB0
    baud: 9600
    pin: 3                 # Nano D3 → 继电器A IN → 按摩仪 5V 供电
    pulse: 8.0
    active_high: false
    cooldown: 60.0
```

### 示例: 俯卧撑达标后电磁锁开 2 秒

```yaml
pushup:
  unlock:
    device: arduino
    name: door_lock
    port: /dev/ttyUSB0
    baud: 9600
    pin: 4                 # Nano D4 → 继电器B IN → 电磁锁 12V 回路
    pulse: 2.0
    active_high: false
    cooldown: 30.0
```

### 不想接硬件时

把 `device: arduino` 改成 `device: dummy`, 只打日志不发串口。

---

## 接线表 (当前方案)

| Nano 脚 | 继电器 | 负载 | 电源 |
|---|---|---|---|
| D3 | CH1 / IN1 | 按摩仪 | 5V/2A 独立电源, COM 接 V+, NO 接按摩仪 V+ |
| D4 | CH2 / IN2 | 电磁锁 | 12V/2A 独立电源, COM 接 V+, NO 接电磁锁 V+ |
| GND | GND | — | 三处共地 (Nano / 继电器 / 负载电源 GND 必须相连) |
| 5V | VCC | — | Nano USB 自带, 接继电器模块逻辑端 (VCC), 不带负载 |

⚠️ **继电器 VCC 和负载电源必须分开**, 共地即可。Nano 用 USB 自供电就够, 不要外接 5V, 否则 USB 5V 和外接 5V 打架。

---

## 串口协议

Pi 与 Nano 之间走一行 ASCII:

```
Pi → Nano:   "PIN <脚号> <HIGH|LOW> <持续毫秒>\n"
Nano → Pi:   "OK\n"  /  "ERR <原因>\n"
```

例: `PIN 3 LOW 8000\n` → D3 拉低 8 秒（active_low 继电器吸合 8 秒）→ 回 `OK\n`

完整固件见 `firmware/nano_relay.ino`。当前固件用 `delay()` 阻塞, 同一时刻只能跑一个动作 —— 你的"一次只接一个外设"策略完全够用。

---

## 工作流程示意

### 启动时

```
web_server.py 启动
   ↓
build_dispatcher_from_config("config/devices.yaml")
   ↓
读 yaml → 按 device 类型创建 ArduinoDevice (两条路由)
   ↓ (任一 ArduinoDevice 串口打开失败 → 自动降级为 DummyDevice)
注册路由 closed_eye.shock → arduino:massager_power
        pushup.unlock     → arduino:door_lock
   ↓
dispatcher worker 线程启动, 等事件
```

### 检测触发时

```
ClosedEyeApp.process_frame()
   检测到闭眼 3 秒
   ↓
self.emit_action("shock")
   ↓
dispatcher.dispatch("closed_eye", "shock")
   ↓
查 cooldown, 合并 payload, 入队 (非阻塞, 立即返回)
   ↓
摄像头线程继续下一帧 ✅

worker 线程:
   从队列拿 event
   ↓
   arduino_device.trigger({pin: 3, pulse: 8.0, ...})
   ↓
   _send("PIN 3 LOW 8000\n")
   ↓
   ser.write(...) → ser.readline()  ← 阻塞 ≈ 8s, 等 Nano 回 OK
   ↓
   Nano D3 拉低 8s → 继电器 A 吸合 → 按摩仪通电 → 回 OK
```

---

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动日志 `串口不可用,降级 dummy` | /dev/ttyUSB0 不存在 / 权限不足 | 见下方串口问题排查 |
| `Permission denied: /dev/ttyUSB0` | 用户不在 dialout 组 | `sudo usermod -aG dialout $USER` + 重新登录 |
| Nano 找不到串口 (插上 dmesg 没反应) | 国产 Nano 用 CH340, Pi OS 默认带驱动 | 实在不行换数据线 (有些只能充电) |
| 启动后 Nano 灯闪一下 (复位) | pyserial 默认拉 DTR 触发 Nano 复位 | `serial.Serial(..., dtr=False)` 或 Nano RST↔5V 串 10μF 电容 |
| 触发时继电器不响应 | active_high/low 反了 | 改 yaml 的 `active_high` 字段, 固件不用动 |
| 触发瞬间 Pi 重启 | 5V 电源带不动继电器 | 继电器 VCC 接独立 5V, 共地 |
| 串口回执丢失 / 超时 | Nano 上一条命令还在 `delay()` 中 | 当前固件是阻塞的, 等完再发下一条 |
| 启动看到 `pyyaml 未安装` | 没装 pyyaml | `pip install pyyaml` |
| 启动看到 `pyserial 未安装` | 没装 pyserial | `pip install pyserial` |
| 触发时 `无路由` 日志 | mode_key 或 action 名拼写不对 | 检查 yaml 顶层 key 和 emit_action 的字符串 |

---

## 添加新设备类型

假设要加 HTTP 远程触发（比如调用米家网关）:

1. 新建 `devices/http_device.py`, 继承 `Device`, 实现 `trigger()`
2. 在 `devices/factory.py:_registry()` 加 `"http": HttpDevice`
3. 在 `config/devices.yaml` 用 `device: http`, 加 URL/headers 等字段

**不需要改任何业务代码** —— 这就是分层解耦的意义。

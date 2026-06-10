# opencv-utils

> 基于 OpenCV + MediaPipe 的实时视觉检测 / 树莓派外设联动项目

三个独立的检测 app（闭眼监控 / 咬手指识别 / 俯卧撑门禁）共用一套摄像头 → 检测 → 事件总线 → 外设触发的架构,既能跑在树莓派上接继电器 / 蜂鸣器 / 舵机,也能在 Windows 上以 dummy 模式纯做视觉算法验证。

---

## ✨ 功能

| App | 模式 | 触发 | 默认外设 |
|---|---|---|---|
| `closed_eye_app.py` | `closed_eye` | 闭眼超过阈值时长 | 继电器(电击 / 提醒) |
| `bite_finger_app.py` | `bite_finger` | 手指入口腔区域 | 蜂鸣器 |
| `pushup_app.py` | `pushup` | 俯卧撑达到目标次数 + 动作标准 | 舵机锁(开门栓) |

所有外设的 `device` 字段默认都是 `dummy`,**只在日志里打事件**,不会动 GPIO。硬件接好后改 `config/devices.yaml` 即可,业务代码一行不用动。

---

## 📁 项目结构

```
opencv-utils/
├── web_server.py              # Flask + MJPEG Web UI 入口
├── web/                       # 静态资源 (HTML / CSS / JS)
├── closed_eye/                # 闭眼监控 app + face_landmarker.task
├── bite_finger/               # 咬手指 app + hand_landmarker.task
├── pushup_gate/               # 俯卧撑门禁 app + pose_landmarker.task
├── devices/                   # 外设抽象层 (base / dummy / relay / buzzer / servo_lock / dispatcher / factory)
├── config/
│   └── devices.yaml           # ← 外设路由表(动作 → 设备)
├── common/                    # base_app 等共享代码
├── firmware/
│   └── nano_relay.ino         # Arduino Nano 固件(USB 串口控制继电器)
├── DEVICES.md                 # 外设 / GPIO / 接线详细说明
├── RASPBERRY_PI_WEB.md        # 树莓派部署 + Web UI 详细说明
└── requirements.txt
```

---

## 🚀 快速开始

### Windows (开发 / 算法验证,所有外设自动降级 dummy)

```powershell
git clone https://github.com/zack744/opencv-utils.git
cd opencv-utils
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python web_server.py --host 0.0.0.0 --port 8000 --source 0
```

浏览器打开 `http://localhost:8000`,可在页面里切换模式 / 改摄像头源 / 开关识别 / 开关录制。
未传 `--source` 时服务不会自动查找或打开摄像头,需要在页面里填写摄像头源并点击"应用"。

### 树莓派 (Linux,启用 GPIO 外设)

```bash
sudo apt update
sudo apt install -y libgl1 libglib2.0-0 libatlas-base-dev \
                    libcamera-tools rpicam-apps v4l-utils
# 推荐 miniforge3 + Python 3.11(mediapipe 只支持到 3.12)
conda create -n opencv-utils python=3.11 -y
conda activate opencv-utils

pip install -r requirements.txt   # gpiozero / lgpio 平台标记只 Linux 安装

# CSI 摄像头
libcamerify python web_server.py --host 0.0.0.0 --port 8000 --source 0
# USB / IP 摄像头
python web_server.py --host 0.0.0.0 --port 8000 --source rtsp://user:pass@192.168.1.110/stream1
```

> 完整部署细节见 [RASPBERRY_PI_WEB.md](./RASPBERRY_PI_WEB.md),外设 / 接线 / 故障排查见 [DEVICES.md](./DEVICES.md)。

---

## ⚙️ 配置外设 (config/devices.yaml)

```yaml
closed_eye:
  shock:
    device: relay           # dummy / relay / buzzer / servo_lock / arduino
    pin: 17                 # BCM 编号
    pulse: 0.3              # 高电平 300ms
    active_high: true
    cooldown: 5.0           # 两次触发的最小间隔(秒)
```

业务侧一行调用就能触发:

```python
self.emit_action("shock", duration=3.2)
#   ↓ dispatcher 自动查路由 → 入队 → worker 线程执行 → 不阻塞摄像头帧
```

加新设备类型 = 新建 `devices/<name>.py` + 在 `devices/factory.py` 注册,**业务代码 0 改动**。

---

## 🧠 架构一句话

```
摄像头帧 → Detection App(发事件) → Dispatcher(异步 + 冷却 + 路由)
                                          ↓
                                 Device.trigger()(在 worker 线程,阻塞)
                                          ↓
                                  GPIO / 串口 / HTTP ...
```

摄像头线程永远不会被外设动作阻塞,外设动作的延迟 / 抖动也不会拖慢检测帧率。

---

## 🧰 依赖

| 包 | 用途 | 平台 |
|---|---|---|
| `flask` | Web 服务 | 全部 |
| `opencv-python` | 摄像头 + 图像处理 | 全部 |
| `mediapipe` | 检测模型 | Python ≤ 3.12 |
| `numpy` | 基础 | 全部 |
| `pyyaml` | 读 `devices.yaml` | 全部 |
| `pyserial` | Arduino Nano 串口 | 全部 |
| `gpiozero` | GPIO 高层封装 | **仅 Linux** |
| `lgpio` | gpiozero 在 Pi 5 的后端 | **仅 Linux** |

---

## 🛠️ 故障排查速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动日志 `gpiozero 不可用,降级为 dummy` | Windows / Mac | 预期行为,树莓派上才需要 |
| 触发时 `无路由` | yaml key / action 名拼写错误 | 检查 `config/devices.yaml` 顶层 key 与 `emit_action` 字符串 |
| 触发了但 GPIO 没动 | yaml 里还是 `device: dummy` | 改成 `relay` / `buzzer` / `servo_lock` |
| 舵机抖 / 嗡嗡响 | PWM 软件抖动 | 用硬件 PWM 引脚 12/13/18/19,或加 PCA9685 |
| Pi 5 上 `RPi.GPIO` 报错 | Pi 5 不再支持 | 本项目已用 `gpiozero` |

更多见 [DEVICES.md](./DEVICES.md)。

---

## 📜 License

MIT — see [LICENSE](./LICENSE).

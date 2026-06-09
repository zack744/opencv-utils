# 树莓派 Web UI 部署说明

这个版本使用 Flask + 原生 HTML/CSS/JS + MJPEG 视频流，不需要桌面环境，也不需要 Node。

## 目录

```text
OpenCV/
├── web_server.py
├── web/static/
│   ├── index.html
│   ├── app.js
│   └── style.css
├── requirements.txt
├── config/devices.yaml
├── firmware/nano_relay.ino       ← 烧到 Nano 里
├── closed_eye/
├── bite_finger/
└── pushup_gate/
```

## 安装依赖

推荐先安装系统层 OpenCV 依赖，树莓派上比直接编译更稳。

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip python3-opencv libgl1 libglib2.0-0
```

然后在项目目录创建虚拟环境。

```bash
cd ~/OpenCV
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install -r requirements.txt
```

如果 `pip install opencv-python` 在树莓派上很慢或失败，可以从 `requirements.txt` 删除 `opencv-python`，继续使用系统安装的 `python3-opencv`。

> 当前架构是 Pi USB → Arduino Nano → 继电器，所以不需要装 `gpiozero`。`requirements.txt` 里那两个 `; platform_system == "Linux"` 标记是旧 GPIO 方案的，保留无害。

## 串口权限（必须）

```bash
sudo usermod -aG dialout $USER
# 重新登录生效
groups   # 应该能看到 dialout
```

## 启动

```bash
source .venv/bin/activate
python web_server.py --host 0.0.0.0 --port 8000 --mode closed_eye --source 0
```

可选模式：

| 模式 | 参数 |
|---|---|
| 闭眼监控 | `closed_eye` |
| 咬手指识别 | `bite_finger` |
| 俯卧撑门禁 | `pushup` |

浏览器访问：

```text
http://树莓派IP:8000
```

启动日志里应该看到：

```text
[arduino:massager_power] /dev/ttyUSB0@9600 ready, pin=3
[arduino:door_lock] /dev/ttyUSB0@9600 ready, pin=4
路由注册: closed_eye.shock → <ArduinoDevice massager_power> (cooldown=60.0s)
路由注册: pushup.unlock → <ArduinoDevice door_lock> (cooldown=30.0s)
```

如果只看到 `[arduino:xxx] 串口不可用,降级 dummy: ...` —— 说明 Nano 没接好或串口权限问题，不影响服务运行，触发时只打日志。

## 摄像头源

| 场景 | 示例 |
|---|---|
| USB / V4L2 本地摄像头 | `--source 0` |
| 树莓派排线 CSI 摄像头兜底方案 | `--source rpicam` |
| 第二个树莓派排线 CSI 摄像头 | `--source rpicam:1` |
| 第二个本地摄像头 | `--source 1` |
| 明确指定 V4L2 设备 | `--source /dev/video0` |
| 手机 IP Webcam / HTTP MJPEG | `--source http://192.168.1.105:8080/video` |
| RTSP 摄像头 | `--source rtsp://user:pass@ip/stream1` |
| 网页端摄像头 | 页面点击"使用网页摄像头"，或填入 `browser` |

页面里也可以直接修改摄像头源并点击"应用"。
HTTP 摄像头源如果漏写协议头,例如 `192.168.1.105:8080/video`,服务会自动按 `http://192.168.1.105:8080/video` 处理。
如果填的是 IP Webcam 根地址,例如 `http://192.168.1.105:8080`,服务会先检查根地址,发现它是 HTML 控制台后自动尝试 `/video` 等常见 MJPEG 路径。
网页端摄像头使用浏览器 `getUserMedia`,跨设备访问树莓派时需要 HTTPS;如果用 `http://树莓派IP:8000` 打开,大多数浏览器会拒绝摄像头权限。

HTTPS 启动示例:

```bash
python web_server.py --host 0.0.0.0 --port 8000 \
  --source browser \
  --certfile /path/to/cert.pem \
  --keyfile /path/to/key.pem
```

树莓派 Camera Module 这类排线摄像头建议按顺序排查:

```bash
# 1) 先确认系统相机栈正常
rpicam-hello --list-cameras

# 2) 先试 OpenCV V4L2
python web_server.py --host 0.0.0.0 --port 8000 --mode closed_eye --source 0

# 3) 如果 V4L2 拉不起帧,改用 rpicam 后端
python web_server.py --host 0.0.0.0 --port 8000 --mode closed_eye --source rpicam
```

`rpicam` 后端通过 `rpicam-vid/libcamera-vid` 输出 MJPEG,再由 OpenCV 解码,不依赖 Picamera2 Python 绑定,也不依赖 OpenCV 的 GStreamer 支持。可选环境变量:

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `CAM_WIDTH` | `640` | 摄像头宽度 |
| `CAM_HEIGHT` | `480` | 摄像头高度 |
| `CAM_FPS` | `15` | 摄像头帧率 |
| `RPICAM_CAMERA` | `0` | 排线摄像头编号 |
| `RPICAM_BIN` | 自动查找 | 手动指定 `rpicam-vid` 或 `libcamera-vid` 路径 |

## API

| 地址 | 方法 | 作用 |
|---|---|---|
| `/` | GET | Web UI |
| `/stream` | GET | MJPEG 视频流 |
| `/api/status` | GET | 当前状态 |
| `/api/modes` | GET | 可用模式 |
| `/api/mode` | POST | 切换检测模式 |
| `/api/recognition` | POST | 开关识别 |
| `/api/recording` | POST | 开关录制 |
| `/api/camera` | POST | 设置摄像头源 |
| `/api/camera/switch` | POST | 切换本地 0/1 摄像头 |
| `/api/pushup/target` | POST | 调整俯卧撑目标次数 |

## 自启动可选

后续要做成开机自启，可以新增 systemd 服务。先确保手动启动稳定，再做自启动更稳。

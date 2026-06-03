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
cd ~/Zihan_ws/World_Cup
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install -r requirements.txt
```

如果 `pip install opencv-python` 在树莓派上很慢或失败，可以从 `requirements.txt` 删除 `opencv-python`，继续使用系统安装的 `python3-opencv`。

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

Windows 浏览器访问：

```text
http://树莓派IP:8000
```

## 摄像头源

| 场景 | 示例 |
|---|---|
| USB / CSI 本地摄像头 | `--source 0` |
| 第二个本地摄像头 | `--source 1` |
| 手机 IP Webcam | `--source http://192.168.1.105:8080/video` |
| RTSP 摄像头 | `--source rtsp://user:pass@ip/stream1` |

页面里也可以直接修改摄像头源并点击“应用”。

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

## 自启动可选

后续要做成开机自启，可以新增 systemd 服务。先确保手动启动稳定，再做自启动更稳。

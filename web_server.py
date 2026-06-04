import argparse
import atexit
import importlib.util
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np
from flask import Flask, abort, jsonify, request, send_from_directory, Response

from devices import build_dispatcher_from_config


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "web" / "static"
DEVICES_CONFIG = ROOT / "config" / "devices.yaml"
RECORDINGS_DIR = ROOT / "recordings"
RECORDING_EXTS = {".mp4", ".webm", ".mkv", ".avi", ".mov"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

MODES = {
    "closed_eye": {
        "label": "闭眼监控",
        "path": ROOT / "closed_eye" / "closed_eye_app.py",
        "class": "ClosedEyeApp",
    },
    "bite_finger": {
        "label": "咬手指识别",
        "path": ROOT / "bite_finger" / "bite_finger_app.py",
        "class": "BiteFingerApp",
    },
    "pushup": {
        "label": "俯卧撑门禁",
        "path": ROOT / "pushup_gate" / "pushup_app.py",
        "class": "PushupApp",
    },
}

DEFAULT_MODE = "closed_eye"


def _default_start_source():
    configured = os.environ.get("CAMERA_SOURCE") or os.environ.get("DEFAULT_CAMERA_SOURCE")
    if configured:
        return configured
    if sys.platform.startswith("linux") and (
        shutil.which("rpicam-vid") or shutil.which("libcamera-vid")
    ):
        return "rpicam"
    return None


DEFAULT_SOURCE = _default_start_source()


def _parse_source(source):
    if source is None:
        return None
    if isinstance(source, int):
        return source
    source = str(source).strip()
    if not source:
        return None
    try:
        return int(source)
    except ValueError:
        return source


def _load_app_class(mode):
    info = MODES[mode]
    module_name = f"opencv_web_{mode}"
    if module_name in sys.modules:
        module = sys.modules[module_name]
    else:
        spec = importlib.util.spec_from_file_location(module_name, info["path"])
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载模块: {info['path']}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return getattr(module, info["class"])


class RuntimeManager:
    def __init__(self, dispatcher=None):
        self._lock = threading.RLock()
        self.runtime = None
        self.mode = None
        self.dispatcher = dispatcher  # 外设事件分发器,所有 app 共享一份

    def ensure_started(self, mode=DEFAULT_MODE, source=None):
        with self._lock:
            if self.runtime is None:
                self._start_locked(mode, source)

    def switch_mode(self, mode, source=None):
        if mode not in MODES:
            raise ValueError(f"未知模式: {mode}")
        with self._lock:
            self._stop_locked()
            self._start_locked(mode, source)
            return self.status()

    def _start_locked(self, mode, source=None):
        app_cls = _load_app_class(mode)
        self.runtime = app_cls(source=_parse_source(source), dispatcher=self.dispatcher)
        self.mode = mode

    def _stop_locked(self):
        if self.runtime is not None:
            self.runtime.close()
        self.runtime = None
        self.mode = None

    def close(self):
        with self._lock:
            self._stop_locked()
        if self.dispatcher is not None:
            try:
                self.dispatcher.close()
            except Exception:
                pass

    def status(self):
        with self._lock:
            if self.runtime is None:
                return {
                    "mode": None,
                    "state": "未启动",
                    "state_color": "gray",
                    "main_text": "服务尚未启动检测运行时",
                    "sub_text": "",
                    "stats": [],
                    "progress": 0,
                    "alert": False,
                }
            status = self.runtime.get_status_snapshot()
            status["mode"] = self.mode
            status["mode_label"] = MODES[self.mode]["label"]
            return status

    def jpeg(self):
        with self._lock:
            runtime = self.runtime
        if runtime is None:
            return None
        return runtime.get_latest_jpeg()

    def set_recognition(self, enabled):
        with self._lock:
            self._require_runtime().set_recognition(enabled)
            return self.status()

    def set_recording(self, enabled):
        with self._lock:
            runtime = self._require_runtime()
            if enabled:
                runtime.start_recording()
            else:
                runtime.stop_recording()
            return self.status()

    def set_camera(self, source):
        with self._lock:
            ok = self._require_runtime().apply_camera_source_value(source)
            if not ok:
                raise RuntimeError(f"无法连接摄像头源: {source}")
            return self.status()

    def switch_camera(self):
        with self._lock:
            ok = self._require_runtime().switch_camera()
            if not ok:
                raise RuntimeError("当前摄像头源不支持 0/1 切换，或目标摄像头不可用")
            return self.status()

    def set_pushup_target(self, reps):
        """仅 pushup 模式生效：调整触发解锁所需的俯卧撑次数。"""
        with self._lock:
            if self.mode != "pushup":
                raise RuntimeError("当前不是俯卧撑模式，无法调整次数")
            runtime = self._require_runtime()
            if not hasattr(runtime, "set_target_reps"):
                raise RuntimeError("当前 runtime 不支持调整目标次数")
            runtime.set_target_reps(reps)
            return self.status()

    def _require_runtime(self):
        if self.runtime is None:
            raise RuntimeError("检测运行时尚未启动")
        return self.runtime


manager = RuntimeManager(dispatcher=build_dispatcher_from_config(str(DEVICES_CONFIG)))
app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")


@app.before_request
def ensure_runtime():
    if request.endpoint != "static":
        manager.ensure_started(DEFAULT_MODE, DEFAULT_SOURCE)


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/api/modes")
def get_modes():
    return jsonify({
        "current": manager.mode,
        "modes": [
            {"id": mode, "label": info["label"]}
            for mode, info in MODES.items()
        ],
    })


@app.get("/api/status")
def get_status():
    return jsonify(manager.status())


@app.post("/api/mode")
def set_mode():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(manager.switch_mode(data.get("mode"), data.get("source")))
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 400


@app.post("/api/recognition")
def set_recognition():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(manager.set_recognition(bool(data.get("enabled"))))
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 400


@app.post("/api/recording")
def set_recording():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(manager.set_recording(bool(data.get("enabled"))))
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 400


@app.post("/api/camera")
def set_camera():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(manager.set_camera(data.get("source")))
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 400


@app.post("/api/camera/switch")
def switch_camera():
    try:
        return jsonify(manager.switch_camera())
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 400


@app.post("/api/pushup/target")
def set_pushup_target():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(manager.set_pushup_target(data.get("reps")))
    except Exception as exc:
        return jsonify({"detail": str(exc)}), 400


@app.get("/api/recordings")
def list_recordings():
    """返回 recordings/ 目录下的视频文件列表(按修改时间倒序)。"""
    items = []
    if RECORDINGS_DIR.exists() and RECORDINGS_DIR.is_dir():
        candidates = [
            p for p in RECORDINGS_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in RECORDING_EXTS
        ]
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in candidates:
            stat = p.stat()
            encoded = quote(p.name, safe="")
            items.append({
                "name": p.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "url": f"/api/recordings/{encoded}",
                "download_url": f"/api/recordings/{encoded}?download=1",
            })
    return jsonify({"items": items, "output_dir": str(RECORDINGS_DIR)})


@app.get("/api/recordings/<path:filename>")
def get_recording(filename):
    """提供录像文件的内联播放与下载。

    - 默认: `Content-Disposition: inline`,前端 <video> 拉流播放;
            `conditional=True` 自动响应 Range,允许拖进度条。
    - `?download=1`: `Content-Disposition: attachment`,浏览器保存到本地。
    """
    if not filename or filename.startswith(".") or "/" in filename or "\\" in filename:
        abort(400)
    target = RECORDINGS_DIR / filename
    if not target.is_file() or target.suffix.lower() not in RECORDING_EXTS:
        abort(404)
    as_attachment = request.args.get("download") in {"1", "true", "yes"}
    return send_from_directory(
        str(RECORDINGS_DIR),
        filename,
        as_attachment=as_attachment,
        conditional=True,
    )


def _placeholder_jpeg():
    img = np.full((480, 640, 3), (235, 238, 234), dtype=np.uint8)
    cv2.putText(img, "Waiting for camera", (150, 230), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (70, 82, 80), 2)
    cv2.putText(img, "OpenCV Web UI", (205, 270), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (90, 104, 99), 1)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buf.tobytes() if ok else b""


PLACEHOLDER_JPEG = _placeholder_jpeg()


def mjpeg_stream():
    while True:
        frame = manager.jpeg() or PLACEHOLDER_JPEG
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        time.sleep(0.05)


@app.get("/stream")
def stream():
    return Response(mjpeg_stream(), mimetype="multipart/x-mixed-replace; boundary=frame")


def parse_args():
    parser = argparse.ArgumentParser(description="OpenCV 三合一 Web UI")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--mode", choices=list(MODES.keys()), default=DEFAULT_MODE, help="启动模式")
    parser.add_argument("--source", default=None, help="摄像头源: 0/1 或 http/rtsp URL")
    return parser.parse_args()


atexit.register(manager.close)


if __name__ == "__main__":
    args = parse_args()
    DEFAULT_MODE = args.mode
    if args.source is not None:
        DEFAULT_SOURCE = _parse_source(args.source)
    manager.ensure_started(DEFAULT_MODE, DEFAULT_SOURCE)
    app.run(host=args.host, port=args.port, threaded=True)

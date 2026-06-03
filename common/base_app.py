"""
无桌面界面的摄像头检测运行时。

子类只需要实现:
    - setup_detector()
    - process_frame(image_bgr, frame_count) -> (annotated_bgr, status_dict)

Web 服务通过这些方法读取状态和视频帧:
    - get_latest_jpeg()
    - get_status_snapshot()
    - set_recognition(...)
    - apply_camera_source_value(...)
    - start_recording() / stop_recording()
"""

import logging
import os
import shutil
import subprocess
import sys
import time
import threading
from abc import ABC, abstractmethod
from typing import Any, Optional

import cv2
import numpy as np


logger = logging.getLogger(__name__)

DEFAULT_CAMERA_SOURCE = 0
REMOTE_CONNECT_RETRIES = 3
REMOTE_RETRY_DELAY = 2.0
REMOTE_RECONNECT_ON_LOST = True
OUTPUT_DIR = "recordings"
VIDEO_FPS = 20.0
VIDEO_SIZE = (640, 480)
VIDEO_FOURCC = "XVID"

# 采集层目标 FPS。参考 web_nano_app 项目在同一台 Pi 上用的就是 15。
# 设高了 USB camera 在 YUYV/MJPG 上会被驱动自动降帧,反而抖动。
CAMERA_CAPTURE_FPS = 15

# 是否在 Linux 上偏好 V4L2 后端(参考项目用的就是这个)。
# Pi OS Bookworm 上 apt 装的 opencv 默认会优先选 GStreamer,
# 而 GStreamer pipeline 没配好就会出现 "isOpened=True 但 read 拿不到帧" 的黑屏现象。
PREFER_V4L2_ON_LINUX = True

# 树莓派排线 CSI 摄像头兜底后端。它通过 rpicam-vid/libcamera-vid 输出 MJPEG,
# 再用 OpenCV 解码,避开 Picamera2 Python 版本绑定和 OpenCV GStreamer 编译要求。
RPICAM_SOURCE_NAMES = {"rpicam", "libcamera", "csi", "picam", "picamera"}
REMOTE_SOURCE_PREFIXES = ("http://", "https://", "rtsp://", "rtmp://", "tcp://", "udp://")


class RpicamMjpegCapture:
    """Small VideoCapture-compatible wrapper for Raspberry Pi CSI cameras."""

    def __init__(self, width=640, height=480, fps=15, camera=0, read_timeout=2.0):
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.camera = int(camera)
        self.read_timeout = float(read_timeout)
        self._proc = None
        self._thread = None
        self._lock = threading.Condition()
        self._latest_frame = None
        self._seq = 0
        self._last_read_seq = 0
        self._closed = False
        self._last_error = None
        self._start()

    @property
    def last_error(self):
        return self._last_error

    def _find_binary(self):
        configured = os.environ.get("RPICAM_BIN")
        if configured:
            return configured
        return shutil.which("rpicam-vid") or shutil.which("libcamera-vid")

    def _start(self):
        binary = self._find_binary()
        if not binary:
            self._last_error = "找不到 rpicam-vid/libcamera-vid"
            return

        cmd = [
            binary,
            "--timeout", "0",
            "--nopreview",
            "--codec", "mjpeg",
            "--width", str(self.width),
            "--height", str(self.height),
            "--framerate", str(self.fps),
            "--camera", str(self.camera),
            "-o", "-",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except Exception as exc:
            self._last_error = f"启动 rpicam 失败: {exc}"
            self._proc = None
            return

        self._thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._thread.start()

    def _reader_loop(self):
        buf = bytearray()
        try:
            while not self._closed and self._proc and self._proc.poll() is None:
                if self._proc.stdout is None:
                    break
                chunk = self._proc.stdout.read(4096)
                if not chunk:
                    time.sleep(0.01)
                    continue
                buf.extend(chunk)

                while True:
                    start = buf.find(b"\xff\xd8")
                    if start < 0:
                        if len(buf) > 1024 * 1024:
                            buf.clear()
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start > 0:
                            del buf[:start]
                        break

                    jpeg = bytes(buf[start:end + 2])
                    del buf[:end + 2]
                    arr = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if arr is None:
                        continue
                    with self._lock:
                        self._latest_frame = arr
                        self._seq += 1
                        self._lock.notify_all()
        except Exception as exc:
            self._last_error = f"读取 rpicam 输出失败: {exc}"

    def isOpened(self):
        return bool(self._proc and self._proc.poll() is None and not self._closed)

    def read(self):
        deadline = time.time() + self.read_timeout
        with self._lock:
            while self.isOpened() and self._seq == self._last_read_seq:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._lock.wait(timeout=remaining)

            if self._latest_frame is None:
                return False, None
            self._last_read_seq = self._seq
            return True, self._latest_frame.copy()

    def set(self, *_args):
        return False

    def release(self):
        self._closed = True
        proc = self._proc
        self._proc = None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


class BaseCameraApp(ABC):
    app_title = "检测系统"
    app_subtitle = ""
    app_icon = ""

    #: 业务模式标识(用于外设路由匹配)。子类可重写,默认为 "" 表示不参与外设触发。
    #: 应与 config/devices.yaml 顶层 key 一致 (如 "closed_eye" / "bite_finger" / "pushup")。
    mode_key: str = ""

    def __init__(self, work_dir=None, initial_source=None, dispatcher=None):
        if work_dir:
            os.chdir(work_dir)

        self.is_recognition_enabled = True
        self.current_camera = DEFAULT_CAMERA_SOURCE if initial_source is None else initial_source
        self.is_recording = False
        self.video_writer = None
        self.recording_filename = None
        self.recording_start_time = 0
        self.camera_status = "disconnected"
        self.current_fps = 0
        self.app_state = {}

        #: 外设事件分发器。可以是 None(不接外设)或 ActionDispatcher 实例。
        #: 由 RuntimeManager 在创建 app 时注入,实现 app/外设解耦。
        self._dispatcher = dispatcher

        self._frame_lock = threading.Lock()
        self._cap_lock = threading.Lock()
        self._latest_frame = None
        self._latest_status = self._default_status()
        self._stop_event = threading.Event()

        self.setup_detector()
        self.cap = self._connect_camera(self.current_camera)

        self._camera_thread = threading.Thread(target=self._camera_loop, daemon=True)
        self._camera_thread.start()

    def emit_action(self, action: str, /, **payload: Any) -> bool:
        """业务层触发一次外设动作。

        在采集线程里调用是安全的 —— dispatcher 是非阻塞入队,
        实际设备执行在它自己的 worker 线程。

        Args:
            action: 动作名,如 "shock" / "alert" / "unlock"
                    positional-only —— payload 里可以安全携带名为 "action"/"mode" 的键
            **payload: 运行时参数,合并进路由 default_payload 后传给设备

        Returns:
            True = 已入队;False = 无路由 / 冷却中 / 队列满 / 无 dispatcher
        """
        if self._dispatcher is None or not self.mode_key:
            return False
        return self._dispatcher.dispatch(self.mode_key, action, **payload)

    @abstractmethod
    def setup_detector(self):
        """初始化 MediaPipe detector。"""

    @abstractmethod
    def process_frame(self, image_bgr, frame_count):
        """处理单帧并返回 (annotated_bgr, status_dict)。"""

    def cleanup_detector(self):
        """子类可重写：关闭 MediaPipe detector。"""

    def _default_status(self):
        return {
            "state": "初始化",
            "state_color": "gray",
            "main_text": "正在启动...",
            "sub_text": "",
            "stats": [],
            "progress": 0.0,
            "alert": False,
        }

    def _paused_status(self):
        return {
            "state": "识别已暂停",
            "state_color": "gray",
            "main_text": "识别已暂停",
            "sub_text": "在页面中开启识别后恢复",
            "stats": self._latest_status.get("stats", []) if self._latest_status else [],
            "progress": 0.0,
            "alert": False,
        }

    def _error_status(self, msg):
        return {
            "state": "运行错误",
            "state_color": "red",
            "main_text": "检测过程出错",
            "sub_text": msg[:120],
            "stats": [],
            "progress": 0.0,
            "alert": True,
        }

    def _is_remote_source(self, source):
        return isinstance(source, str) and source.strip().lower().startswith(REMOTE_SOURCE_PREFIXES)

    def _is_rpicam_source(self, source):
        if not isinstance(source, str):
            return False
        value = source.strip().lower()
        name = value.split(":", 1)[0]
        return name in RPICAM_SOURCE_NAMES

    def _describe_source(self, source):
        if self._is_rpicam_source(source):
            return f"树莓派 CSI {source}"
        if self._is_remote_source(source):
            return f"远程 {source}"
        if isinstance(source, str) and source.startswith("/dev/"):
            return f"本地 {source}"
        return f"本地 #{source}"

    def _connect_camera(self, source):
        if self._is_rpicam_source(source):
            return self._connect_rpicam_camera(source)
        if self._is_remote_source(source):
            return self._connect_remote_camera(source)
        return self._connect_local_camera(source)

    def _connect_rpicam_camera(self, source):
        camera = os.environ.get("RPICAM_CAMERA")
        if camera is None and isinstance(source, str) and ":" in source:
            camera = source.split(":", 1)[1]
        try:
            camera_index = int(camera) if camera is not None and str(camera).strip() else 0
        except ValueError:
            camera_index = 0

        cap = RpicamMjpegCapture(
            width=int(os.environ.get("CAM_WIDTH", str(VIDEO_SIZE[0]))),
            height=int(os.environ.get("CAM_HEIGHT", str(VIDEO_SIZE[1]))),
            fps=int(os.environ.get("CAM_FPS", str(CAMERA_CAPTURE_FPS))),
            camera=camera_index,
            read_timeout=float(os.environ.get("CAM_READ_TIMEOUT", "3.0")),
        )
        if not cap.isOpened():
            logger.error("rpicam 摄像头启动失败: %s", cap.last_error)
            self.camera_status = "disconnected"
            return cap

        ok, frame = cap.read()
        if ok and frame is not None:
            logger.info("rpicam 摄像头连接成功 camera=%s size=%dx%d",
                        camera_index, frame.shape[1], frame.shape[0])
            self.camera_status = "ok"
            return cap

        logger.error("rpicam 已启动但没有读到帧: %s", cap.last_error)
        self.camera_status = "disconnected"
        return cap

    def _connect_local_camera(self, source):
        """打开本地摄像头。

        关键改动(对齐参考项目 web_nano_app):
        - 在 Linux 上显式优先用 V4L2 后端,避免 OpenCV 自动选到不工作的 GStreamer
        - 数字 source 自动尝试 `/dev/video{N}` 字符串路径(参考项目正是这么写的)
        - 设置 FOURCC=MJPG + 显式 FPS,USB camera 在高分辨率下更稳
        - 不只是 isOpened(),还要真试读一帧,读不到就 release 试下一个候选
        """
        candidates = self._build_local_candidates(source)

        last_error = None
        for dev, backend, backend_name in candidates:
            try:
                cap = cv2.VideoCapture(dev, backend) if backend is not None else cv2.VideoCapture(dev)
            except Exception as exc:
                last_error = f"VideoCapture 构造异常 dev={dev!r} backend={backend_name}: {exc}"
                logger.warning(last_error)
                continue

            if not cap.isOpened():
                last_error = f"isOpened=False dev={dev!r} backend={backend_name}"
                logger.warning(last_error)
                try:
                    cap.release()
                except Exception:
                    pass
                continue

            # 先 MJPG —— USB camera 在高分辨率下 YUYV 会被驱动限到几 fps
            try:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            except Exception:
                pass
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_SIZE[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_SIZE[1])
            cap.set(cv2.CAP_PROP_FPS, CAMERA_CAPTURE_FPS)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            # 验证能真读一帧。很多树莓派失败场景是 isOpened=True 但 read 返回 False
            ok, frame = cap.read()
            if ok and frame is not None:
                logger.info("摄像头连接成功 dev=%r backend=%s size=%dx%d",
                            dev, backend_name, frame.shape[1], frame.shape[0])
                self.camera_status = "ok"
                return cap

            last_error = f"isOpened=True 但 read 失败 dev={dev!r} backend={backend_name}"
            logger.warning(last_error)
            try:
                cap.release()
            except Exception:
                pass

        logger.error("本地摄像头所有候选均失败 source=%r last_error=%s", source, last_error)
        self.camera_status = "disconnected"
        # 返回一个空 VideoCapture,保持 _camera_loop 里现有的 isOpened()/read() 容错路径不崩
        return cv2.VideoCapture()

    def _build_local_candidates(self, source):
        """生成 (device, backend, backend_name) 候选列表,按尝试顺序排列。"""
        is_linux = sys.platform.startswith("linux")
        v4l2 = cv2.CAP_V4L2 if PREFER_V4L2_ON_LINUX and is_linux else None
        any_be = cv2.CAP_ANY

        # 字符串路径(/dev/videoN 或其他):直接用,Linux 上优先 V4L2
        if isinstance(source, str):
            cands = []
            if v4l2 is not None and source.startswith("/dev/"):
                cands.append((source, v4l2, "V4L2"))
            cands.append((source, any_be, "ANY"))
            return cands

        # 数字 source:Linux 上扩展成多个候选,先 /dev/videoN+V4L2,再 N+V4L2,再 N+ANY
        if isinstance(source, int):
            if is_linux:
                cands = []
                if v4l2 is not None:
                    cands.append((f"/dev/video{source}", v4l2, "V4L2(/dev/video{})".format(source)))
                    cands.append((source, v4l2, "V4L2(index)"))
                cands.append((source, any_be, "ANY"))
                return cands
            # 非 Linux:保持原有行为
            return [(source, None, "default")]

        # 其他类型:原样透传
        return [(source, None, "default")]

    def _connect_remote_camera(self, source):
        """远程 RTSP / HTTP 流。保留原有重连逻辑,只是从 _connect_camera 拆出来更清楚。"""
        cap = cv2.VideoCapture(source)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_SIZE[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_SIZE[1])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
        for _ in range(REMOTE_CONNECT_RETRIES):
            if cap.isOpened():
                ok, _ = cap.read()
                if ok:
                    self.camera_status = "ok"
                    return cap
            time.sleep(REMOTE_RETRY_DELAY)
            cap.release()
            cap = cv2.VideoCapture(source)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_SIZE[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_SIZE[1])
        self.camera_status = "disconnected"
        return cap

    def _reconnect_camera(self):
        with self._cap_lock:
            if self.cap is not None:
                try:
                    self.cap.release()
                except Exception:
                    pass
                self.cap = None
            time.sleep(REMOTE_RETRY_DELAY)
            self.cap = self._connect_camera(self.current_camera)

    def _swap_cap(self, new_source):
        with self._cap_lock:
            old_cap = self.cap
            self.cap = None

        new_cap = self._connect_camera(new_source)
        is_ok = new_cap is not None and new_cap.isOpened()
        if is_ok:
            if old_cap is not None:
                try:
                    old_cap.release()
                except Exception:
                    pass
            with self._cap_lock:
                self.cap = new_cap
                self.current_camera = new_source
            return True

        if new_cap is not None:
            try:
                new_cap.release()
            except Exception:
                pass
        with self._cap_lock:
            self.cap = old_cap
        return False

    def _switch_camera(self):
        if (
            self._is_remote_source(self.current_camera)
            or self._is_rpicam_source(self.current_camera)
            or isinstance(self.current_camera, str)
        ):
            return False
        return self._swap_cap(1 - self.current_camera)

    def _camera_loop(self):
        frame_count = 0
        fps_t0 = time.time()
        fps_count = 0

        while not self._stop_event.is_set():
            with self._cap_lock:
                cap = self.cap
            if cap is None:
                time.sleep(0.1)
                continue

            try:
                if not cap.isOpened():
                    self.camera_status = "disconnected"
                    time.sleep(0.2)
                    continue
                ret, frame = cap.read()
            except cv2.error:
                time.sleep(0.05)
                continue
            except Exception:
                time.sleep(0.1)
                continue

            if not ret:
                self.camera_status = "disconnected"
                if REMOTE_RECONNECT_ON_LOST and self._is_remote_source(self.current_camera):
                    self._reconnect_camera()
                    continue
                time.sleep(0.2)
                continue

            self.camera_status = "ok"
            frame = cv2.flip(frame, 1)
            frame_count += 1
            fps_count += 1

            if time.time() - fps_t0 >= 1.0:
                self.current_fps = fps_count
                fps_count = 0
                fps_t0 = time.time()

            try:
                if self.is_recognition_enabled:
                    annotated, status = self.process_frame(frame, frame_count)
                else:
                    annotated = frame.copy()
                    status = self._paused_status()
            except Exception as exc:
                annotated = frame.copy()
                status = self._error_status(str(exc))

            if status is None:
                status = self._default_status()

            if self.is_recording and self.video_writer is not None:
                self.video_writer.write(annotated)

            with self._frame_lock:
                self._latest_frame = annotated
                self._latest_status = status

    def set_recognition(self, enabled):
        self.is_recognition_enabled = bool(enabled)

    def apply_camera_source_value(self, source):
        if isinstance(source, str):
            source = source.strip()
            if not source:
                return False
            try:
                source = int(source)
            except ValueError:
                pass
        if source == self.current_camera:
            return True
        return self._swap_cap(source)

    def switch_camera(self):
        return bool(self._switch_camera())

    def start_recording(self):
        if self.is_recording:
            return self.recording_filename
        if not os.path.exists(OUTPUT_DIR):
            os.makedirs(OUTPUT_DIR)
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(OUTPUT_DIR, f"recording_{ts}.avi")
        fourcc = cv2.VideoWriter_fourcc(*VIDEO_FOURCC)
        self.video_writer = cv2.VideoWriter(filename, fourcc, VIDEO_FPS, VIDEO_SIZE)
        self.recording_start_time = time.time()
        self.recording_filename = os.path.abspath(filename)
        self.is_recording = True
        return self.recording_filename

    def stop_recording(self):
        if self.video_writer is not None:
            try:
                self.video_writer.release()
            except Exception:
                pass
            self.video_writer = None
        self.is_recording = False
        filename = self.recording_filename
        self.recording_filename = None
        return filename

    def get_latest_frame(self):
        with self._frame_lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame.copy()

    def get_latest_jpeg(self, quality=80):
        frame = self.get_latest_frame()
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            return None
        return buf.tobytes()

    def get_status_snapshot(self):
        with self._frame_lock:
            status = dict(self._latest_status or self._default_status())
        elapsed = time.time() - self.recording_start_time if self.is_recording else 0.0
        status.update({
            "app_title": self.app_title,
            "app_subtitle": self.app_subtitle,
            "app_icon": self.app_icon,
            "camera_status": self.camera_status,
            "camera_source": self._describe_source(self.current_camera),
            "camera_source_value": str(self.current_camera),
            "fps": self.current_fps,
            "recognition_enabled": self.is_recognition_enabled,
            "recording": self.is_recording,
            "recording_elapsed": elapsed,
            "recording_file": self.recording_filename,
            "output_dir": os.path.abspath(OUTPUT_DIR),
        })
        return status

    def close(self):
        self._stop_event.set()
        if getattr(self, "_camera_thread", None) is not None:
            self._camera_thread.join(timeout=1.0)
        self.stop_recording()
        with self._cap_lock:
            if self.cap is not None:
                try:
                    self.cap.release()
                except Exception:
                    pass
                self.cap = None
        try:
            self.cleanup_detector()
        except Exception:
            pass

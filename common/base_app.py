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

import os
import time
import threading
from abc import ABC, abstractmethod
from typing import Any, Optional

import cv2


DEFAULT_CAMERA_SOURCE = 0
REMOTE_CONNECT_RETRIES = 3
REMOTE_RETRY_DELAY = 2.0
REMOTE_RECONNECT_ON_LOST = True
OUTPUT_DIR = "recordings"
VIDEO_FPS = 20.0
VIDEO_SIZE = (640, 480)
VIDEO_FOURCC = "XVID"


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
        return isinstance(source, str)

    def _describe_source(self, source):
        if self._is_remote_source(source):
            return f"远程 {source}"
        return f"本地 #{source}"

    def _connect_camera(self, source):
        cap = cv2.VideoCapture(source)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, VIDEO_SIZE[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, VIDEO_SIZE[1])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if self._is_remote_source(source):
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

        self.camera_status = "ok" if cap.isOpened() else "disconnected"
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
        if self._is_remote_source(self.current_camera):
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

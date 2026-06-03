"""闭眼监控 - Web 运行时检测类。"""

import os
import sys
import time
import urllib.request
import cv2
import numpy as np

from mediapipe.tasks.python.vision import FaceLandmarker, FaceLandmarkerOptions, RunningMode
from mediapipe.tasks import python
from mediapipe import Image as MPImage
from mediapipe.tasks.python.vision.core import image as vision_image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.base_app import BaseCameraApp

# --------------------- 模型 --------------------- #
FACE_MODEL_PATH = "face_landmarker.task"
FACE_MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task"

# --------------------- 阈值 --------------------- #
CLOSED_EYE_DISTANCE = 0.015
MIN_CLOSED_DURATION = 3.0
SHOCK_COOLDOWN = 2.0


def download_models():
    if not os.path.exists(FACE_MODEL_PATH):
        print(f"下载面部模型: {FACE_MODEL_PATH}")
        urllib.request.urlretrieve(FACE_MODEL_URL, FACE_MODEL_PATH)


class ClosedEyeApp(BaseCameraApp):
    app_title = "闭眼监控"
    app_subtitle = "检测闭眼时长，超过阈值自动提醒"
    app_icon = "👁"
    mode_key = "closed_eye"  # 对应 config/devices.yaml 顶层 key

    def __init__(self, source=None, dispatcher=None):
        self.last_face_landmarks = None
        self.last_face_frame = 0
        self.eye_closed_start_time = 0
        self.last_eye_state = False
        self.is_closed = False
        self.last_shock_time = 0
        self.eye_closed_duration = 0.0
        self.right_dist = None
        self.left_dist = None
        self.face_detected = False

        super().__init__(
            work_dir=os.path.dirname(os.path.abspath(__file__)),
            initial_source=source,
            dispatcher=dispatcher,
        )

    def setup_detector(self):
        download_models()
        base_options = python.BaseOptions(model_asset_path=FACE_MODEL_PATH)
        options = FaceLandmarkerOptions(
            base_options=base_options,
            output_face_blendshapes=False,
            running_mode=RunningMode.IMAGE,
        )
        self.detector = FaceLandmarker.create_from_options(options)

    def cleanup_detector(self):
        try:
            self.detector.close()
        except Exception:
            pass

    @staticmethod
    def _dist(p1, p2):
        return ((p2.x - p1.x) ** 2 + (p2.y - p1.y) ** 2) ** 0.5

    def _is_eyes_closed(self, face):
        r = self._dist(face[159], face[145])
        l = self._dist(face[386], face[374])
        return r < CLOSED_EYE_DISTANCE and l < CLOSED_EYE_DISTANCE, r, l

    def _draw_eyes(self, image, face):
        h, w = image.shape[:2]
        for idx in [159, 145, 386, 374]:
            p = face[idx]
            cv2.circle(image, (int(p.x * w), int(p.y * h)), 6, (0, 255, 255), -1)

    def process_frame(self, image, frame_count):
        if (frame_count - self.last_face_frame) >= 2 or self.last_face_landmarks is None:
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            mp_img = MPImage(image_format=vision_image.ImageFormat.SRGB, data=rgb)
            result = self.detector.detect(mp_img)
            if result and result.face_landmarks:
                self.last_face_landmarks = result.face_landmarks[0]
            else:
                self.last_face_landmarks = None
            self.last_face_frame = frame_count

        current_closed = False
        self.right_dist = self.left_dist = None
        self.face_detected = self.last_face_landmarks is not None

        if self.last_face_landmarks is not None:
            self._draw_eyes(image, self.last_face_landmarks)
            current_closed, self.right_dist, self.left_dist = self._is_eyes_closed(self.last_face_landmarks)

        # 时间判断
        if current_closed:
            if self.last_eye_state:
                self.eye_closed_duration = time.time() - self.eye_closed_start_time
                if self.eye_closed_duration >= MIN_CLOSED_DURATION:
                    now = time.time()
                    if now - self.last_shock_time >= SHOCK_COOLDOWN:
                        self.is_closed = True
                        self.last_shock_time = now
                        # 触发外设(由 config/devices.yaml 决定接谁; 未配置则静默)
                        self.emit_action("shock", duration=self.eye_closed_duration)
            else:
                self.eye_closed_start_time = time.time()
                self.is_closed = False
                self.eye_closed_duration = 0.0
        else:
            self.eye_closed_start_time = time.time()
            self.is_closed = False
            self.eye_closed_duration = 0.0
        self.last_eye_state = current_closed

        status = self._build_status()
        return image, status

    def _build_status(self):
        cooldown_remaining = 0.0
        if self.last_shock_time > 0:
            cooldown_remaining = max(0.0, SHOCK_COOLDOWN - (time.time() - self.last_shock_time))

        if self.is_closed:
            state, color, main_text, sub_text = (
                "闭眼触发！", "red",
                f"已闭眼 {self.eye_closed_duration:.1f}s",
                f"冷却 {cooldown_remaining:.1f}s",
            )
        elif self.eye_closed_duration > 0:
            state, color, main_text, sub_text = (
                "检测中", "orange",
                f"正在闭眼... {self.eye_closed_duration:.1f}s",
                f"达到 {MIN_CLOSED_DURATION:.0f}s 会触发",
            )
        elif not self.face_detected:
            state, color, main_text, sub_text = (
                "未检测到人脸", "gray",
                "请把脸对准摄像头",
                "",
            )
        else:
            state, color, main_text, sub_text = (
                "睁眼", "green",
                "状态正常",
                "持续监测中",
            )

        stats = []
        if self.right_dist is not None:
            stats.append(("右眼距离", f"{self.right_dist:.4f}"))
        if self.left_dist is not None:
            stats.append(("左眼距离", f"{self.left_dist:.4f}"))
        stats.append(("阈值", f"{CLOSED_EYE_DISTANCE:.4f}"))
        stats.append(("面部", "已锁定" if self.face_detected else "未检测"))
        if cooldown_remaining > 0:
            stats.append(("冷却剩余", f"{cooldown_remaining:.1f}s"))

        progress = min(1.0, self.eye_closed_duration / MIN_CLOSED_DURATION)
        return {
            "state": state, "state_color": color,
            "alert": self.is_closed,
            "main_text": main_text, "sub_text": sub_text,
            "stats": stats, "progress": progress,
        }

"""俯卧撑门禁 - Web 运行时检测类。"""

import os
import sys
import time
import urllib.request
import cv2
import numpy as np

from mediapipe.tasks.python.vision import PoseLandmarker, PoseLandmarkerOptions, RunningMode
from mediapipe.tasks import python
from mediapipe import Image as MPImage
from mediapipe.tasks.python.vision.core import image as vision_image

# 允许 import common.base_app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.base_app import BaseCameraApp

# --------------------- 模型 --------------------- #
POSE_MODEL_PATH = "pose_landmarker.task"
POSE_MODEL_URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task"

# --------------------- 检测参数（与原版一致） --------------------- #
PUSHUP_MIN_REPS = 3
PUSHUP_TARGET_MIN = 1            # 允许设置的最小目标次数
PUSHUP_TARGET_MAX = 100          # 允许设置的最大目标次数
ELBOW_DOWN_THRESHOLD = 145
ELBOW_UP_THRESHOLD = 150
SHOULDER_HIP_ANGLE_MIN = 130
PLANK_TILT_MAX = 55
SHOULDER_WRIST_DOWN_RATIO = 0.32
SHOULDER_WRIST_UP_RATIO = 0.48
MIN_WRIST_BELOW_SHOULDER_RATIO = 0.06
PUSHUP_COOLDOWN = 5.0
ANGLE_BUFFER_SIZE = 5
VIS_THRESHOLD = 0.5


def download_models():
    if not os.path.exists(POSE_MODEL_PATH):
        print(f"下载姿态模型: {POSE_MODEL_PATH}")
        urllib.request.urlretrieve(POSE_MODEL_URL, POSE_MODEL_PATH)


class PushupApp(BaseCameraApp):
    app_title = "俯卧撑门禁"
    app_subtitle = "做完 3 个俯卧撑，冰箱门才会打开"
    app_icon = "💪"
    mode_key = "pushup"  # 对应 config/devices.yaml 顶层 key

    def __init__(self, source=None, dispatcher=None):
        # 检测相关状态
        self.last_landmarks = None
        self.last_landmarks_frame = 0
        self.angle_buffer = []

        self.is_pushup = False
        self.pushup_start_time = 0
        self.last_pushup_state = False
        self.rep_count = 0
        self.last_stage = None
        self.last_trigger_time = 0
        self.is_triggered = False

        # 触发所需的俯卧撑次数（默认 3，UI 可调整）
        self.target_reps = PUSHUP_MIN_REPS

        # 当前调试值
        self.current_arm_angle = None
        self.current_body_angle = None
        self.current_body_tilt = None
        self.current_shoulder_wrist_ratio = None

        super().__init__(
            work_dir=os.path.dirname(os.path.abspath(__file__)),
            initial_source=source,
            dispatcher=dispatcher,
        )

    def setup_detector(self):
        download_models()
        base_options = python.BaseOptions(model_asset_path=POSE_MODEL_PATH)
        options = PoseLandmarkerOptions(
            base_options=base_options, num_poses=1, running_mode=RunningMode.IMAGE,
        )
        self.detector = PoseLandmarker.create_from_options(options)

    def cleanup_detector(self):
        try:
            self.detector.close()
        except Exception:
            pass

    # ---------- 运行时可调参数 ---------- #
    def set_target_reps(self, value):
        """设置触发解锁所需的俯卧撑次数。越界或非整数抛 ValueError。"""
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"目标次数必须是整数,收到: {value!r}")
        if n < PUSHUP_TARGET_MIN or n > PUSHUP_TARGET_MAX:
            raise ValueError(
                f"目标次数需在 {PUSHUP_TARGET_MIN}-{PUSHUP_TARGET_MAX} 之间"
            )
        self.target_reps = n
        # 调整目标后,本次会话的次数也复位,避免残留计数立刻触发
        self.rep_count = 0
        self.last_stage = None
        return n

    # ---------- 几何工具 ---------- #
    @staticmethod
    def _angle(p1, p2, p3):
        v1 = np.array([p1.x - p2.x, p1.y - p2.y])
        v2 = np.array([p3.x - p2.x, p3.y - p2.y])
        cos = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-6)
        cos = np.clip(cos, -1.0, 1.0)
        return np.degrees(np.arccos(cos))

    @staticmethod
    def _dist(p1, p2):
        return np.sqrt((p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2)

    @staticmethod
    def _tilt(p1, p2):
        dx, dy = abs(p2.x - p1.x), abs(p2.y - p1.y)
        return np.degrees(np.arctan2(dy, dx + 1e-6))

    # ---------- 检测 ---------- #
    def _detect(self, landmarks):
        def side_visible(*idx):
            return all(landmarks[i].visibility >= VIS_THRESHOLD for i in idx)

        L_arm = side_visible(11, 13, 15)
        R_arm = side_visible(12, 14, 16)
        L_body_a = side_visible(11, 23, 27)
        R_body_a = side_visible(12, 24, 28)
        L_body_k = side_visible(11, 23, 25)
        R_body_k = side_visible(12, 24, 26)
        L_body = L_body_a or L_body_k
        R_body = R_body_a or R_body_k
        if not (L_arm or R_arm) or not (L_body or R_body):
            return False, None, None, None, None, None

        def metrics(use_left):
            s, e, w = (11, 13, 15) if use_left else (12, 14, 16)
            hip = 23 if use_left else 24
            end = 27 if (use_left and L_body_a) else (28 if (not use_left and R_body_a) else
                    (25 if use_left and L_body_k else (26 if (not use_left) and R_body_k else None)))
            if end is None:
                return None
            sh, el, wr, hp, be = landmarks[s], landmarks[e], landmarks[w], landmarks[hip], landmarks[end]
            arm = self._angle(sh, el, wr)
            body = self._angle(sh, hp, be)
            tilt = self._tilt(sh, be)
            ratio = (wr.y - sh.y) / (self._dist(sh, be) + 1e-6)
            score = (sh.visibility + el.visibility + wr.visibility + hp.visibility + be.visibility) / 5.0
            return {"arm": arm, "body": body, "tilt": tilt, "ratio": ratio, "score": score}

        cands = []
        if L_arm and L_body:
            m = metrics(True)
            if m: cands.append(m)
        if R_arm and R_body:
            m = metrics(False)
            if m: cands.append(m)
        if not cands:
            return False, None, None, None, None, None

        best = max(cands, key=lambda x: x["score"])
        arm, body, tilt, ratio = best["arm"], best["body"], best["tilt"], best["ratio"]

        # 平滑
        self.angle_buffer.append((arm, body, tilt, ratio))
        if len(self.angle_buffer) > ANGLE_BUFFER_SIZE:
            self.angle_buffer.pop(0)
        if len(self.angle_buffer) >= 3:
            arm = sum(a for a, _, _, _ in self.angle_buffer) / len(self.angle_buffer)
            body = sum(b for _, b, _, _ in self.angle_buffer) / len(self.angle_buffer)
            tilt = sum(t for _, _, t, _ in self.angle_buffer) / len(self.angle_buffer)
            ratio = sum(r for _, _, _, r in self.angle_buffer) / len(self.angle_buffer)

        active = (body > SHOULDER_HIP_ANGLE_MIN and tilt < PLANK_TILT_MAX
                  and ratio > MIN_WRIST_BELOW_SHOULDER_RATIO)
        if not active:
            return False, None, arm, body, tilt, ratio

        if ratio <= SHOULDER_WRIST_DOWN_RATIO or arm < ELBOW_DOWN_THRESHOLD:
            stage = "down"
        elif ratio >= SHOULDER_WRIST_UP_RATIO or arm > ELBOW_UP_THRESHOLD:
            stage = "up"
        else:
            stage = None
        return True, stage, arm, body, tilt, ratio

    def _draw_landmarks(self, image, landmarks):
        h, w = image.shape[:2]
        connections = [
            (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
            (11, 23), (12, 24), (23, 24),
            (23, 25), (25, 27), (24, 26), (26, 28),
        ]
        for i, j in connections:
            p1, p2 = landmarks[i], landmarks[j]
            cv2.line(image, (int(p1.x * w), int(p1.y * h)),
                     (int(p2.x * w), int(p2.y * h)), (0, 255, 0), 3)
        for idx in [11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28]:
            p = landmarks[idx]
            cv2.circle(image, (int(p.x * w), int(p.y * h)), 8, (0, 200, 255), -1)

    # ---------- 主处理 ---------- #
    def process_frame(self, image, frame_count):
        # 每 N 帧做一次检测
        need = ((frame_count - self.last_landmarks_frame) >= 2) or (self.last_landmarks is None)
        if need:
            rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            mp_img = MPImage(image_format=vision_image.ImageFormat.SRGB, data=rgb)
            result = self.detector.detect(mp_img)
            if result and result.pose_landmarks:
                self.last_landmarks = result.pose_landmarks[0]
                self.last_landmarks_frame = frame_count
            else:
                self.last_landmarks = None

        landmarks = self.last_landmarks
        current_detected = False
        current_stage = None
        pushup_duration = 0.0

        if landmarks is not None and self.is_recognition_enabled:
            self._draw_landmarks(image, landmarks)
            is_active, stage, arm, body, tilt, ratio = self._detect(landmarks)
            self.current_arm_angle = arm
            self.current_body_angle = body
            self.current_body_tilt = tilt
            self.current_shoulder_wrist_ratio = ratio
            if is_active:
                current_detected = True
                current_stage = stage
        else:
            self.current_arm_angle = self.current_body_angle = None
            self.current_body_tilt = self.current_shoulder_wrist_ratio = None

        # 状态机
        if current_detected:
            self.is_pushup = True
            if self.last_pushup_state:
                pushup_duration = time.time() - self.pushup_start_time
                if current_stage == "up" and self.last_stage == "down":
                    self.rep_count += 1
                    self.last_stage = current_stage
                elif current_stage is not None and current_stage != self.last_stage:
                    self.last_stage = current_stage
            else:
                self.pushup_start_time = time.time()
                self.rep_count = 0
                if current_stage is not None:
                    self.last_stage = current_stage

            if self.rep_count >= self.target_reps:
                now = time.time()
                if now - self.last_trigger_time >= PUSHUP_COOLDOWN:
                    self.last_trigger_time = now
                    self.is_triggered = True
                    # 触发开锁(dispatcher 还有一道冷却)
                    self.emit_action("unlock", reps=self.rep_count)
        else:
            self.pushup_start_time = 0
            self.is_pushup = False
            self.last_stage = None
            self.rep_count = 0
            self.angle_buffer = []
        self.last_pushup_state = current_detected

        cooldown_remaining = 0.0
        if self.last_trigger_time > 0:
            cooldown_remaining = max(0.0, PUSHUP_COOLDOWN - (time.time() - self.last_trigger_time))
            if cooldown_remaining == 0:
                self.is_triggered = False

        # 在画面上加触发提示（仅文字，UI 已搬到侧栏）
        if self.is_triggered:
            cv2.rectangle(image, (0, image.shape[0] - 60), (image.shape[1], image.shape[0]),
                          (40, 0, 0), -1)
            cv2.putText(image, "***  DOOR OPEN  ***", (image.shape[1] // 2 - 180, image.shape[0] - 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 3)

        status = self._build_status(cooldown_remaining, current_stage, pushup_duration)
        return image, status

    def _build_status(self, cooldown_remaining, current_stage, pushup_duration):
        if self.is_triggered:
            state, color, alert, main_text, sub_text = (
                "开锁！", "green", False,
                f"完成 {self.rep_count}/{self.target_reps} 个俯卧撑",
                f"持续 {pushup_duration:.1f}s · 冷却 {cooldown_remaining:.1f}s",
            )
        elif self.is_pushup:
            stage_cn = {"down": "下压中", "up": "撑起", None: "保持"}[current_stage]
            state, color, alert, main_text, sub_text = (
                "检测中", "orange", False,
                f"俯卧撑中 - {stage_cn}",
                f"次数 {self.rep_count}/{self.target_reps} · 持续 {pushup_duration:.1f}s",
            )
        else:
            state, color, alert, main_text, sub_text = (
                "等待开始", "blue", False,
                "请开始做俯卧撑",
                f"需要完成 {self.target_reps} 个俯卧撑",
            )

        stats = []
        if self.current_arm_angle is not None:
            stats.append(("臂角", f"{self.current_arm_angle:.0f}°"))
        if self.current_body_angle is not None:
            stats.append(("身角", f"{self.current_body_angle:.0f}°"))
        if self.current_body_tilt is not None:
            stats.append(("体倾", f"{self.current_body_tilt:.0f}°"))
        if self.current_shoulder_wrist_ratio is not None:
            stats.append(("肩腕比", f"{self.current_shoulder_wrist_ratio:.2f}"))
        stats.append(("完成次数", f"{self.rep_count} / {self.target_reps}"))
        if cooldown_remaining > 0:
            stats.append(("冷却剩余", f"{cooldown_remaining:.1f}s"))

        progress = min(1.0, self.rep_count / self.target_reps)
        return {
            "state": state, "state_color": color, "alert": alert,
            "main_text": main_text, "sub_text": sub_text,
            "stats": stats, "progress": progress,
            "target_reps": self.target_reps,
        }

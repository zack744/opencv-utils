"""咬手指识别 - Web 运行时检测类。"""

import os
import sys
import time
import urllib.request
import cv2
import numpy as np

from mediapipe.tasks.python.vision import HandLandmarker, HandLandmarkerOptions, RunningMode
from mediapipe.tasks.python.vision import FaceLandmarker, FaceLandmarkerOptions
from mediapipe.tasks import python
import mediapipe as mp

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.base_app import BaseCameraApp

# --------------------- 模型 --------------------- #
HAND_MODEL_PATH = "hand_landmarker.task"
HAND_MODEL_URL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task"
FACE_MODEL_PATH = "face_landmarker.task"
FACE_MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task"

# --------------------- 阈值（与原版一致） --------------------- #
FINGER_TO_LIP_DISTANCE = 0.06
FALLBACK_DISTANCE = 0.12
FALLBACK_BEND_THRESHOLD = 0.12
MIN_BITE_DURATION = 0.3

FINGER_TIPS = [(4, "大拇指"), (8, "食指"), (12, "中指"),
               (16, "无名指"), (20, "小指")]


def download_models():
    for path, url, name in [
        (HAND_MODEL_PATH, HAND_MODEL_URL, "手部"),
        (FACE_MODEL_PATH, FACE_MODEL_URL, "面部"),
    ]:
        if not os.path.exists(path):
            print(f"下载{name}模型: {path}")
            urllib.request.urlretrieve(url, path)


class BiteFingerApp(BaseCameraApp):
    app_title = "咬手指识别"
    app_subtitle = "通过手-唇距离判断，提醒你别再啃指甲啦"
    app_icon = "🦷"
    mode_key = "bite_finger"  # 对应 config/devices.yaml 顶层 key

    def __init__(self, source=None, dispatcher=None):
        # 状态
        self.last_hand_landmarks = None
        self.last_hand_frame = 0
        self.last_face_landmarks = None
        self.last_face_frame = 0

        self.bite_start_time = 0
        self.last_bite_state = False
        self.is_biting = False
        self.biting_finger = None
        self.last_distance = None
        self.using_fallback = False
        self.hand_count = 0
        self.mode = "精确"

        # 用于边沿触发(从 False → True 那一帧才 emit,避免持续咬时反复触发)
        self._last_alert_state = False

        super().__init__(
            work_dir=os.path.dirname(os.path.abspath(__file__)),
            initial_source=source,
            dispatcher=dispatcher,
        )

    def setup_detector(self):
        download_models()
        hand_opts = HandLandmarkerOptions(
            base_options=python.BaseOptions(model_asset_path=HAND_MODEL_PATH),
            num_hands=2, running_mode=RunningMode.IMAGE,
        )
        self.hand_detector = HandLandmarker.create_from_options(hand_opts)
        face_opts = FaceLandmarkerOptions(
            base_options=python.BaseOptions(model_asset_path=FACE_MODEL_PATH),
            output_face_blendshapes=False, running_mode=RunningMode.IMAGE,
        )
        self.face_detector = FaceLandmarker.create_from_options(face_opts)

    def cleanup_detector(self):
        for d in (getattr(self, "hand_detector", None),
                  getattr(self, "face_detector", None)):
            try:
                if d: d.close()
            except Exception:
                pass

    # ---------- 几何工具 ---------- #
    @staticmethod
    def _dist2d(p1, p2):
        return np.sqrt((p1.x - p2.x) ** 2 + (p1.y - p2.y) ** 2)

    @staticmethod
    def _lip_center(face):
        u, l = face[13], face[14]

        class _P:
            def __init__(self, x, y, z):
                self.x, self.y, self.z = x, y, z
        return _P((u.x + l.x) / 2, (u.y + l.y) / 2, (u.z + l.z) / 2)

    # ---------- 检测 ---------- #
    def _check_finger_near_lip(self, hand, lip):
        best = (False, None, float("inf"))
        for idx, name in FINGER_TIPS:
            tip = hand[idx]
            d = self._dist2d(tip, lip)
            if d < FINGER_TO_LIP_DISTANCE and d < best[2]:
                best = (True, name, d)
        return best  # (is_biting, finger, dist)

    def _check_fallback(self, hand):
        target_x, target_y = 0.5, 0.75
        best = (False, None, float("inf"))
        bent_count = 0
        for idx, name in FINGER_TIPS:
            tip = hand[idx]
            dist_mouth = np.sqrt((tip.x - target_x) ** 2 + (tip.y - target_y) ** 2)
            wrist = hand[0]
            tip_wrist = self._dist2d(tip, wrist)
            if tip_wrist < FALLBACK_BEND_THRESHOLD:
                bent_count += 1
                if dist_mouth < FALLBACK_DISTANCE and dist_mouth < best[2]:
                    best = (True, name, dist_mouth)
        if bent_count >= 4:  # 排除握拳
            return (False, None, None)
        return best

    def _draw_hand(self, image, hand):
        h, w = image.shape[:2]
        connections = [
            (0, 1), (1, 2), (2, 3), (3, 4),
            (0, 5), (5, 6), (6, 7), (7, 8),
            (0, 9), (9, 10), (10, 11), (11, 12),
            (0, 13), (13, 14), (14, 15), (15, 16),
            (0, 17), (17, 18), (18, 19), (19, 20),
            (5, 9), (9, 13), (13, 17),
        ]
        for i, j in connections:
            p1, p2 = hand[i], hand[j]
            cv2.line(image, (int(p1.x * w), int(p1.y * h)),
                     (int(p2.x * w), int(p2.y * h)), (0, 255, 0), 2)
        for p in hand:
            cv2.circle(image, (int(p.x * w), int(p.y * h)), 5, (0, 255, 255), -1)

    def _draw_lip(self, image, face):
        h, w = image.shape[:2]
        for idx in [13, 14, 61, 291]:
            p = face[idx]
            cv2.circle(image, (int(p.x * w), int(p.y * h)), 8, (255, 0, 255), -1)

    # ---------- 主处理 ---------- #
    def process_frame(self, image, frame_count):
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

        # 间隔检测
        if (frame_count - self.last_hand_frame) >= 2 or self.last_hand_landmarks is None:
            hr = self.hand_detector.detect(mp_img)
            self.last_hand_landmarks = hr.hand_landmarks if hr.hand_landmarks else None
            self.last_hand_frame = frame_count
        if (frame_count - self.last_face_frame) >= 2 or self.last_face_landmarks is None:
            fr = self.face_detector.detect(mp_img)
            if fr and fr.face_landmarks:
                self.last_face_landmarks = fr.face_landmarks[0]
            else:
                self.last_face_landmarks = None
            self.last_face_frame = frame_count

        lip = None
        if self.last_face_landmarks is not None:
            self._draw_lip(image, self.last_face_landmarks)
            lip = self._lip_center(self.last_face_landmarks)

        current_biting = False
        current_finger = None
        current_dist = None

        if self.last_hand_landmarks is not None:
            self.hand_count = len(self.last_hand_landmarks)
            for hand in self.last_hand_landmarks:
                self._draw_hand(image, hand)
                if lip is not None:
                    self.using_fallback = False
                    biting, finger, dist = self._check_finger_near_lip(hand, lip)
                else:
                    self.using_fallback = True
                    biting, finger, dist = self._check_fallback(hand)
                if biting:
                    current_biting = True
                    current_finger = finger
                    current_dist = dist
        else:
            self.hand_count = 0

        self.mode = "回退" if self.using_fallback else "精确"

        # 持续时间
        if current_biting:
            if self.last_bite_state:
                if time.time() - self.bite_start_time >= MIN_BITE_DURATION:
                    self.is_biting = True
                    self.biting_finger = current_finger
                    self.last_distance = current_dist
            else:
                self.bite_start_time = time.time()
                self.is_biting = False
        else:
            self.bite_start_time = 0
            self.is_biting = False
            self.last_distance = None
        self.last_bite_state = current_biting

        # 边沿触发(刚进入 is_biting 的那一帧触发外设,由 dispatcher 自己再做冷却)
        if self.is_biting and not self._last_alert_state:
            self.emit_action(
                "alert",
                finger=self.biting_finger,
                distance=self.last_distance,
                detect_mode=self.mode,   # 不要叫 mode —— 会与 dispatcher.dispatch(mode, action, ...) 形参撞名
            )
        self._last_alert_state = self.is_biting

        status = self._build_status()
        return image, status

    def _build_status(self):
        if self.is_biting:
            state, color, main_text, sub_text = (
                "咬手指！", "red",
                f"检测到咬{self.biting_finger}",
                f"持续 ≥ {MIN_BITE_DURATION:.1f}s · {self.mode}模式",
            )
        else:
            state, color, main_text, sub_text = (
                "正常", "green",
                "未检测到咬手指动作",
                f"{self.mode}模式 · 检测到 {self.hand_count} 只手",
            )

        stats = [
            ("检测模式", self.mode),
            ("手部数量", str(self.hand_count)),
        ]
        if self.is_biting and self.last_distance is not None:
            stats.append(("距离", f"{self.last_distance:.3f}"))
        elif self.last_face_landmarks is None:
            stats.append(("面部", "未检测到"))
        else:
            stats.append(("面部", "已锁定"))

        return {
            "state": state, "state_color": color, "alert": self.is_biting,
            "main_text": main_text, "sub_text": sub_text,
            "stats": stats, "progress": 0.0,
        }

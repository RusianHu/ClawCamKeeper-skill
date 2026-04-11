"""
检测链路模块
负责摄像头读取、人体检测、风险区域判定
"""

import threading
import time
import queue
import sys
from datetime import datetime
from typing import Callable, Optional
import cv2
import mediapipe as mp
import numpy as np
from loguru import logger


class DetectionResult:
    """检测结果"""
    def __init__(self, person_detected: bool, confidence: float = 0.0, 
                 bbox: Optional[tuple] = None, frame: Optional[np.ndarray] = None):
        self.person_detected = person_detected
        self.confidence = confidence
        self.bbox = bbox  # (x, y, w, h) 归一化坐标
        self.frame = frame  # 渲染后的帧（用于调试）
        self.timestamp = datetime.now()

    def to_dict(self) -> dict:
        return {
            "person_detected": self.person_detected,
            "confidence": self.confidence,
            "bbox": self.bbox,
            "timestamp": self.timestamp.isoformat()
        }


class Detector:
    """
    人体检测器
    使用 MediaPipe Pose 进行人体检测
    """

    def __init__(self, config: dict, on_detection: Optional[Callable[[DetectionResult], None]] = None):
        self.config = config
        self.on_detection = on_detection
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._result_queue = queue.Queue(maxsize=10)
        
        # 检测配置
        cam_config = config.get('camera', {})
        det_config = config.get('detection', {})
        
        self.device_index = cam_config.get('device_index', 0)
        self.frame_width = cam_config.get('frame_width', 640)
        self.frame_height = cam_config.get('frame_height', 480)
        self.fps = cam_config.get('fps', 30)
        self.camera_backend = cam_config.get('backend', 'auto')
        self.max_read_failures = cam_config.get('max_read_failures', 30)
        self.probe_frames = cam_config.get('probe_frames', 5)
        self.confidence_threshold = det_config.get('confidence_threshold', 0.5)
        
        # 风险区域配置
        risk_zone = det_config.get('risk_zone', {})
        self.risk_zone = {
            'x': risk_zone.get('x', 0.0),
            'y': risk_zone.get('y', 0.0),
            'width': risk_zone.get('width', 1.0),
            'height': risk_zone.get('height', 1.0)
        }
        
        # MediaPipe Pose
        self.mp_pose = mp.solutions.pose
        self.pose = self.mp_pose.Pose(
            static_image_mode=False,
            model_complexity=1,
            min_detection_confidence=self.confidence_threshold,
            min_tracking_confidence=self.confidence_threshold
        )
        
        # 摄像头
        self.cap: Optional[cv2.VideoCapture] = None
        
        # 最新检测结果
        self._latest_result: Optional[DetectionResult] = None
        self._result_lock = threading.Lock()

        # 运行时摄像头状态
        self._camera_runtime_available = False
        self._last_camera_error: Optional[str] = None
        self._consecutive_read_failures = 0
        self._total_read_failures = 0
        self._opened_backend = "uninitialized"
        self._last_success_time: Optional[datetime] = None

    @property
    def latest_result(self) -> Optional[DetectionResult]:
        with self._result_lock:
            return self._latest_result

    def get_camera_status(self) -> dict:
        """获取摄像头运行时状态"""
        return {
            "runtime_available": self._camera_runtime_available,
            "last_error": self._last_camera_error,
            "consecutive_read_failures": self._consecutive_read_failures,
            "total_read_failures": self._total_read_failures,
            "backend": self._opened_backend,
            "last_success_time": self._last_success_time.isoformat() if self._last_success_time else None,
        }

    def _get_backend_candidates(self) -> list[tuple[str, Optional[int]]]:
        """获取摄像头后端候选列表"""
        if self.camera_backend != 'auto':
            backend_name = str(self.camera_backend).upper()
            attr_name = backend_name if backend_name.startswith('CAP_') else f"CAP_{backend_name}"
            backend_value = getattr(cv2, attr_name, None)
            if backend_value is None:
                logger.warning(f"未知摄像头后端配置: {self.camera_backend}，将回退到自动模式")
            else:
                return [(attr_name, backend_value)]

        candidates = [("CAP_ANY", None)]
        if sys.platform.startswith("win"):
            candidates = [
                ("CAP_DSHOW", getattr(cv2, "CAP_DSHOW", None)),
                ("CAP_MSMF", getattr(cv2, "CAP_MSMF", None)),
                ("CAP_ANY", None),
            ]

        normalized = []
        for name, backend in candidates:
            if backend is None and name != "CAP_ANY":
                continue
            normalized.append((name, backend))
        return normalized

    def _configure_capture(self, cap: cv2.VideoCapture):
        """配置摄像头参数"""
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.frame_height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def _probe_capture(self, cap: cv2.VideoCapture) -> bool:
        """通过多次预热读帧验证摄像头稳定性"""
        success_count = 0
        required_success = 1 if self.probe_frames <= 2 else 2

        for _ in range(self.probe_frames):
            ret, frame = cap.read()
            if ret and frame is not None:
                success_count += 1
            time.sleep(0.05)

        return success_count >= required_success

    def _open_capture(self) -> bool:
        """尝试使用候选后端打开摄像头"""
        for backend_name, backend in self._get_backend_candidates():
            cap = None
            try:
                cap = cv2.VideoCapture(self.device_index) if backend is None else cv2.VideoCapture(self.device_index, backend)
                if not cap or not cap.isOpened():
                    if cap:
                        cap.release()
                    continue

                self._configure_capture(cap)
                time.sleep(0.2)

                if self._probe_capture(cap):
                    self.cap = cap
                    self._camera_runtime_available = True
                    self._last_camera_error = None
                    self._consecutive_read_failures = 0
                    self._opened_backend = backend_name
                    self._last_success_time = datetime.now()
                    logger.info(f"摄像头已打开: {self.frame_width}x{self.frame_height}@{self.fps}fps, backend={backend_name}")
                    return True

                logger.warning(f"摄像头后端预热失败: {backend_name}")
                cap.release()
            except Exception as e:
                if cap:
                    cap.release()
                logger.warning(f"摄像头后端打开失败 {backend_name}: {e}")

        self.cap = None
        self._camera_runtime_available = False
        self._opened_backend = "unavailable"
        self._last_camera_error = f"无法打开摄像头 (设备索引: {self.device_index})"
        return False

    def is_camera_available(self) -> bool:
        """检查摄像头是否可用"""
        try:
            success = self._open_capture()
            if self.cap:
                self.cap.release()
                self.cap = None
            self._camera_runtime_available = success
            return success
        except Exception as e:
            self._last_camera_error = str(e)
            logger.error(f"摄像头检查失败: {e}")
            return False

    def start(self):
        """启动检测线程"""
        with self._lock:
            if self._running:
                logger.warning("检测器已在运行")
                return
            
            self._running = True
            self._thread = threading.Thread(target=self._detection_loop, daemon=True, name="DetectorThread")
            self._thread.start()
            logger.info("检测器已启动")

    def stop(self):
        """停止检测线程"""
        with self._lock:
            self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if self.cap:
            self.cap.release()
            self.cap = None
        self._camera_runtime_available = False
        logger.info("检测器已停止")

    def _detection_loop(self):
        """检测主循环"""
        logger.info("检测循环启动")

        if not self._open_capture():
            logger.error(self._last_camera_error or f"无法打开摄像头 (设备索引: {self.device_index})")
            self._running = False
            return

        while self._running:
            try:
                if not self.cap:
                    if not self._open_capture():
                        time.sleep(1.0)
                        continue

                ret, frame = self.cap.read()
                if not ret or frame is None:
                    self._total_read_failures += 1
                    self._consecutive_read_failures += 1

                    if self._consecutive_read_failures == 1 or self._consecutive_read_failures % 10 == 0:
                        logger.warning(
                            f"无法读取摄像头帧 (连续失败: {self._consecutive_read_failures}, backend: {self._opened_backend})"
                        )

                    if self._consecutive_read_failures >= self.max_read_failures:
                        self._camera_runtime_available = False
                        self._last_camera_error = (
                            f"摄像头连续读取失败 {self._consecutive_read_failures} 次，backend={self._opened_backend}"
                        )
                        logger.error(f"{self._last_camera_error}，正在尝试重新打开摄像头")
                        if self.cap:
                            self.cap.release()
                            self.cap = None
                        if self._open_capture():
                            logger.info(f"摄像头重连成功，backend={self._opened_backend}")
                        else:
                            time.sleep(1.0)
                        continue

                    time.sleep(0.1)
                    continue

                if self._consecutive_read_failures > 0:
                    logger.info(f"摄像头读帧已恢复，连续失败次数已清零 (backend={self._opened_backend})")

                self._camera_runtime_available = True
                self._last_camera_error = None
                self._consecutive_read_failures = 0
                self._last_success_time = datetime.now()

                result = self._process_frame(frame)

                # 保存最新结果
                with self._result_lock:
                    self._latest_result = result

                # 通知回调
                if self.on_detection:
                    try:
                        self.on_detection(result)
                    except Exception as e:
                        logger.error(f"检测回调异常: {e}")

                # 非阻塞放入队列
                try:
                    self._result_queue.put_nowait(result)
                except queue.Full:
                    pass

            except Exception as e:
                self._last_camera_error = str(e)
                logger.error(f"检测循环异常: {e}")
                time.sleep(0.1)

        logger.info("检测循环结束")

    def _process_frame(self, frame: np.ndarray) -> DetectionResult:
        """处理单帧图像"""
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.pose.process(rgb_frame)
        
        person_detected = False
        confidence = 0.0
        bbox = None
        
        # 渲染帧用于调试
        debug_frame = frame.copy()
        h, w = frame.shape[:2]
        
        if results.pose_landmarks:
            landmarks = results.pose_landmarks.landmark
            
            # 计算人体边界框
            x_coords = [lm.x for lm in landmarks]
            y_coords = [lm.y for lm in landmarks]
            
            min_x, max_x = min(x_coords), max(x_coords)
            min_y, max_y = min(y_coords), max(y_coords)
            
            bbox_w = max_x - min_x
            bbox_h = max_y - min_y
            
            # 检查是否在风险区域内
            if self._is_in_risk_zone(min_x, min_y, bbox_w, bbox_h):
                person_detected = True
                confidence = results.pose_landmarks.landmark[mp.solutions.pose.PoseLandmark.NOSE].visibility
                bbox = (min_x, min_y, bbox_w, bbox_h)
                
                # 在调试帧上绘制
                x1, y1 = int(min_x * w), int(min_y * h)
                x2, y2 = int((min_x + bbox_w) * w), int((min_y + bbox_h) * h)
                cv2.rectangle(debug_frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
                cv2.putText(debug_frame, f"DETECTED: {confidence:.2f}", (x1, y1 - 10),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
            else:
                # 检测到但不在风险区域
                x1, y1 = int(min_x * w), int(min_y * h)
                x2, y2 = int((min_x + bbox_w) * w), int((min_y + bbox_h) * h)
                cv2.rectangle(debug_frame, (x1, y1), (x2, y2), (0, 255, 0), 1)
        
        # 绘制风险区域边界
        rz = self.risk_zone
        rx1, ry1 = int(rz['x'] * w), int(rz['y'] * h)
        rx2, ry2 = int((rz['x'] + rz['width']) * w), int((rz['y'] + rz['height']) * h)
        cv2.rectangle(debug_frame, (rx1, ry1), (rx2, ry2), (255, 0, 0), 1)
        cv2.putText(debug_frame, "RISK ZONE", (rx1, ry1 - 5),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 0, 0), 1)
        
        return DetectionResult(
            person_detected=person_detected,
            confidence=confidence,
            bbox=bbox,
            frame=debug_frame
        )

    def _is_in_risk_zone(self, x: float, y: float, w: float, h: float) -> bool:
        """检查目标是否在风险区域内"""
        rz = self.risk_zone
        
        # 计算重叠区域
        overlap_x1 = max(x, rz['x'])
        overlap_y1 = max(y, rz['y'])
        overlap_x2 = min(x + w, rz['x'] + rz['width'])
        overlap_y2 = min(y + h, rz['y'] + rz['height'])
        
        if overlap_x2 <= overlap_x1 or overlap_y2 <= overlap_y1:
            return False
        
        # 计算重叠面积比例
        overlap_area = (overlap_x2 - overlap_x1) * (overlap_y2 - overlap_y1)
        target_area = w * h
        
        if target_area == 0:
            return False
        
        overlap_ratio = overlap_area / target_area
        return overlap_ratio > 0.3  # 30% 重叠即判定进入

    def get_result(self) -> Optional[DetectionResult]:
        """获取最新的检测结果（非阻塞）"""
        try:
            return self._result_queue.get_nowait()
        except queue.Empty:
            return None

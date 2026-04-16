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

    def __init__(
        self,
        person_detected: bool,
        confidence: float = 0.0,
        bbox: Optional[tuple] = None,
        frame: Optional[np.ndarray] = None,
        perf: Optional[dict] = None,
    ):
        self.person_detected = person_detected
        self.confidence = confidence
        self.bbox = bbox  # (x, y, w, h) 归一化坐标
        self.frame = frame  # 渲染后的帧（用于调试）
        self.timestamp = datetime.now()
        self.perf = perf or {}

    def to_dict(self) -> dict:
        return {
            "person_detected": self.person_detected,
            "confidence": self.confidence,
            "bbox": self.bbox,
            "timestamp": self.timestamp.isoformat(),
            "perf": self.perf,
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
        cam_config = config.get("camera", {})
        det_config = config.get("detection", {})

        self.device_index = cam_config.get("device_index", 0)
        self.frame_width = cam_config.get("frame_width", 640)
        self.frame_height = cam_config.get("frame_height", 480)
        self.fps = cam_config.get("fps", 30)
        self.camera_backend = cam_config.get("backend", "auto")
        self.max_read_failures = cam_config.get("max_read_failures", 30)
        self.probe_frames = cam_config.get("probe_frames", 5)
        self.quick_probe_frames = cam_config.get("quick_probe_frames", 1)
        quick_backend_order = cam_config.get("quick_probe_backend_order")
        if quick_backend_order is None:
            quick_backend_order = ["CAP_ANY"]
        self.quick_probe_backend_order = quick_backend_order
        self.quick_check_max_ms = cam_config.get("quick_check_max_ms", 1500)
        self.quick_probe_open_budget_ratio = float(cam_config.get("quick_probe_open_budget_ratio", 0.55))
        self.quick_probe_frame_budget_ms = int(cam_config.get("quick_probe_frame_budget_ms", 120))
        self.quick_probe_retries = int(cam_config.get("quick_probe_retries", 3))
        self.quick_probe_retry_delay_ms = int(cam_config.get("quick_probe_retry_delay_ms", 450))
        self.startup_warmup_ms = int(cam_config.get("startup_warmup_ms", 700))
        self.confidence_threshold = det_config.get("confidence_threshold", 0.5)

        # 风险区域配置
        risk_zone = det_config.get("risk_zone", {})
        self.risk_zone = {
            "x": risk_zone.get("x", 0.0),
            "y": risk_zone.get("y", 0.0),
            "width": risk_zone.get("width", 1.0),
            "height": risk_zone.get("height", 1.0),
        }

        # MediaPipe Pose
        self.mp_pose = mp.solutions.pose
        self.pose = self.mp_pose.Pose(
            static_image_mode=False,
            model_complexity=1,
            min_detection_confidence=self.confidence_threshold,
            min_tracking_confidence=self.confidence_threshold,
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

        # 性能状态
        self._perf_lock = threading.Lock()
        self._perf = {
            "open_capture": {
                "last_total_ms": 0.0,
                "last_backend": None,
                "attempts": [],
                "success": False,
                "last_probe_frames": self.probe_frames,
                "last_check_ms": 0.0,
                "last_check_mode": None,
            },
            "frame": {
                "last_read_ms": 0.0,
                "last_process_ms": 0.0,
                "last_total_loop_ms": 0.0,
                "last_queue_put_ms": 0.0,
                "last_callback_ms": 0.0,
                "last_timestamp": None,
                "last_person_detected": False,
                "last_confidence": 0.0,
            },
            "lifecycle": {
                "start_count": 0,
                "stop_count": 0,
                "last_start_ms": 0.0,
                "last_stop_ms": 0.0,
            },
        }

    @property
    def latest_result(self) -> Optional[DetectionResult]:
        with self._result_lock:
            return self._latest_result

    def _elapsed_ms(self, start: float, end: Optional[float] = None) -> float:
        """将 perf_counter 时间差换算为毫秒"""
        if end is None:
            end = time.perf_counter()
        return round((end - start) * 1000, 2)

    def _snapshot_perf(self) -> dict:
        """获取性能状态快照"""
        with self._perf_lock:
            return {
                "open_capture": {
                    "last_total_ms": self._perf["open_capture"]["last_total_ms"],
                    "last_backend": self._perf["open_capture"]["last_backend"],
                    "attempts": list(self._perf["open_capture"]["attempts"]),
                    "success": self._perf["open_capture"]["success"],
                    "last_probe_frames": self._perf["open_capture"]["last_probe_frames"],
                },
                "frame": dict(self._perf["frame"]),
                "lifecycle": dict(self._perf["lifecycle"]),
            }

    def _update_perf(self, section: str, **kwargs):
        """更新性能状态"""
        with self._perf_lock:
            self._perf[section].update(kwargs)

    def get_camera_status(self) -> dict:
        """获取摄像头运行时状态"""
        return {
            "runtime_available": self._camera_runtime_available,
            "last_error": self._last_camera_error,
            "consecutive_read_failures": self._consecutive_read_failures,
            "total_read_failures": self._total_read_failures,
            "backend": self._opened_backend,
            "last_success_time": self._last_success_time.isoformat() if self._last_success_time else None,
            "perf": self._snapshot_perf(),
        }

    def _get_backend_candidates(self, quick: bool = False) -> list[tuple[str, Optional[int]]]:
        """获取摄像头后端候选列表"""
        if quick:
            requested = self.quick_probe_backend_order or ["CAP_ANY"]
            normalized = []
            seen = set()
            for item in requested:
                backend_name = str(item).upper()
                attr_name = backend_name if backend_name.startswith("CAP_") else f"CAP_{backend_name}"
                if attr_name in seen:
                    continue
                seen.add(attr_name)
                if attr_name == "CAP_ANY":
                    normalized.append(("CAP_ANY", None))
                    continue
                backend_value = getattr(cv2, attr_name, None)
                if backend_value is not None:
                    normalized.append((attr_name, backend_value))
            if normalized:
                return normalized

        if self.camera_backend != "auto":
            backend_name = str(self.camera_backend).upper()
            attr_name = backend_name if backend_name.startswith("CAP_") else f"CAP_{backend_name}"
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

    def _configure_capture(self, cap: cv2.VideoCapture, *, minimal: bool = False):
        """配置摄像头参数"""
        if minimal:
            if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.frame_height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def _probe_capture(
        self,
        cap: cv2.VideoCapture,
        probe_frames: Optional[int] = None,
        *,
        quick: bool = False,
        budget_ms: Optional[float] = None,
    ) -> bool:
        """通过多次预热读帧验证摄像头稳定性"""
        frames = max(1, int(probe_frames or self.probe_frames))
        success_count = 0
        required_success = 1 if frames <= 2 else 2
        sleep_s = 0.01 if quick else 0.05
        probe_started_at = time.perf_counter()

        for _ in range(frames):
            if budget_ms is not None and self._elapsed_ms(probe_started_at) >= budget_ms:
                break
            ret, frame = cap.read()
            if ret and frame is not None:
                success_count += 1
                if quick:
                    return True
            if budget_ms is not None and self._elapsed_ms(probe_started_at) >= budget_ms:
                break
            time.sleep(sleep_s)

        return success_count >= required_success

    def _open_capture(self, *, quick: bool = False) -> bool:
        """尝试使用候选后端打开摄像头"""
        started_at = time.perf_counter()
        attempts = []
        probe_frames = self.quick_probe_frames if quick else self.probe_frames
        budget_ms = self.quick_check_max_ms if quick else None
        cycle_count = max(1, self.quick_probe_retries) if quick else 1

        for cycle_index in range(cycle_count):
            if quick and cycle_index == 0 and self.startup_warmup_ms > 0:
                time.sleep(max(0.0, self.startup_warmup_ms / 1000.0))

            for backend_name, backend in self._get_backend_candidates(quick=quick):
                if budget_ms is not None and self._elapsed_ms(started_at) >= budget_ms:
                    logger.warning(f"摄像头快速探测达到预算上限: {budget_ms}ms")
                    break

                attempt_started_at = time.perf_counter()
                cap = None
                attempt_info = {
                    "backend": backend_name,
                    "opened": False,
                    "probe_success": False,
                    "elapsed_ms": 0.0,
                    "mode": "quick" if quick else "full",
                    "cycle": cycle_index + 1,
                }
                try:
                    open_started_at = time.perf_counter()
                    cap = cv2.VideoCapture(self.device_index) if backend is None else cv2.VideoCapture(self.device_index, backend)
                    attempt_info["open_ms"] = self._elapsed_ms(open_started_at)
                    if not cap or not cap.isOpened():
                        if cap:
                            cap.release()
                        attempt_info["elapsed_ms"] = self._elapsed_ms(attempt_started_at)
                        attempts.append(attempt_info)
                        continue

                    attempt_info["opened"] = True
                    configure_started_at = time.perf_counter()
                    self._configure_capture(cap, minimal=quick)
                    attempt_info["configure_mode"] = "minimal" if quick else "full"
                    attempt_info["configure_ms"] = self._elapsed_ms(configure_started_at)

                    warmup_sleep_s = 0.02 if quick else 0.12
                    warmup_started_at = time.perf_counter()
                    time.sleep(warmup_sleep_s)
                    attempt_info["warmup_ms"] = self._elapsed_ms(warmup_started_at)

                    if quick and budget_ms is not None:
                        open_budget_ms = max(1.0, budget_ms * self.quick_probe_open_budget_ratio)
                        if float(attempt_info.get("open_ms") or 0.0) >= open_budget_ms:
                            attempt_info["probe_skipped"] = True
                            attempt_info["probe_skip_reason"] = "open_budget_exhausted"
                            attempt_info["elapsed_ms"] = self._elapsed_ms(attempt_started_at)
                            cap.release()
                            attempts.append(attempt_info)
                            continue

                    probe_budget_ms = self.quick_probe_frame_budget_ms if quick else None
                    probe_started_at = time.perf_counter()
                    probe_success = self._probe_capture(
                        cap,
                        probe_frames=probe_frames,
                        quick=quick,
                        budget_ms=probe_budget_ms,
                    )
                    attempt_info["probe_ms"] = self._elapsed_ms(probe_started_at)
                    attempt_info["probe_success"] = probe_success

                    if probe_success:
                        self.cap = cap
                        self._camera_runtime_available = True
                        self._last_camera_error = None
                        self._consecutive_read_failures = 0
                        self._opened_backend = backend_name
                        self._last_success_time = datetime.now()
                        attempt_info["elapsed_ms"] = self._elapsed_ms(attempt_started_at)
                        attempts.append(attempt_info)
                        self._update_perf(
                            "open_capture",
                            last_total_ms=self._elapsed_ms(started_at),
                            last_backend=backend_name,
                            attempts=attempts,
                            success=True,
                            last_probe_frames=probe_frames,
                        )
                        logger.info(
                            f"摄像头已打开: {self.frame_width}x{self.frame_height}@{self.fps}fps, "
                            f"backend={backend_name}, elapsed={self._elapsed_ms(started_at)}ms"
                        )
                        return True

                    logger.warning(f"摄像头后端预热失败: {backend_name}")
                    cap.release()
                    attempt_info["elapsed_ms"] = self._elapsed_ms(attempt_started_at)
                    attempts.append(attempt_info)
                except Exception as e:
                    if cap:
                        cap.release()
                    attempt_info["error"] = str(e)
                    attempt_info["elapsed_ms"] = self._elapsed_ms(attempt_started_at)
                    attempts.append(attempt_info)
                    logger.warning(f"摄像头后端打开失败 {backend_name}: {e}")

            if quick and cycle_index + 1 < cycle_count:
                if budget_ms is not None and self._elapsed_ms(started_at) >= budget_ms:
                    break
                time.sleep(max(0.0, self.quick_probe_retry_delay_ms / 1000.0))

        self.cap = None
        self._camera_runtime_available = False
        self._opened_backend = "unavailable"
        suffix = " (快速探测)" if quick else ""
        self._last_camera_error = f"无法打开摄像头{suffix} (设备索引: {self.device_index})"
        self._update_perf(
            "open_capture",
            last_total_ms=self._elapsed_ms(started_at),
            last_backend=None,
            attempts=attempts,
            success=False,
            last_probe_frames=probe_frames,
        )
        return False

    def is_camera_available(self, quick: bool = False) -> bool:
        """检查摄像头是否可用"""
        started_at = time.perf_counter()
        previous_cap = self.cap
        try:
            success = self._open_capture(quick=quick)
            if self.cap:
                self.cap.release()
                self.cap = None
            if previous_cap is not None:
                self.cap = previous_cap
            self._camera_runtime_available = success
            with self._perf_lock:
                self._perf["open_capture"]["last_check_ms"] = self._elapsed_ms(started_at)
                self._perf["open_capture"]["last_check_mode"] = "quick" if quick else "full"
            return success
        except Exception as e:
            self._last_camera_error = str(e)
            logger.error(f"摄像头检查失败: {e}")
            if self.cap:
                self.cap.release()
                self.cap = None
            if previous_cap is not None:
                self.cap = previous_cap
            with self._perf_lock:
                self._perf["open_capture"]["last_check_ms"] = self._elapsed_ms(started_at)
                self._perf["open_capture"]["last_check_mode"] = "quick" if quick else "full"
            return False

    def start(self):
        """启动检测线程"""
        started_at = time.perf_counter()
        with self._lock:
            if self._running:
                logger.warning("检测器已在运行")
                return

            self._running = True
            self._thread = threading.Thread(target=self._detection_loop, daemon=True, name="DetectorThread")
            self._thread.start()
            logger.info("检测器已启动")

        with self._perf_lock:
            self._perf["lifecycle"]["start_count"] += 1
            self._perf["lifecycle"]["last_start_ms"] = self._elapsed_ms(started_at)

    def stop(self):
        """停止检测线程"""
        started_at = time.perf_counter()
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

        with self._perf_lock:
            self._perf["lifecycle"]["stop_count"] += 1
            self._perf["lifecycle"]["last_stop_ms"] = self._elapsed_ms(started_at)

    def _detection_loop(self):
        """检测主循环"""
        logger.info("检测循环启动")

        if not self._open_capture():
            logger.error(self._last_camera_error or f"无法打开摄像头 (设备索引: {self.device_index})")
            self._running = False
            return

        while self._running:
            loop_started_at = time.perf_counter()
            try:
                if not self.cap:
                    if not self._open_capture():
                        time.sleep(1.0)
                        continue

                read_started_at = time.perf_counter()
                ret, frame = self.cap.read()
                read_elapsed_ms = self._elapsed_ms(read_started_at)
                if not ret or frame is None:
                    self._total_read_failures += 1
                    self._consecutive_read_failures += 1
                    self._update_perf(
                        "frame",
                        last_read_ms=read_elapsed_ms,
                        last_total_loop_ms=self._elapsed_ms(loop_started_at),
                        last_timestamp=datetime.now().isoformat(),
                    )

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

                process_started_at = time.perf_counter()
                result = self._process_frame(frame)
                process_elapsed_ms = self._elapsed_ms(process_started_at)

                with self._result_lock:
                    self._latest_result = result

                callback_elapsed_ms = 0.0
                if self.on_detection:
                    callback_started_at = time.perf_counter()
                    try:
                        self.on_detection(result)
                    except Exception as e:
                        logger.error(f"检测回调异常: {e}")
                    callback_elapsed_ms = self._elapsed_ms(callback_started_at)

                queue_elapsed_ms = 0.0
                queue_started_at = time.perf_counter()
                try:
                    self._result_queue.put_nowait(result)
                except queue.Full:
                    pass
                queue_elapsed_ms = self._elapsed_ms(queue_started_at)

                self._update_perf(
                    "frame",
                    last_read_ms=read_elapsed_ms,
                    last_process_ms=process_elapsed_ms,
                    last_total_loop_ms=self._elapsed_ms(loop_started_at),
                    last_queue_put_ms=queue_elapsed_ms,
                    last_callback_ms=callback_elapsed_ms,
                    last_timestamp=result.timestamp.isoformat(),
                    last_person_detected=result.person_detected,
                    last_confidence=result.confidence,
                )

            except Exception as e:
                self._last_camera_error = str(e)
                logger.error(f"检测循环异常: {e}")
                self._update_perf(
                    "frame",
                    last_total_loop_ms=self._elapsed_ms(loop_started_at),
                    last_timestamp=datetime.now().isoformat(),
                )
                time.sleep(0.1)

        logger.info("检测循环结束")

    def _process_frame(self, frame: np.ndarray) -> DetectionResult:
        """处理单帧图像"""
        started_at = time.perf_counter()
        color_started_at = time.perf_counter()
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        color_ms = self._elapsed_ms(color_started_at)

        pose_started_at = time.perf_counter()
        results = self.pose.process(rgb_frame)
        pose_ms = self._elapsed_ms(pose_started_at)

        person_detected = False
        confidence = 0.0
        bbox = None

        # 渲染帧用于调试
        render_started_at = time.perf_counter()
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
                cv2.putText(
                    debug_frame,
                    f"DETECTED: {confidence:.2f}",
                    (x1, y1 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 0, 255),
                    2,
                )
            else:
                # 检测到但不在风险区域
                x1, y1 = int(min_x * w), int(min_y * h)
                x2, y2 = int((min_x + bbox_w) * w), int((min_y + bbox_h) * h)
                cv2.rectangle(debug_frame, (x1, y1), (x2, y2), (0, 255, 0), 1)

        # 绘制风险区域边界
        rz = self.risk_zone
        rx1, ry1 = int(rz["x"] * w), int(rz["y"] * h)
        rx2, ry2 = int((rz["x"] + rz["width"]) * w), int((rz["y"] + rz["height"]) * h)
        cv2.rectangle(debug_frame, (rx1, ry1), (rx2, ry2), (255, 0, 0), 1)
        cv2.putText(debug_frame, "RISK ZONE", (rx1, ry1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 0, 0), 1)
        render_ms = self._elapsed_ms(render_started_at)

        perf = {
            "color_convert_ms": color_ms,
            "pose_process_ms": pose_ms,
            "render_ms": render_ms,
            "total_ms": self._elapsed_ms(started_at),
        }

        return DetectionResult(
            person_detected=person_detected,
            confidence=confidence,
            bbox=bbox,
            frame=debug_frame,
            perf=perf,
        )

    def _is_in_risk_zone(self, x: float, y: float, w: float, h: float) -> bool:
        """检查目标是否在风险区域内"""
        rz = self.risk_zone

        # 计算重叠区域
        overlap_x1 = max(x, rz["x"])
        overlap_y1 = max(y, rz["y"])
        overlap_x2 = min(x + w, rz["x"] + rz["width"])
        overlap_y2 = min(y + h, rz["y"] + rz["height"])

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

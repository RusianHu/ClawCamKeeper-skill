"""
核心监控引擎
整合状态机、检测链路、动作链路，实现完整的双阶段报警闭环
"""

from copy import deepcopy
import threading
import time
from datetime import datetime
from typing import Callable, List, Optional

from loguru import logger

from .action import ActionChain
from .config_manager import analyze_config_changes, normalize_config
from .detector import DetectionResult, Detector
from .state import AlertPhase, ArmState, SystemState
from .statemachine import StateMachine


class EventRecord:
    """事件记录"""

    def __init__(self, event_type: str, message: str, data: dict = None):
        self.timestamp = datetime.now()
        self.event_type = event_type
        self.message = message
        self.data = data or {}

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "event_type": self.event_type,
            "message": self.message,
            "data": self.data,
        }


class MonitorEngine:
    """
    监控引擎
    负责整合所有组件，实现完整的监控闭环
    """

    def __init__(self, config: dict):
        self.config = normalize_config(config)
        self._config_lock = threading.RLock()
        self.state_machine = StateMachine()
        self.detector: Optional[Detector] = None
        self.action_chain = ActionChain(self.config)

        # 报警计数
        self._pre_alert_count = 0
        self._full_alert_count = 0
        self._alert_lock = threading.Lock()

        # 检测配置
        self.pre_alert_frames = 10
        self.full_alert_frames = 30
        self._apply_runtime_config()

        # 事件记录
        self._events: List[EventRecord] = []
        self._events_lock = threading.Lock()
        self._max_events = 100  # 最多保留 100 条事件

        # 状态变化回调
        self._on_state_change: Optional[Callable] = None

        # 运行状态
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._camera_fault_reported = False
        self._last_camera_probe_status = {
            "runtime_available": False,
            "last_error": "尚未执行摄像头探测",
            "consecutive_read_failures": 0,
            "total_read_failures": 0,
            "backend": None,
            "last_success_time": None,
            "source": "uninitialized",
        }

        # 注册状态变化监听
        self.state_machine.add_listener(self._on_state_changed)

    def _apply_runtime_config(self):
        """把配置同步到引擎级运行时字段"""
        det_config = self.config.get("detection", {})
        self.pre_alert_frames = det_config.get("pre_alert_frames", 10)
        self.full_alert_frames = det_config.get("full_alert_frames", 30)

    def _probe_camera_availability(self, source: str = "runtime_probe") -> tuple[bool, dict]:
        """探测摄像头可用性；检测器运行中时直接复用运行时状态"""
        if self.detector:
            camera_status = {
                **self.detector.get_camera_status(),
                "source": "live_detector",
            }
            return camera_status.get("runtime_available", False), camera_status

        test_detector = Detector(self.config)
        camera_available = test_detector.is_camera_available()
        camera_status = {
            **test_detector.get_camera_status(),
            "source": source,
        }
        return camera_available, camera_status

    def _refresh_component_availability(self, camera_source: str = "runtime_refresh") -> dict:
        """刷新摄像头、动作链路与安全窗口可用性"""
        camera_available, camera_status = self._probe_camera_availability(camera_source)
        self._last_camera_probe_status = camera_status

        action_chain_available = self.action_chain.is_available()
        safe_window_available = (
            self.action_chain.check_safe_window_available()
            if action_chain_available
            else False
        )

        self.state_machine.update_availability(
            camera=camera_available,
            action_chain=action_chain_available,
            safe_window=safe_window_available,
        )

        return {
            "camera_available": camera_available,
            "camera_status": camera_status,
            "action_chain_available": action_chain_available,
            "safe_window_available": safe_window_available,
        }

    def _add_event(self, event_type: str, message: str, data: dict = None):
        """添加事件记录"""
        event = EventRecord(event_type, message, data)
        with self._events_lock:
            self._events.append(event)
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events :]

    def get_events(self, limit: int = 20) -> List[dict]:
        """获取最近的事件记录"""
        with self._events_lock:
            events = self._events[-limit:]
            return [e.to_dict() for e in reversed(events)]

    def set_on_state_change(self, callback: Callable[[SystemState], None]):
        """设置状态变化回调"""
        self._on_state_change = callback

    def _on_state_changed(self, state: SystemState):
        """状态变化时的处理"""
        if self._on_state_change:
            try:
                self._on_state_change(state)
            except Exception as e:
                logger.error(f"状态变化回调异常: {e}")

    def initialize(self) -> tuple[bool, str]:
        """
        初始化引擎
        Phase 2 增强：更详细的健康检查
        Returns: (success, message)
        """
        availability = self._refresh_component_availability(camera_source="initialize_probe")
        camera_available = availability["camera_available"]
        action_available = availability["action_chain_available"]
        safe_window_available = availability["safe_window_available"]

        issues = []
        warnings = []

        if not camera_available:
            issues.append("摄像头不可用，无法武装")
        if not action_available:
            issues.append("Windows 窗口控制不可用（pywin32 未安装）")

        safe_window_status = self.action_chain.get_safe_window_status()
        primary_ok = safe_window_status.get(self.action_chain.primary_safe_app, False)
        backup_ok = safe_window_status.get(self.action_chain.backup_safe_app, False)

        if not primary_ok and not backup_ok:
            if not safe_window_available:
                issues.append(
                    f"主备安全窗口均不可用（主: {self.action_chain.primary_safe_app}, 备: {self.action_chain.backup_safe_app}）"
                )
            else:
                warnings.append("安全窗口未运行，但可在需要时启动")
        elif not primary_ok:
            warnings.append(f"主安全窗口不可用，将使用备选: {self.action_chain.backup_safe_app}")

        if issues:
            return False, f"初始化失败: {', '.join(issues)}"

        self._add_event(
            "init",
            "引擎初始化完成",
            {
                "camera": camera_available,
                "action_chain": action_available,
                "safe_window_primary": primary_ok,
                "safe_window_backup": backup_ok,
                "warnings": warnings,
            },
        )

        msg = "引擎初始化完成"
        if warnings:
            msg += f" (警告: {', '.join(warnings)})"

        return True, msg

    def arm(self) -> tuple[bool, str]:
        """武装系统"""
        availability = self._refresh_component_availability(camera_source="arm_probe")
        state = self.state_machine.state
        action_chain_available = availability["action_chain_available"]
        safe_window_available = availability["safe_window_available"]

        if not state.camera_available:
            return False, "摄像头不可用，无法武装"
        if not action_chain_available:
            return False, "动作链路不可用，无法武装"
        if not safe_window_available:
            return False, self.action_chain.last_error or "安全窗口不可用，无法武装"

        success, msg = self.state_machine.arm()
        if success:
            self._add_event("arm", "系统已武装")
            self._start_detection()

        return success, msg

    def disarm(self) -> tuple[bool, str]:
        """解除武装"""
        success, msg = self.state_machine.disarm()
        if success:
            self._add_event("disarm", "系统已解除武装")
            self._stop_detection()

        return success, msg

    def recover(self) -> tuple[bool, str]:
        """手动恢复"""
        success, msg = self.state_machine.recover()
        if success:
            self._add_event("recover", "用户手动恢复系统")
            self._start_detection()

        return success, msg

    def _start_detection(self):
        """启动检测"""
        if self._running:
            return

        self._running = True
        self._pre_alert_count = 0
        self._full_alert_count = 0
        self._camera_fault_reported = False

        self.detector = Detector(self.config, on_detection=self._on_detection)
        self.detector.start()

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="MonitorThread",
        )
        self._monitor_thread.start()

        logger.info("检测已启动")

    def _stop_detection(self):
        """停止检测"""
        self._running = False

        current_thread = threading.current_thread()
        monitor_thread = self._monitor_thread

        if self.detector:
            self._last_camera_probe_status = {
                **self.detector.get_camera_status(),
                "source": "last_runtime",
            }
            self.detector.stop()
            self.detector = None

        if (
            monitor_thread
            and monitor_thread.is_alive()
            and monitor_thread is not current_thread
        ):
            monitor_thread.join(timeout=2)

        self._monitor_thread = None
        logger.info("检测已停止")

    def _on_detection(self, result: DetectionResult):
        """检测结果回调"""
        # 这个回调在检测线程中调用，只做简单记录
        pass

    def _sync_detector_health(self):
        """同步检测器运行时健康状态到状态机"""
        if not self.detector:
            return

        camera_status = self.detector.get_camera_status()
        self._last_camera_probe_status = {
            **camera_status,
            "source": "live_detector",
        }
        runtime_available = camera_status.get("runtime_available", False)
        last_error = camera_status.get("last_error")
        state = self.state_machine.state

        if runtime_available:
            if not state.camera_available:
                self.state_machine.update_availability(camera=True)
                self._add_event("camera_recovered", "摄像头检测链路已恢复", camera_status)
                logger.info("摄像头检测链路已恢复")
            self._camera_fault_reported = False
            return

        if last_error:
            if state.camera_available:
                self.state_machine.update_availability(camera=False)
            if not self._camera_fault_reported:
                self._add_event("camera_failure", f"摄像头检测链路不可用: {last_error}", camera_status)
                logger.error(f"摄像头检测链路不可用: {last_error}")
                self._camera_fault_reported = True

    def _monitor_loop(self):
        """监控循环（独立线程）"""
        logger.info("监控循环启动")

        while self._running:
            try:
                state = self.state_machine.state

                if state.arm_state != ArmState.ARMED:
                    time.sleep(0.1)
                    continue

                if self.detector:
                    self._sync_detector_health()
                    camera_status = self.detector.get_camera_status()
                    if not camera_status.get("runtime_available", False):
                        time.sleep(0.1)
                        continue

                    result = self.detector.latest_result
                    if result and result.person_detected:
                        self._handle_person_detected()
                    elif result and not result.person_detected:
                        with self._alert_lock:
                            if self._pre_alert_count > 0:
                                self._pre_alert_count = 0
                                self.state_machine.reset_alert()
                                self._add_event("reset", "人体离开，重置报警计数")

                time.sleep(1.0 / 30)

            except Exception as e:
                logger.error(f"监控循环异常: {e}")
                time.sleep(1)

        logger.info("监控循环结束")

    def _handle_person_detected(self):
        """处理检测到人体"""
        with self._alert_lock:
            state = self.state_machine.state

            if state.alert_phase == AlertPhase.NONE:
                self._pre_alert_count += 1
                if self._pre_alert_count >= self.pre_alert_frames:
                    self.state_machine.trigger_pre_alert()
                    self._add_event("pre_alert", "进入无感预备状态")

            elif state.alert_phase == AlertPhase.PRE_ALERT:
                self._full_alert_count += 1
                if self._full_alert_count >= self.full_alert_frames:
                    self._execute_full_alert()

            elif state.alert_phase == AlertPhase.FULL_ALERT:
                pass

    def _execute_full_alert(self):
        """执行完全报警动作链"""
        self.state_machine.trigger_full_alert()
        self._add_event("full_alert", "触发完全报警，执行安全动作")

        result = self.action_chain.execute_full_alert()

        if result["success"]:
            self._add_event("action_success", "报警动作链执行成功", result)
            self.state_machine.enter_danger_lock()
            self._add_event("danger_lock", "进入危险锁定状态")
            self._stop_detection()
        else:
            self._add_event("action_failure", "报警动作链执行失败", result)
            logger.error(f"报警动作链执行失败: {result['errors']}")
            self.state_machine.enter_danger_lock()
            self._add_event("danger_lock", "动作失败，仍进入危险锁定状态")
            self._stop_detection()

    def get_config(self) -> dict:
        """获取当前配置快照"""
        with self._config_lock:
            return deepcopy(self.config)

    def reload_config(self, new_config: dict) -> dict:
        """对运行中引擎应用新配置，并返回热加载结果"""
        normalized_config = normalize_config(new_config)

        with self._config_lock:
            previous_config = deepcopy(self.config)
            change_summary = analyze_config_changes(previous_config, normalized_config)
            changed_keys = change_summary["changed_keys"]

            if not changed_keys:
                return {
                    "success": True,
                    "message": "配置无变化",
                    "changed_keys": [],
                    "effective_immediately": [],
                    "effective_on_next_detection_start": [],
                    "detector_restart_required": [],
                    "detector_restarted": False,
                    "service_restart_required": [],
                    "unknown": [],
                }

            armed_state = self.state_machine.state.arm_state
            should_restart_detector = bool(change_summary["detector_restart_required"]) and armed_state == ArmState.ARMED

            if should_restart_detector and self._running:
                self._stop_detection()

            self.config = normalized_config
            self._apply_runtime_config()
            self.action_chain = ActionChain(self.config)

            detector_restarted = False
            if should_restart_detector:
                self._start_detection()
                detector_restarted = True

            availability = self._refresh_component_availability(camera_source="reload_probe")

            effective_immediately = list(change_summary["immediate"])
            effective_on_next_detection_start: list[str] = []

            if change_summary["detector_restart_required"]:
                if detector_restarted:
                    effective_immediately.extend(change_summary["detector_restart_required"])
                else:
                    effective_on_next_detection_start.extend(change_summary["detector_restart_required"])

            message_parts = [f"已应用 {len(changed_keys)} 项配置变更"]
            if detector_restarted:
                message_parts.append("检测链路已重建")
            elif effective_on_next_detection_start:
                message_parts.append("部分配置将在下次启动检测链路时生效")
            if change_summary["service_restart_required"]:
                message_parts.append("WebUI/日志相关变更需重启服务后完全生效")

            result = {
                "success": True,
                "message": "；".join(message_parts),
                "changed_keys": changed_keys,
                "effective_immediately": sorted(effective_immediately),
                "effective_on_next_detection_start": sorted(effective_on_next_detection_start),
                "detector_restart_required": change_summary["detector_restart_required"],
                "detector_restarted": detector_restarted,
                "service_restart_required": change_summary["service_restart_required"],
                "unknown": change_summary["unknown"],
                "availability": availability,
            }

            self._add_event(
                "config_reload",
                result["message"],
                {
                    "changed_keys": changed_keys,
                    "detector_restarted": detector_restarted,
                    "service_restart_required": change_summary["service_restart_required"],
                },
            )
            logger.info(result["message"])
            return result

    def get_status(self) -> dict:
        """获取当前状态（用于 CLI/WebUI）"""
        state = self.state_machine.state
        status = state.to_dict()
        action_chain_available = self.action_chain.is_available()
        safe_window_available = (
            self.action_chain.check_safe_window_available()
            if action_chain_available
            else False
        )

        if self.detector:
            latest = self.detector.latest_result
            if latest:
                status["latest_detection"] = latest.to_dict()
            status["camera_runtime_status"] = {
                **self.detector.get_camera_status(),
                "source": "live_detector",
            }
        else:
            status["camera_runtime_status"] = dict(self._last_camera_probe_status)

        status["camera_available"] = (
            status["camera_available"]
            and status["camera_runtime_status"].get("runtime_available", False)
        )

        status["action_chain_available"] = action_chain_available
        status["safe_window_available"] = safe_window_available
        status["is_protecting"] = (
            status.get("arm_state") == ArmState.ARMED.value
            and status.get("camera_available", False)
            and action_chain_available
            and safe_window_available
        )

        status["last_action_error"] = self.action_chain.last_error
        status["safe_window_status"] = self.action_chain.get_safe_window_status()
        status["primary_safe_app"] = self.action_chain.primary_safe_app
        status["backup_safe_app"] = self.action_chain.backup_safe_app
        status["risk_apps"] = self.action_chain.get_risk_apps()

        return status

    def doctor(self) -> dict:
        """
        健康检查
        Phase 2 增强：更详细的组件状态和错误历史
        """
        state = self.state_machine.state
        action_chain_available = self.action_chain.is_available()
        safe_window_available = (
            self.action_chain.check_safe_window_available()
            if action_chain_available
            else False
        )

        action_health = self.action_chain.check_action_chain_health()

        camera_runtime_status = (
            {
                **self.detector.get_camera_status(),
                "source": "live_detector",
            }
            if self.detector
            else dict(self._last_camera_probe_status)
        )

        report = {
            "healthy": True,
            "issues": [],
            "warnings": [],
            "components": {
                "camera": {
                    "available": state.camera_available and camera_runtime_status.get("runtime_available", False),
                    "status": "✅ 可用" if state.camera_available and camera_runtime_status.get("runtime_available", False) else "❌ 不可用",
                    "runtime": camera_runtime_status,
                },
                "safe_window": {
                    "available": safe_window_available,
                    "primary": self.action_chain.primary_safe_app,
                    "backup": self.action_chain.backup_safe_app,
                    "status": action_health["safe_windows"],
                },
                "action_chain": {
                    "available": action_chain_available,
                    "status": "✅ 可用" if action_chain_available else "❌ 不可用",
                },
            },
            "state": state.to_dict(),
            "risk_apps": self.action_chain.get_risk_apps(),
            "action_chain_errors": action_health.get("recent_errors", []),
        }

        if not state.camera_available:
            report["healthy"] = False
            report["issues"].append("摄像头不可用，无法进行人体检测")
        elif not camera_runtime_status.get("runtime_available", False):
            report["healthy"] = False
            report["issues"].append(
                f"摄像头运行时不可用: {camera_runtime_status.get('last_error') or '检测线程未获得有效帧'}"
            )

        if not action_chain_available:
            report["healthy"] = False
            report["issues"].append("Windows 窗口控制不可用（pywin32 未安装），无法执行窗口操作")

        primary_ok = action_health["safe_windows"].get(self.action_chain.primary_safe_app, False)
        backup_ok = action_health["safe_windows"].get(self.action_chain.backup_safe_app, False)

        if not primary_ok and not backup_ok:
            if safe_window_available:
                report["warnings"].append("安全窗口未运行，但可在报警时启动")
            else:
                report["healthy"] = False
                report["issues"].append(
                    self.action_chain.last_error
                    or f"主备安全窗口均不可用（主: {self.action_chain.primary_safe_app}, 备: {self.action_chain.backup_safe_app}）"
                )
        elif not primary_ok:
            report["warnings"].append(f"主安全窗口不可用，将使用备选: {self.action_chain.backup_safe_app}")

        if action_health.get("recent_errors"):
            report["warnings"].append(f"动作链路最近发生 {len(action_health['recent_errors'])} 次错误")

        return report

    def shutdown(self):
        """关闭引擎"""
        self._stop_detection()
        logger.info("引擎已关闭")

"""
核心监控引擎
整合状态机、检测链路、动作链路，实现完整的双阶段报警闭环
"""

from copy import deepcopy
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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

    _RUNTIME_DIR = Path(tempfile.gettempdir()) / "clawcamkeeper-openclaw"
    _NOTIFICATION_CONTEXT_FILE = _RUNTIME_DIR / "notification-context.json"

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
        self._full_alert_cooldown_until = 0.0

        # 检测配置
        self.pre_alert_frames = 10
        self.full_alert_frames = 30
        self._apply_runtime_config()

        # 事件记录
        self._events: List[EventRecord] = []
        self._events_lock = threading.Lock()
        self._max_events = 100  # 最多保留 100 条事件

        # 轻量通知队列（供 openclaw / WebUI 轮询）
        self._notifications: List[dict[str, Any]] = []
        self._notifications_lock = threading.Lock()
        self._max_notifications = 50
        self._notification_seq = 0
        self._notification_dedupe_window_s = 8.0
        self._notification_last_sent_at: dict[str, float] = {}
        self._notification_dispatch_lock = threading.Lock()
        self._notification_context = {
            "session_key": None,
            "session_label": None,
            "channel": None,
            "target": None,
            "account": None,
            "source": None,
            "registered_at": None,
            "expires_at": None,
        }
        self._restore_notification_context()
        self._last_notification_dispatch_result: dict[str, Any] = {
            "ok": None,
            "status": "idle",
            "message": "尚未执行主动通知回推",
            "timestamp": None,
        }

        # 最近一次关键动作结果（供轻量可观察性面板展示）
        self._last_action_result: dict[str, Any] = {
            "event_type": None,
            "title": "尚无关键动作",
            "success": None,
            "message": "系统刚启动，还没有可展示的关键动作结果",
            "timestamp": None,
            "details": {},
        }
        self._timeline_limit = 8

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
        self._last_camera_probe_cache = {
            "available": None,
            "status": None,
            "source": None,
            "quick": None,
            "captured_at": 0.0,
            "ttl_s": 0.0,
        }

        # 注册状态变化监听
        self.state_machine.add_listener(self._on_state_changed)

        # 性能观测
        self._perf_lock = threading.Lock()
        self._perf = {
            "engine": {
                "last_initialize_ms": 0.0,
                "last_arm_ms": 0.0,
                "last_disarm_ms": 0.0,
                "last_recover_ms": 0.0,
                "last_status_ms": 0.0,
                "last_doctor_ms": 0.0,
                "last_events_ms": 0.0,
                "last_reload_config_ms": 0.0,
                "last_action_test_ms": 0.0,
                "last_availability_refresh_ms": 0.0,
                "last_full_alert_ms": 0.0,
                "last_availability_source": None,
                "last_status_timestamp": None,
                "last_doctor_timestamp": None,
            },
            "monitor": {
                "iterations": 0,
                "last_loop_ms": 0.0,
                "last_state": None,
            },
        }

    def _apply_runtime_config(self):
        """把配置同步到引擎级运行时字段"""
        det_config = self.config.get("detection", {})
        self.pre_alert_frames = det_config.get("pre_alert_frames", 10)
        self.full_alert_frames = det_config.get("full_alert_frames", 30)

        openclaw_cfg = self.config.get("openclaw", {}) if isinstance(self.config, dict) else {}
        notification_cfg = openclaw_cfg.get("notifications", {}) if isinstance(openclaw_cfg, dict) else {}
        self._notification_enabled = bool(notification_cfg.get("enabled", False))
        self._notification_command = str(notification_cfg.get("command", "openclaw")).strip() or "openclaw"
        resolved_command = shutil.which(self._notification_command)
        self._notification_command_resolved = resolved_command or self._notification_command
        self._notification_timeout_seconds = int(notification_cfg.get("timeout_seconds", 8))
        self._notification_context_ttl_seconds = int(notification_cfg.get("context_ttl_seconds", 900))
        self._notification_message_prefix = str(notification_cfg.get("message_prefix", "[ClawCamKeeper]")).strip() or "[ClawCamKeeper]"
        self._notification_routes = deepcopy(notification_cfg.get("routes", {})) if isinstance(notification_cfg.get("routes"), dict) else {}
        self._notification_fallback = deepcopy(notification_cfg.get("fallback", {})) if isinstance(notification_cfg.get("fallback"), dict) else {}

    def _elapsed_ms(self, start: float, end: Optional[float] = None) -> float:
        """将 perf_counter 时间差换算为毫秒"""
        if end is None:
            end = time.perf_counter()
        return round((end - start) * 1000, 2)

    def _update_engine_perf(self, **kwargs):
        """更新引擎级性能信息"""
        with self._perf_lock:
            self._perf["engine"].update(kwargs)

    def _update_monitor_perf(self, **kwargs):
        """更新监控循环性能信息"""
        with self._perf_lock:
            self._perf["monitor"].update(kwargs)

    def get_perf_snapshot(self) -> dict:
        """获取当前性能快照"""
        with self._perf_lock:
            perf = deepcopy(self._perf)

        detector_perf = {}
        if self.detector:
            detector_perf = self.detector.get_camera_status().get("perf", {})
        elif isinstance(self._last_camera_probe_status, dict):
            detector_perf = self._last_camera_probe_status.get("perf", {})

        perf["detector"] = detector_perf or {}
        perf["action_chain"] = {
            "last_switch": self.action_chain.get_last_switch_diagnostics(),
            "last_minimize": self.action_chain.get_last_minimize_diagnostics(),
        }
        return perf

    def _probe_camera_availability(self, source: str = "runtime_probe", quick: bool = False) -> tuple[bool, dict]:
        """探测摄像头可用性；检测器运行中时直接复用运行时状态"""
        if self.detector:
            camera_status = {
                **self.detector.get_camera_status(),
                "source": "live_detector",
            }
            return camera_status.get("runtime_available", False), camera_status

        camera_config = self.config.get("camera", {}) if isinstance(self.config, dict) else {}
        quick_probe_cache_ttl_s = float(camera_config.get("quick_probe_cache_ttl_s", 10.0))
        now = time.perf_counter()
        cache = self._last_camera_probe_cache
        if (
            quick
            and quick_probe_cache_ttl_s > 0
            and cache.get("quick") is True
            and cache.get("available") is False
            and isinstance(cache.get("status"), dict)
            and (now - float(cache.get("captured_at") or 0.0)) <= quick_probe_cache_ttl_s
        ):
            cached_status = {
                **cache["status"],
                "source": source,
                "probe_mode": "quick",
                "cached": True,
                "cache_age_ms": round((now - float(cache.get("captured_at") or 0.0)) * 1000, 2),
                "cache_ttl_s": quick_probe_cache_ttl_s,
                "cache_original_source": cache.get("source"),
            }
            return False, cached_status

        test_detector = Detector(self.config)
        camera_available = test_detector.is_camera_available(quick=quick)
        camera_status = {
            **test_detector.get_camera_status(),
            "source": source,
            "probe_mode": "quick" if quick else "full",
            "cached": False,
        }
        if quick and quick_probe_cache_ttl_s > 0:
            self._last_camera_probe_cache = {
                "available": camera_available,
                "status": dict(camera_status),
                "source": source,
                "quick": True,
                "captured_at": now,
                "ttl_s": quick_probe_cache_ttl_s,
            }
        elif not quick:
            self._last_camera_probe_cache = {
                "available": None,
                "status": None,
                "source": None,
                "quick": None,
                "captured_at": 0.0,
                "ttl_s": 0.0,
            }
        return camera_available, camera_status

    def _refresh_component_availability(self, camera_source: str = "runtime_refresh", quick_camera_probe: bool = False) -> dict:
        """刷新摄像头、动作链路与安全窗口可用性"""
        started_at = time.perf_counter()

        camera_started_at = time.perf_counter()
        camera_available, camera_status = self._probe_camera_availability(camera_source, quick=quick_camera_probe)
        camera_elapsed_ms = self._elapsed_ms(camera_started_at)
        self._last_camera_probe_status = camera_status

        action_started_at = time.perf_counter()
        action_chain_available = self.action_chain.is_available()
        action_elapsed_ms = self._elapsed_ms(action_started_at)

        safe_window_started_at = time.perf_counter()
        safe_window_available = (
            self.action_chain.check_safe_window_available()
            if action_chain_available
            else False
        )
        safe_window_elapsed_ms = self._elapsed_ms(safe_window_started_at)

        state_update_started_at = time.perf_counter()
        self.state_machine.update_availability(
            camera=camera_available,
            action_chain=action_chain_available,
            safe_window=safe_window_available,
        )
        state_update_elapsed_ms = self._elapsed_ms(state_update_started_at)

        availability = {
            "camera_available": camera_available,
            "camera_status": camera_status,
            "action_chain_available": action_chain_available,
            "safe_window_available": safe_window_available,
            "timings": {
                "camera_ms": camera_elapsed_ms,
                "action_chain_ms": action_elapsed_ms,
                "safe_window_ms": safe_window_elapsed_ms,
                "state_update_ms": state_update_elapsed_ms,
                "total_ms": self._elapsed_ms(started_at),
            },
        }
        self._update_engine_perf(
            last_availability_refresh_ms=availability["timings"]["total_ms"],
            last_availability_source=camera_source,
        )
        return availability

    def _build_state_summary(self) -> dict[str, Any]:
        """构建轻量状态摘要，供事件/通知/UI 复用。"""
        state = self.state_machine.state
        return {
            "arm_state": state.arm_state.value,
            "alert_phase": state.alert_phase.value,
            "is_locked": state.is_locked,
            "is_protecting": bool(
                state.arm_state == ArmState.ARMED
                and state.camera_available
                and state.action_chain_available
                and state.safe_window_available
            ),
            "monitoring_active": bool(self._running and state.arm_state == ArmState.ARMED),
            "camera_available": state.camera_available,
            "safe_window_available": state.safe_window_available,
            "action_chain_available": state.action_chain_available,
            "primary_safe_app": self.action_chain.primary_safe_app,
            "backup_safe_app": self.action_chain.backup_safe_app,
            "risk_apps_count": len(self.action_chain.get_risk_apps()),
            "last_event_message": state.last_event_message,
            "last_event_time": state.last_event_time.isoformat() if state.last_event_time else None,
        }

    def _build_remote_action_matrix(self) -> dict[str, dict[str, Any]]:
        """远程动作允许矩阵：仅表达控制语义，不承载业务状态。"""
        return {
            "unarmed": {
                "allowed": ["status", "doctor", "events", "config-show", "arm", "set-safe-window"],
                "blocked": ["disarm", "recover"],
                "notes": ["未武装时允许只读查询和进入武装", "恢复仅在 danger_locked 状态显式允许"],
            },
            "armed": {
                "allowed": ["status", "doctor", "events", "config-show", "disarm", "set-safe-window", "action-test"],
                "blocked": ["arm", "recover"],
                "notes": ["已武装时允许诊断与解除武装", "不允许把 recover 当作普通解除手段"],
            },
            "danger_locked": {
                "allowed": ["status", "doctor", "events", "config-show", "set-safe-window", "recover"],
                "blocked": ["arm", "disarm"],
                "notes": ["危险锁定后保持静默锁定", "恢复必须显式调用 recover"],
            },
        }

    def _build_recent_timeline(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """返回最小必要时间线，避免 UI 退化成重型取证视图。"""
        effective_limit = limit or self._timeline_limit
        tone_map = {
            "arm": "ok",
            "disarm": "muted",
            "recover": "ok",
            "pre_alert": "warn",
            "full_alert": "danger",
            "action_success": "ok",
            "action_failure": "danger",
            "danger_lock": "danger",
            "action_test": "info",
            "action_test_failure": "danger",
            "camera_failure": "danger",
            "camera_recovered": "ok",
            "config_reload": "info",
        }
        with self._events_lock:
            events = list(self._events[-effective_limit:])

        return [
            {
                "timestamp": event.timestamp.isoformat(),
                "event_type": event.event_type,
                "message": event.message,
                "tone": tone_map.get(event.event_type, "info"),
                "summary": {
                    "safe_window_used": event.data.get("safe_window_used") if isinstance(event.data, dict) else None,
                    "risk_apps_minimized": event.data.get("risk_apps_minimized") if isinstance(event.data, dict) else None,
                    "timings": event.data.get("timings") if isinstance(event.data, dict) else None,
                },
            }
            for event in reversed(events)
        ]

    def _update_last_action_result(self, event_type: str, message: str, data: Optional[dict[str, Any]] = None) -> None:
        """记录最近一次关键动作结果，供状态面板直接展示。"""
        action_titles = {
            "arm": "最近动作：武装",
            "disarm": "最近动作：解除武装",
            "recover": "最近动作：手动恢复",
            "action_success": "最近动作：报警动作链成功",
            "action_failure": "最近动作：报警动作链失败",
            "action_test": "最近动作：动作链测试",
            "action_test_failure": "最近动作：动作链测试失败",
            "config_reload": "最近动作：配置热加载",
        }
        if event_type not in action_titles:
            return

        details = deepcopy(data or {})
        success = None
        if event_type in {"arm", "disarm", "recover", "action_success", "action_test", "config_reload"}:
            success = True
        elif event_type in {"action_failure", "action_test_failure"}:
            success = False

        self._last_action_result = {
            "event_type": event_type,
            "title": action_titles[event_type],
            "success": success,
            "message": message,
            "timestamp": datetime.now().isoformat(),
            "details": details,
        }

    @staticmethod
    def _normalize_context_value(value: Any) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @classmethod
    def _notification_context_file(cls) -> Path:
        cls._RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        return cls._NOTIFICATION_CONTEXT_FILE

    def _persist_notification_context(self) -> None:
        try:
            path = self._notification_context_file()
            with self._notification_dispatch_lock:
                payload = deepcopy(self._notification_context)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"持久化通知上下文失败: {exc}")

    def _restore_notification_context(self) -> None:
        try:
            path = self._notification_context_file()
            if not path.exists():
                return
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return

            restored = {
                "session_key": self._normalize_context_value(payload.get("session_key") or payload.get("sessionKey")),
                "session_label": self._normalize_context_value(payload.get("session_label") or payload.get("sessionLabel")),
                "channel": self._normalize_context_value(payload.get("channel")),
                "target": self._normalize_context_value(payload.get("target")),
                "account": self._normalize_context_value(payload.get("account")),
                "source": self._normalize_context_value(payload.get("source")) or "restore",
                "registered_at": self._normalize_context_value(payload.get("registered_at")),
                "expires_at": payload.get("expires_at"),
            }
            if restored["channel"]:
                restored["channel"] = restored["channel"].lower()
            expires_at = restored.get("expires_at")
            if expires_at is not None:
                try:
                    expires_at = float(expires_at)
                except (TypeError, ValueError):
                    expires_at = None
                restored["expires_at"] = expires_at

            self._notification_context = restored
        except Exception as exc:
            logger.warning(f"恢复通知上下文失败: {exc}")

    def _clear_persisted_notification_context(self) -> None:
        try:
            path = self._notification_context_file()
            if path.exists():
                path.unlink()
        except Exception as exc:
            logger.warning(f"清理通知上下文持久化文件失败: {exc}")

    def _get_notification_context_snapshot(self) -> dict[str, Any]:
        with self._notification_dispatch_lock:
            context = deepcopy(self._notification_context)

        expires_at = context.get("expires_at")
        if expires_at is not None:
            expires_in_s = max(0, int(expires_at - time.time()))
            context["active"] = expires_in_s > 0
            context["expires_in_s"] = expires_in_s
        else:
            context["active"] = False
            context["expires_in_s"] = None
        return context

    def _get_active_notification_context(self) -> Optional[dict[str, Any]]:
        snapshot = self._get_notification_context_snapshot()
        return snapshot if snapshot.get("active") else None

    def register_openclaw_notification_context(self, context: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        payload = deepcopy(context or {})
        now = time.time()
        normalized = {
            "session_key": self._normalize_context_value(payload.get("session_key") or payload.get("sessionKey")),
            "session_label": self._normalize_context_value(payload.get("session_label") or payload.get("sessionLabel")),
            "channel": self._normalize_context_value(payload.get("channel")),
            "target": self._normalize_context_value(payload.get("target")),
            "account": self._normalize_context_value(payload.get("account")),
            "source": self._normalize_context_value(payload.get("source")) or "api",
            "registered_at": datetime.now().isoformat(),
            "expires_at": now + self._notification_context_ttl_seconds,
        }
        if normalized["channel"]:
            normalized["channel"] = normalized["channel"].lower()

        with self._notification_dispatch_lock:
            self._notification_context = normalized
        self._persist_notification_context()

        return self._get_notification_context_snapshot()

    def clear_openclaw_notification_context(self, source: str = "api") -> dict[str, Any]:
        with self._notification_dispatch_lock:
            self._notification_context = {
                "session_key": None,
                "session_label": None,
                "channel": None,
                "target": None,
                "account": None,
                "source": self._normalize_context_value(source) or "api",
                "registered_at": datetime.now().isoformat(),
                "expires_at": None,
            }
        self._clear_persisted_notification_context()
        return self._get_notification_context_snapshot()

    def get_openclaw_notification_context(self) -> dict[str, Any]:
        return self._get_notification_context_snapshot()

    def get_notification_dispatch_status(self) -> dict[str, Any]:
        with self._notification_dispatch_lock:
            return deepcopy(self._last_notification_dispatch_result)

    def test_notification_dispatch(
        self,
        *,
        message: Optional[str] = None,
        severity: str = "warning",
        event_type: str = "notification_test",
    ) -> dict[str, Any]:
        state = self.state_machine.state
        notification = {
            "id": None,
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "severity": severity,
            "message": message or "这是 ClawCamKeeper 主动通知链路测试消息",
            "state_summary": {
                "arm_state": state.arm_state.value,
                "alert_phase": state.alert_phase.value,
                "is_locked": state.is_locked,
            },
            "delivery": {
                "push": True,
                "severity": severity,
                "dedupe_key": f"notification_test:{event_type}",
            },
        }
        self._dispatch_notification(notification)
        dispatch = deepcopy(notification.get("delivery", {}).get("dispatch") or self.get_notification_dispatch_status())
        result = {
            "success": bool(dispatch.get("ok")),
            "message": dispatch.get("message") or "通知链路测试完成",
            "dispatch": dispatch,
            "notification": {
                "event_type": notification["event_type"],
                "severity": notification["severity"],
                "message": notification["message"],
            },
            "timings": {
                "total_ms": 0.0,
            },
        }
        self._add_event(
            "notification_test",
            result["message"],
            {
                "notification": result["notification"],
                "dispatch": dispatch,
            },
        )
        return result

    def _resolve_notification_route(self) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        active_context = self._get_active_notification_context()
        channel = active_context.get("channel") if active_context else None
        target = active_context.get("target") if active_context else None
        account = active_context.get("account") if active_context else None
        route_source = "active_context"

        configured_route = self._notification_routes.get(channel, {}) if channel else {}
        if isinstance(configured_route, dict):
            if not target:
                target = self._normalize_context_value(configured_route.get("target"))
                if target:
                    route_source = "configured_route"
            if not account:
                account = self._normalize_context_value(configured_route.get("account"))

        if not channel:
            fallback_channel = self._normalize_context_value(self._notification_fallback.get("channel"))
            if fallback_channel:
                channel = fallback_channel.lower()
                route_source = "fallback"
            if not target:
                target = self._normalize_context_value(self._notification_fallback.get("target"))
            if not account:
                account = self._normalize_context_value(self._notification_fallback.get("account"))

        if not channel:
            return None, "当前没有活动的 OpenClaw 渠道上下文，且未配置 fallback.channel"
        if not target:
            return None, f"渠道 {channel} 缺少 target，无法主动通知"

        return {
            "channel": channel,
            "target": target,
            "account": account,
            "route_source": route_source,
            "context": active_context,
        }, None

    def _format_push_message(self, notification: dict[str, Any]) -> str:
        severity_map = {
            "critical": "严重预警",
            "error": "错误告警",
            "warning": "风险提醒",
            "info": "状态通知",
        }
        state_summary = notification.get("state_summary", {}) if isinstance(notification.get("state_summary"), dict) else {}
        arm_state = state_summary.get("arm_state") or "-"
        alert_phase = state_summary.get("alert_phase") or "-"
        is_locked = "是" if state_summary.get("is_locked") else "否"
        prefix = self._notification_message_prefix
        return (
            f"{prefix} {severity_map.get(notification.get('severity'), '通知')}\n"
            f"事件: {notification.get('event_type', '-')}\n"
            f"说明: {notification.get('message', '')}\n"
            f"状态: arm={arm_state}, alert={alert_phase}, locked={is_locked}"
        )

    def _load_openclaw_main_config(self) -> dict[str, Any]:
        candidates = [
            Path.cwd() / "openclaw.json",
            Path.home() / ".openclaw" / "openclaw.json",
        ]
        for path in candidates:
            try:
                if path.exists():
                    return json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(f"读取 OpenClaw 主配置失败 {path}: {exc}")
        return {}

    def _build_openclaw_command_invocation(self) -> list[str]:
        command = str(self._notification_command_resolved or self._notification_command).strip() or "openclaw"

        if os.name == "nt":
            node_bin = shutil.which("node")
            command_path = Path(command)
            if not command_path.is_absolute():
                resolved = shutil.which(command)
                if resolved:
                    command_path = Path(resolved)

            candidates: list[Path] = []
            if command_path.exists():
                candidates.append(command_path.parent / "node_modules" / "openclaw" / "dist" / "openclaw.mjs")
            candidates.append(Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "openclaw" / "dist" / "openclaw.mjs")

            seen: set[str] = set()
            for candidate in candidates:
                candidate_key = str(candidate)
                if candidate_key in seen:
                    continue
                seen.add(candidate_key)
                if candidate.exists() and node_bin:
                    return [node_bin, str(candidate)]

        return [command]

    def _build_openclaw_message_send_command(self, route: dict[str, Any], message_text: str) -> list[str]:
        command = [
            *self._build_openclaw_command_invocation(),
            "message",
            "send",
            "--json",
            "--channel",
            route["channel"],
            "--target",
            route["target"],
            "--message",
            message_text,
        ]
        if route.get("account"):
            command.extend(["--account", route["account"]])
        return command

    def _run_openclaw_cli_send(
        self,
        route: dict[str, Any],
        message_text: str,
        *,
        success_message: str = "主动通知已发送",
    ) -> dict[str, Any]:
        command = self._build_openclaw_message_send_command(route, message_text)

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._notification_timeout_seconds,
                check=False,
            )
            stdout = (completed.stdout or "").strip()
            stderr = (completed.stderr or "").strip()
            return {
                "ok": completed.returncode == 0,
                "status": "sent_via_openclaw_cli" if completed.returncode == 0 else "send_failed",
                "message": success_message if completed.returncode == 0 else (stderr or stdout or "OpenClaw 主动通知发送失败"),
                "timestamp": datetime.now().isoformat(),
                "channel": route["channel"],
                "target": route["target"],
                "account": route.get("account"),
                "route_source": route.get("route_source"),
                "command": command,
                "exit_code": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "primary_path": "openclaw_cli_message",
                "effective_path": "openclaw_cli_message" if completed.returncode == 0 else None,
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "status": "timeout",
                "message": f"OpenClaw CLI 补发超时（>{self._notification_timeout_seconds}s）",
                "timestamp": datetime.now().isoformat(),
                "channel": route["channel"],
                "target": route["target"],
                "account": route.get("account"),
                "route_source": route.get("route_source"),
                "command": command,
                "primary_path": "openclaw_cli_message",
                "effective_path": None,
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": "error",
                "message": f"OpenClaw CLI 补发异常: {exc}",
                "timestamp": datetime.now().isoformat(),
                "channel": route["channel"],
                "target": route["target"],
                "account": route.get("account"),
                "route_source": route.get("route_source"),
                "command": command,
                "primary_path": "openclaw_cli_message",
                "effective_path": None,
            }

    @staticmethod
    def _resolve_feishu_open_api_base(feishu_cfg: dict[str, Any]) -> str:
        domain = str((feishu_cfg or {}).get("domain") or "feishu").strip().lower()
        return "https://open.larksuite.com" if "lark" in domain else "https://open.feishu.cn"

    def _feishu_direct_send(self, route: dict[str, Any], message_text: str) -> dict[str, Any]:
        if route.get("channel") != "feishu":
            return {
                "ok": False,
                "status": "unsupported_channel",
                "message": f"直连后备暂只支持 feishu，当前为 {route.get('channel')}",
                "timestamp": datetime.now().isoformat(),
            }

        target = str(route.get("target") or "").strip()
        receive_id_type: Optional[str] = None
        receive_id: Optional[str] = None
        if target.startswith("user:"):
            receive_id_type = "open_id"
            receive_id = target.split(":", 1)[1]
        elif target.startswith("chat:"):
            receive_id_type = "chat_id"
            receive_id = target.split(":", 1)[1]
        elif target.startswith("open_id:"):
            receive_id_type = "open_id"
            receive_id = target.split(":", 1)[1]
        elif target.startswith("chat_id:"):
            receive_id_type = "chat_id"
            receive_id = target.split(":", 1)[1]
        elif target.startswith("ou_"):
            receive_id_type = "open_id"
            receive_id = target
        elif target.startswith("oc_"):
            receive_id_type = "chat_id"
            receive_id = target

        if not receive_id_type or not receive_id:
            return {
                "ok": False,
                "status": "unsupported_target",
                "message": f"Feishu 直连后备当前仅支持 open_id/chat_id 目标，当前为 {target}",
                "timestamp": datetime.now().isoformat(),
                "channel": route.get("channel"),
                "target": target,
                "account": route.get("account"),
                "route_source": route.get("route_source"),
            }

        openclaw_cfg = self._load_openclaw_main_config()
        feishu_cfg = ((openclaw_cfg.get("channels") or {}).get("feishu") or {}) if isinstance(openclaw_cfg, dict) else {}
        app_id = str(feishu_cfg.get("appId") or "").strip()
        app_secret = str(feishu_cfg.get("appSecret") or "").strip()
        if not app_id or not app_secret:
            return {
                "ok": False,
                "status": "missing_credentials",
                "message": "OpenClaw 主配置中缺少 Feishu appId/appSecret，无法走直连后备",
                "timestamp": datetime.now().isoformat(),
                "channel": route.get("channel"),
                "target": target,
                "account": route.get("account"),
                "route_source": route.get("route_source"),
            }

        api_base = self._resolve_feishu_open_api_base(feishu_cfg)
        token_url = f"{api_base}/open-apis/auth/v3/tenant_access_token/internal"
        token_payload = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
        token_req = Request(token_url, data=token_payload, headers={"Content-Type": "application/json"}, method="POST")

        try:
            with urlopen(token_req, timeout=max(3, self._notification_timeout_seconds)) as resp:
                token_data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return {
                "ok": False,
                "status": "token_http_error",
                "message": f"获取 Feishu tenant_access_token 失败: HTTP {exc.code} {body}".strip(),
                "timestamp": datetime.now().isoformat(),
                "channel": route.get("channel"),
                "target": target,
                "account": route.get("account"),
                "route_source": route.get("route_source"),
            }
        except URLError as exc:
            return {
                "ok": False,
                "status": "token_network_error",
                "message": f"获取 Feishu tenant_access_token 网络异常: {exc}",
                "timestamp": datetime.now().isoformat(),
                "channel": route.get("channel"),
                "target": target,
                "account": route.get("account"),
                "route_source": route.get("route_source"),
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": "token_error",
                "message": f"获取 Feishu tenant_access_token 异常: {exc}",
                "timestamp": datetime.now().isoformat(),
                "channel": route.get("channel"),
                "target": target,
                "account": route.get("account"),
                "route_source": route.get("route_source"),
            }

        access_token = str(token_data.get("tenant_access_token") or "").strip()
        if token_data.get("code") not in (None, 0) or not access_token:
            return {
                "ok": False,
                "status": "missing_access_token",
                "message": f"Feishu tenant_access_token 响应异常: {token_data}",
                "timestamp": datetime.now().isoformat(),
                "channel": route.get("channel"),
                "target": target,
                "account": route.get("account"),
                "route_source": route.get("route_source"),
            }

        send_url = f"{api_base}/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
        send_body = json.dumps(
            {
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": message_text}, ensure_ascii=False),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        send_req = Request(
            send_url,
            data=send_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            method="POST",
        )

        try:
            with urlopen(send_req, timeout=max(3, self._notification_timeout_seconds)) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                send_data = json.loads(raw) if raw else {}
            if send_data.get("code") not in (None, 0):
                return {
                    "ok": False,
                    "status": "send_api_error",
                    "message": f"Feishu 直连后备发送失败: {send_data}",
                    "timestamp": datetime.now().isoformat(),
                    "channel": route.get("channel"),
                    "target": target,
                    "account": route.get("account"),
                    "route_source": route.get("route_source"),
                    "response": send_data,
                }
            return {
                "ok": True,
                "status": "sent_via_direct_http",
                "message": "Feishu 直连后备发送成功",
                "timestamp": datetime.now().isoformat(),
                "channel": route.get("channel"),
                "target": target,
                "account": route.get("account"),
                "route_source": route.get("route_source"),
                "response": send_data,
                "receive_id_type": receive_id_type,
                "effective_path": "feishu_direct_http",
            }
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return {
                "ok": False,
                "status": "send_http_error",
                "message": f"Feishu 直连后备发送失败: HTTP {exc.code} {body}".strip(),
                "timestamp": datetime.now().isoformat(),
                "channel": route.get("channel"),
                "target": target,
                "account": route.get("account"),
                "route_source": route.get("route_source"),
            }
        except URLError as exc:
            return {
                "ok": False,
                "status": "send_network_error",
                "message": f"Feishu 直连后备网络异常: {exc}",
                "timestamp": datetime.now().isoformat(),
                "channel": route.get("channel"),
                "target": target,
                "account": route.get("account"),
                "route_source": route.get("route_source"),
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": "send_error",
                "message": f"Feishu 直连后备发送异常: {exc}",
                "timestamp": datetime.now().isoformat(),
                "channel": route.get("channel"),
                "target": target,
                "account": route.get("account"),
                "route_source": route.get("route_source"),
            }

    def _qqbot_direct_send(self, route: dict[str, Any], message_text: str) -> dict[str, Any]:
        if route.get("channel") != "qqbot":
            return {
                "ok": False,
                "status": "unsupported_channel",
                "message": f"直连后备暂只支持 qqbot，当前为 {route.get('channel')}",
                "timestamp": datetime.now().isoformat(),
            }

        target = str(route.get("target") or "").strip()
        if not target.startswith("qqbot:c2c:"):
            return {
                "ok": False,
                "status": "unsupported_target",
                "message": f"QQBot 直连后备目前仅支持 c2c target，当前为 {target}",
                "timestamp": datetime.now().isoformat(),
            }

        openclaw_cfg = self._load_openclaw_main_config()
        qqbot_cfg = ((openclaw_cfg.get("channels") or {}).get("qqbot") or {}) if isinstance(openclaw_cfg, dict) else {}
        app_id = str(qqbot_cfg.get("appId") or "").strip()
        client_secret = str(qqbot_cfg.get("clientSecret") or "").strip()
        if not app_id or not client_secret:
            return {
                "ok": False,
                "status": "missing_credentials",
                "message": "OpenClaw 主配置中缺少 QQBot appId/clientSecret，无法走直连后备",
                "timestamp": datetime.now().isoformat(),
            }

        openid = target.split(":", 2)[2]
        token_url = "https://bots.qq.com/app/getAppAccessToken"
        token_payload = json.dumps({"appId": app_id, "clientSecret": client_secret}).encode("utf-8")
        token_req = Request(token_url, data=token_payload, headers={"Content-Type": "application/json"}, method="POST")

        try:
            with urlopen(token_req, timeout=max(3, self._notification_timeout_seconds)) as resp:
                token_data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return {
                "ok": False,
                "status": "token_http_error",
                "message": f"获取 QQBot access_token 失败: HTTP {exc.code} {body}".strip(),
                "timestamp": datetime.now().isoformat(),
            }
        except URLError as exc:
            return {
                "ok": False,
                "status": "token_network_error",
                "message": f"获取 QQBot access_token 网络异常: {exc}",
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": "token_error",
                "message": f"获取 QQBot access_token 异常: {exc}",
                "timestamp": datetime.now().isoformat(),
            }

        access_token = str(token_data.get("access_token") or "").strip()
        if not access_token:
            return {
                "ok": False,
                "status": "missing_access_token",
                "message": f"QQBot access_token 响应异常: {token_data}",
                "timestamp": datetime.now().isoformat(),
            }

        send_url = f"https://api.sgroup.qq.com/v2/users/{openid}/messages"
        send_body = json.dumps({"content": message_text}).encode("utf-8")
        send_req = Request(
            send_url,
            data=send_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"QQBot {access_token}",
                "X-Union-Appid": app_id,
            },
            method="POST",
        )

        try:
            with urlopen(send_req, timeout=max(3, self._notification_timeout_seconds)) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                send_data = json.loads(raw) if raw else {}
            return {
                "ok": True,
                "status": "sent_via_direct_http",
                "message": "QQBot 直连后备发送成功",
                "timestamp": datetime.now().isoformat(),
                "channel": route.get("channel"),
                "target": target,
                "account": route.get("account"),
                "route_source": route.get("route_source"),
                "response": send_data,
            }
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return {
                "ok": False,
                "status": "send_http_error",
                "message": f"QQBot 直连后备发送失败: HTTP {exc.code} {body}".strip(),
                "timestamp": datetime.now().isoformat(),
                "channel": route.get("channel"),
                "target": target,
                "account": route.get("account"),
                "route_source": route.get("route_source"),
            }
        except URLError as exc:
            return {
                "ok": False,
                "status": "send_network_error",
                "message": f"QQBot 直连后备网络异常: {exc}",
                "timestamp": datetime.now().isoformat(),
                "channel": route.get("channel"),
                "target": target,
                "account": route.get("account"),
                "route_source": route.get("route_source"),
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": "send_error",
                "message": f"QQBot 直连后备发送异常: {exc}",
                "timestamp": datetime.now().isoformat(),
                "channel": route.get("channel"),
                "target": target,
                "account": route.get("account"),
                "route_source": route.get("route_source"),
            }

    def _dispatch_notification(self, notification: dict[str, Any]) -> None:
        delivery = notification.setdefault("delivery", {})
        if not delivery.get("push"):
            return

        if not self._notification_enabled:
            result = {
                "ok": False,
                "status": "disabled",
                "message": "主动通知已禁用",
                "timestamp": datetime.now().isoformat(),
            }
            delivery["dispatch"] = result
            with self._notification_dispatch_lock:
                self._last_notification_dispatch_result = deepcopy(result)
            return

        route, route_error = self._resolve_notification_route()
        if route is None:
            result = {
                "ok": False,
                "status": "not_routed",
                "message": route_error,
                "timestamp": datetime.now().isoformat(),
            }
            delivery["dispatch"] = result
            with self._notification_dispatch_lock:
                self._last_notification_dispatch_result = deepcopy(result)
            return

        message_text = self._format_push_message(notification)
        channel = str(route.get("channel") or "").lower()

        if channel == "qqbot":
            direct_result = self._qqbot_direct_send(route, message_text)
            result = {
                **direct_result,
                "primary_path": "qqbot_direct_http",
                "effective_path": direct_result.get("effective_path") if direct_result.get("ok") else None,
            }
            if not direct_result.get("ok"):
                cli_result = self._run_openclaw_cli_send(
                    route,
                    message_text,
                    success_message="QQBot 直连失败，已通过 OpenClaw CLI 补发成功",
                )
                result["fallback"] = cli_result
                if cli_result.get("ok"):
                    result["ok"] = True
                    result["status"] = cli_result.get("status", "sent_via_openclaw_cli")
                    result["message"] = (
                        f"QQBot 直连失败，已通过 OpenClaw CLI 补发成功；原因为：{direct_result.get('message', '')}"
                    )
                    result["effective_path"] = cli_result.get("effective_path")
        elif channel == "feishu":
            direct_result = self._feishu_direct_send(route, message_text)
            result = {
                **direct_result,
                "primary_path": "feishu_direct_http",
                "effective_path": direct_result.get("effective_path") if direct_result.get("ok") else None,
            }
            if not direct_result.get("ok"):
                result["fallback"] = {
                    "skipped": True,
                    "reason": "openclaw_cli_feishu_bootstrap_hang",
                    "message": "已跳过 OpenClaw CLI 的 Feishu 补发路径：当前已知该链路可能在 plugin bootstrap 阶段卡住",
                    "timestamp": datetime.now().isoformat(),
                    "primary_path": "openclaw_cli_message",
                }
        else:
            result = self._run_openclaw_cli_send(route, message_text)

        delivery["dispatch"] = result
        with self._notification_dispatch_lock:
            self._last_notification_dispatch_result = deepcopy(result)

    def _queue_notification(self, event_type: str, message: str, data: Optional[dict[str, Any]] = None) -> None:
        """将关键事件放入轻量通知队列，用于消息转发与前端轮询。"""
        policies = {
            "arm": {"push": False, "severity": "info", "dedupe_key": "arm_success"},
            "disarm": {"push": False, "severity": "info", "dedupe_key": "disarm_success"},
            "recover": {"push": False, "severity": "info", "dedupe_key": "recover_success"},
            "action_success": {"push": False, "severity": "critical", "dedupe_key": "action_success"},
            "danger_lock": {"push": True, "severity": "critical", "dedupe_key": "danger_lock"},
            "action_failure": {"push": True, "severity": "error", "dedupe_key": "action_failure"},
            "camera_failure": {"push": True, "severity": "warning", "dedupe_key": "camera_failure"},
        }
        policy = policies.get(event_type)
        if not policy:
            return

        now = time.time()
        dedupe_key = policy["dedupe_key"]
        last_sent_at = self._notification_last_sent_at.get(dedupe_key, 0.0)
        if now - last_sent_at < self._notification_dedupe_window_s:
            return
        self._notification_last_sent_at[dedupe_key] = now

        payload = deepcopy(data or {})
        timings = payload.get("timings", {}) if isinstance(payload, dict) else {}
        state_summary = self._build_state_summary()
        notification = {
            "id": self._notification_seq + 1,
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "severity": policy["severity"],
            "delivery": {
                "push": bool(policy.get("push", False)),
                "query": True,
                "dedupe_window_s": self._notification_dedupe_window_s,
                "active_push_enabled": self._notification_enabled,
                "dispatch": None,
            },
            "message": message,
            "state_summary": state_summary,
            "timings": timings if isinstance(timings, dict) else {},
            "details": payload,
        }
        self._notification_seq += 1
        notification["id"] = self._notification_seq

        with self._notifications_lock:
            self._notifications.append(notification)
            if len(self._notifications) > self._max_notifications:
                self._notifications = self._notifications[-self._max_notifications :]

        threading.Thread(
            target=self._dispatch_notification,
            args=(notification,),
            daemon=True,
            name=f"NotificationDispatch-{event_type}",
        ).start()

    def get_notifications(self, since_id: int = 0, limit: int = 20) -> list[dict[str, Any]]:
        """返回轻量通知队列，供 OpenClaw / WebUI 轮询。"""
        with self._notifications_lock:
            items = [deepcopy(item) for item in self._notifications if item.get("id", 0) > since_id]
        return items[:limit] if limit > 0 else items

    def _add_event(self, event_type: str, message: str, data: dict = None):
        """添加事件记录"""
        payload = deepcopy(data or {})
        payload.setdefault("state_summary", self._build_state_summary())
        event = EventRecord(event_type, message, payload)
        with self._events_lock:
            self._events.append(event)
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events :]

        self._update_last_action_result(event_type, message, payload)
        self._queue_notification(event_type, message, payload)

    def get_events(self, limit: int = 20) -> List[dict]:
        """获取最近的事件记录"""
        started_at = time.perf_counter()
        with self._events_lock:
            events = self._events[-limit:]
            result = [e.to_dict() for e in reversed(events)]
        self._update_engine_perf(last_events_ms=self._elapsed_ms(started_at))
        return result

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
        started_at = time.perf_counter()
        availability = self._refresh_component_availability(
            camera_source="initialize_probe",
            quick_camera_probe=True,
        )
        camera_available = availability["camera_available"]
        action_available = availability["action_chain_available"]
        safe_window_available = availability["safe_window_available"]

        warnings = []
        degraded_reasons = []

        if not camera_available:
            warnings.append("摄像头不可用，当前不可武装")
            degraded_reasons.append("camera_unavailable")
        if not action_available:
            warnings.append("Windows 窗口控制不可用（pywin32 未安装）")
            degraded_reasons.append("action_chain_unavailable")

        safe_window_status = self.action_chain.get_safe_window_status()
        primary_ok = safe_window_status.get(self.action_chain.primary_safe_app, False)
        backup_ok = safe_window_status.get(self.action_chain.backup_safe_app, False)

        if not primary_ok and not backup_ok:
            if not safe_window_available:
                warnings.append(
                    f"主备安全窗口均不可用（主: {self.action_chain.primary_safe_app}, 备: {self.action_chain.backup_safe_app}）"
                )
                degraded_reasons.append("safe_window_unavailable")
            else:
                warnings.append("安全窗口未运行，但可在需要时启动")
        elif not primary_ok:
            warnings.append(f"主安全窗口不可用，将使用备选: {self.action_chain.backup_safe_app}")

        total_ms = self._elapsed_ms(started_at)
        self._update_engine_perf(last_initialize_ms=total_ms)

        init_event_type = "init_degraded" if degraded_reasons else "init"
        init_message = "引擎初始化完成（降级模式）" if degraded_reasons else "引擎初始化完成"
        self._add_event(
            init_event_type,
            init_message,
            {
                "camera": camera_available,
                "action_chain": action_available,
                "safe_window_primary": primary_ok,
                "safe_window_backup": backup_ok,
                "warnings": warnings,
                "degraded": bool(degraded_reasons),
                "degraded_reasons": degraded_reasons,
                "timings": {
                    "total_ms": total_ms,
                },
            },
        )

        msg = init_message
        if warnings:
            msg += f" (警告: {', '.join(warnings)})"

        return True, msg

    def arm(self) -> tuple[bool, str]:
        """武装系统"""
        started_at = time.perf_counter()
        availability = self._refresh_component_availability(camera_source="arm_probe", quick_camera_probe=False)
        state = self.state_machine.state
        action_chain_available = availability["action_chain_available"]
        safe_window_available = availability["safe_window_available"]

        if not state.camera_available:
            self._update_engine_perf(last_arm_ms=self._elapsed_ms(started_at))
            return False, "摄像头不可用，无法武装"
        if not action_chain_available:
            self._update_engine_perf(last_arm_ms=self._elapsed_ms(started_at))
            return False, "动作链路不可用，无法武装"
        if not safe_window_available:
            self._update_engine_perf(last_arm_ms=self._elapsed_ms(started_at))
            return False, self.action_chain.last_error or "安全窗口不可用，无法武装"

        success, msg = self.state_machine.arm()
        if success:
            self._add_event("arm", "系统已武装")
            self._start_detection()

        self._update_engine_perf(last_arm_ms=self._elapsed_ms(started_at))
        return success, msg

    def disarm(self) -> tuple[bool, str]:
        """解除武装"""
        started_at = time.perf_counter()
        success, msg = self.state_machine.disarm()
        if success:
            self._add_event("disarm", "系统已解除武装")
            self._stop_detection()

        self._update_engine_perf(last_disarm_ms=self._elapsed_ms(started_at))
        return success, msg

    def recover(self) -> tuple[bool, str]:
        """手动恢复"""
        started_at = time.perf_counter()
        success, msg = self.state_machine.recover()
        if success:
            self._add_event("recover", "用户手动恢复系统")
            self._start_detection()

        self._update_engine_perf(last_recover_ms=self._elapsed_ms(started_at))
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
            loop_started_at = time.perf_counter()
            try:
                state = self.state_machine.state

                if state.arm_state != ArmState.ARMED:
                    time.sleep(0.1)
                    self._update_monitor_perf(
                        iterations=self._perf["monitor"]["iterations"] + 1,
                        last_loop_ms=self._elapsed_ms(loop_started_at),
                        last_state=state.arm_state.value,
                    )
                    continue

                if self.detector:
                    self._sync_detector_health()
                    camera_status = self.detector.get_camera_status()
                    if not camera_status.get("runtime_available", False):
                        time.sleep(0.1)
                        self._update_monitor_perf(
                            iterations=self._perf["monitor"]["iterations"] + 1,
                            last_loop_ms=self._elapsed_ms(loop_started_at),
                            last_state=state.arm_state.value,
                        )
                        continue

                    result = self.detector.latest_result
                    if result and result.person_detected:
                        self._handle_person_detected()
                    elif result and not result.person_detected:
                        with self._alert_lock:
                            if self._pre_alert_count > 0 or self._full_alert_count > 0:
                                self._pre_alert_count = 0
                                self._full_alert_count = 0
                                self.state_machine.reset_alert()
                                self._add_event("reset", "人体离开，重置报警计数")

                time.sleep(1.0 / 30)
                self._update_monitor_perf(
                    iterations=self._perf["monitor"]["iterations"] + 1,
                    last_loop_ms=self._elapsed_ms(loop_started_at),
                    last_state=state.arm_state.value,
                )

            except Exception as e:
                logger.error(f"监控循环异常: {e}")
                self._update_monitor_perf(
                    iterations=self._perf["monitor"]["iterations"] + 1,
                    last_loop_ms=self._elapsed_ms(loop_started_at),
                    last_state="exception",
                )
                time.sleep(1)

        logger.info("监控循环结束")

    def _handle_person_detected(self):
        """处理检测到人体"""
        with self._alert_lock:
            now = time.time()
            if now < self._full_alert_cooldown_until:
                return

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
        started_at = time.perf_counter()
        self.state_machine.trigger_full_alert()
        self._add_event("full_alert", "触发完全报警，执行安全动作")

        result = self.action_chain.execute_full_alert()

        if result["success"]:
            self._add_event("action_success", "报警动作链执行成功", result)
            self._pre_alert_count = 0
            self._full_alert_count = 0
            self._full_alert_cooldown_until = time.time() + max(2.0, self._notification_dedupe_window_s)
            self.state_machine.enter_danger_lock()
            self._add_event(
                "danger_lock",
                "动作成功，进入危险锁定状态，需人工恢复",
                {
                    "triggered_by": "action_success",
                    "safe_window_used": result.get("safe_window_used"),
                    "risk_apps_minimized": result.get("risk_apps_minimized"),
                    "timings": result.get("timings", {}),
                },
            )
            self._stop_detection()
        else:
            self._add_event("action_failure", "报警动作链执行失败", result)
            logger.error(f"报警动作链执行失败: {result['errors']}")
            self._full_alert_cooldown_until = time.time() + max(2.0, self._notification_dedupe_window_s)
            self.state_machine.enter_danger_lock()
            self._add_event(
                "danger_lock",
                "动作失败，仍进入危险锁定状态，需人工恢复",
                {
                    "errors": result.get("errors", []),
                    "timings": result.get("timings", {}),
                },
            )
            self._stop_detection()

        self._update_engine_perf(last_full_alert_ms=self._elapsed_ms(started_at))

    def test_action_chain(self, full_check: bool = False) -> dict:
        """手动测试动作链，不改变武装/报警状态机"""
        started_at = time.perf_counter()
        state = self.state_machine.state

        if full_check:
            availability = self._refresh_component_availability(
                camera_source="manual_test_probe",
                quick_camera_probe=True,
            )
            probe_mode = "full"
        else:
            action_chain_available = self.action_chain.is_available()
            safe_window_available = (
                self.action_chain.check_safe_window_available()
                if action_chain_available
                else False
            )

            if self.detector:
                camera_status = {
                    **self.detector.get_camera_status(),
                    "source": "live_detector_quick_test",
                }
            else:
                camera_status = dict(self._last_camera_probe_status)
                camera_status["source"] = "cached_probe_quick_test"

            availability = {
                "camera_available": state.camera_available,
                "camera_status": camera_status,
                "action_chain_available": action_chain_available,
                "safe_window_available": safe_window_available,
            }
            self.state_machine.update_availability(
                action_chain=action_chain_available,
                safe_window=safe_window_available,
            )
            probe_mode = "quick"

        result = {
            "success": False,
            "message": "",
            "safe_window_switched": False,
            "safe_window_used": None,
            "risk_apps_minimized": 0,
            "errors": [],
            "warnings": [],
            "probe_mode": probe_mode,
            "availability": availability,
            "timings": {
                "availability_refresh_ms": 0.0,
                "switch_ms": 0.0,
                "minimize_ms": 0.0,
                "total_ms": 0.0,
            },
            "diagnostics": {
                "switch": {},
                "minimize": {},
            },
        }
        result["timings"]["availability_refresh_ms"] = round((time.perf_counter() - started_at) * 1000, 2)

        if not availability["action_chain_available"]:
            result["message"] = "动作链路不可用，无法执行模拟切换"
            result["errors"].append(result["message"])
            result["timings"]["total_ms"] = round((time.perf_counter() - started_at) * 1000, 2)
            result["perf"] = self.get_perf_snapshot()
            self._update_engine_perf(last_action_test_ms=result["timings"]["total_ms"])
            self._add_event("action_test_failure", result["message"], result)
            logger.error(result["message"])
            return result

        success, msg, switch_details = self.action_chain.switch_to_safe_window_detailed()
        result["diagnostics"]["switch"] = switch_details
        result["timings"]["switch_ms"] = switch_details.get("total_ms", 0.0)
        if success:
            result["safe_window_switched"] = True
            result["safe_window_used"] = switch_details.get("target_app")
            if (
                result["safe_window_used"] == self.action_chain.backup_safe_app
                and self.action_chain.backup_safe_app != self.action_chain.primary_safe_app
            ):
                result["warnings"].append(f"主安全窗口不可用，测试时使用备选: {self.action_chain.backup_safe_app}")
        else:
            result["errors"].append(msg or "安全窗口切换失败")

        minimized_count, minimize_details = self.action_chain.minimize_risk_apps_with_details()
        result["risk_apps_minimized"] = minimized_count
        result["diagnostics"]["minimize"] = minimize_details
        result["timings"]["minimize_ms"] = minimize_details.get("elapsed_ms", 0.0)
        result["success"] = result["safe_window_switched"]
        result["timings"]["total_ms"] = round((time.perf_counter() - started_at) * 1000, 2)
        result["perf"] = self.get_perf_snapshot()
        self._update_engine_perf(last_action_test_ms=result["timings"]["total_ms"])

        if result["success"]:
            target_name = result["safe_window_used"] or self.action_chain.primary_safe_app
            result["message"] = (
                f"模拟切换完成：已切换到 {target_name}，"
                f"最小化 {result['risk_apps_minimized']} 个风险窗口，"
                f"切换耗时 {result['timings']['switch_ms']}ms，总耗时 {result['timings']['total_ms']}ms"
            )
            self._add_event(
                "action_test",
                result["message"],
                {
                    "safe_window_used": result["safe_window_used"],
                    "risk_apps_minimized": result["risk_apps_minimized"],
                    "warnings": result["warnings"],
                    "probe_mode": result["probe_mode"],
                    "timings": result["timings"],
                },
            )
            logger.info(result["message"])
        else:
            result["message"] = result["errors"][0] if result["errors"] else "模拟切换失败"
            self._add_event(
                "action_test_failure",
                result["message"],
                {
                    "errors": result["errors"],
                    "risk_apps_minimized": result["risk_apps_minimized"],
                    "probe_mode": result["probe_mode"],
                    "timings": result["timings"],
                },
            )
            logger.error(
                f"{result['message']}，模式={result['probe_mode']}，切换耗时={result['timings']['switch_ms']}ms，总耗时={result['timings']['total_ms']}ms"
            )

        return result

    def get_config(self) -> dict:
        """获取当前配置快照"""
        with self._config_lock:
            return deepcopy(self.config)

    def reload_config(self, new_config: dict) -> dict:
        """对运行中引擎应用新配置，并返回热加载结果"""
        started_at = time.perf_counter()
        normalized_config = normalize_config(new_config)

        with self._config_lock:
            previous_config = deepcopy(self.config)
            change_summary = analyze_config_changes(previous_config, normalized_config)
            changed_keys = change_summary["changed_keys"]

            if not changed_keys:
                result = {
                    "success": True,
                    "message": "配置无变化",
                    "changed_keys": [],
                    "effective_immediately": [],
                    "effective_on_next_detection_start": [],
                    "detector_restart_required": [],
                    "detector_restarted": False,
                    "service_restart_required": [],
                    "unknown": [],
                    "timings": {
                        "availability_refresh_ms": 0.0,
                        "total_ms": self._elapsed_ms(started_at),
                    },
                    "perf": self.get_perf_snapshot(),
                }
                self._update_engine_perf(last_reload_config_ms=result["timings"]["total_ms"])
                return result

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
                "timings": {
                    "availability_refresh_ms": availability.get("timings", {}).get("total_ms", 0.0),
                    "total_ms": self._elapsed_ms(started_at),
                },
                "perf": self.get_perf_snapshot(),
            }

            self._add_event(
                "config_reload",
                result["message"],
                {
                    "changed_keys": changed_keys,
                    "detector_restarted": detector_restarted,
                    "service_restart_required": change_summary["service_restart_required"],
                    "timings": result["timings"],
                },
            )
            self._update_engine_perf(last_reload_config_ms=result["timings"]["total_ms"])
            logger.info(result["message"])
            return result

    def get_status(self) -> dict:
        """获取当前状态（用于 CLI/WebUI）"""
        started_at = time.perf_counter()
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

        if isinstance(status.get("camera_runtime_status"), dict):
            status["camera_runtime_status"].setdefault("availability_timings", {})
            status["camera_runtime_status"]["availability_timings"].update(
                {
                    "last_refresh_ms": self._perf["engine"].get("last_availability_refresh_ms", 0.0),
                    "last_refresh_source": self._perf["engine"].get("last_availability_source"),
                }
            )

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
        status["monitoring_active"] = bool(self._running and state.arm_state == ArmState.ARMED)
        status["last_action_result"] = deepcopy(self._last_action_result)
        status["timeline"] = self._build_recent_timeline()
        status["remote_action_matrix"] = self._build_remote_action_matrix()
        status["session_policy"] = {
            "session_label_scope": "conversation_isolation_only",
            "business_state_in_session": False,
            "business_state_source": "local_engine_runtime",
            "recommended_usage": [
                "session / session-label 仅用于会话隔离",
                "业务状态必须始终以本地 status/state_snapshot 为准",
            ],
        }
        with self._notifications_lock:
            pending_notifications = len(self._notifications)
            latest_notification_id = self._notifications[-1]["id"] if self._notifications else 0
        status["notification_channel"] = {
            "poll_endpoint": "/api/notifications",
            "supported_immediate_events": ["danger_lock", "action_failure", "camera_failure"],
            "query_only_events": ["arm", "disarm", "recover", "action_test", "config_reload", "pre_alert", "action_success"],
            "pending": pending_notifications,
            "latest_id": latest_notification_id,
            "dedupe_window_s": self._notification_dedupe_window_s,
            "active_push_enabled": self._notification_enabled,
            "push_command": self._notification_command_resolved,
            "context": self._get_notification_context_snapshot(),
            "last_dispatch": self.get_notification_dispatch_status(),
        }
        status["observability"] = {
            "protection_label": (
                "危险锁定中，需人工恢复"
                if status.get("is_locked")
                else "实时保护中"
                if status.get("is_protecting")
                else "已武装，等待触发"
                if status.get("arm_state") == ArmState.ARMED.value
                else "未武装"
            ),
            "confidence_mode": "lightweight_no_forensics",
            "last_action_success": self._last_action_result.get("success"),
            "timeline_size": len(status["timeline"]),
        }
        status["evidence_policy"] = {
            "heavy_image_capture": False,
            "default_frame_retention": "transient_only",
            "forensics_mode": False,
        }
        status["perf"] = self.get_perf_snapshot()
        status["timings"] = {
            "total_ms": self._elapsed_ms(started_at),
        }

        self._update_engine_perf(
            last_status_ms=status["timings"]["total_ms"],
            last_status_timestamp=datetime.now().isoformat(),
        )
        return status

    def doctor(self) -> dict:
        """
        健康检查
        Phase 2 增强：更详细的组件状态和错误历史
        """
        started_at = time.perf_counter()
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
                    "status": "✅ 可用" if action_chain_available else "❌ 可用",
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

        report["perf"] = self.get_perf_snapshot()
        report["timings"] = {
            "total_ms": self._elapsed_ms(started_at),
        }
        self._update_engine_perf(
            last_doctor_ms=report["timings"]["total_ms"],
            last_doctor_timestamp=datetime.now().isoformat(),
        )
        return report

    def shutdown(self):
        """关闭引擎"""
        self._stop_detection()
        logger.info("引擎已关闭")

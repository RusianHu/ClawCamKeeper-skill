"""
核心状态机实现
负责管理 未武装 / 已武装 / 危险锁定 三态转换
"""

import threading
import time
from datetime import datetime
from typing import Callable, Optional
from loguru import logger

from .state import ArmState, AlertPhase, SystemState


class StateMachine:
    """
    核心状态机
    管理武装状态与报警阶段的转换
    """

    def __init__(self):
        self._state = SystemState()
        self._lock = threading.Lock()
        self._listeners: list[Callable] = []

    @property
    def state(self) -> SystemState:
        with self._lock:
            return self._state

    def add_listener(self, callback: Callable[[SystemState], None]):
        """添加状态变化监听器"""
        self._listeners.append(callback)

    def _notify_listeners(self):
        """通知所有监听器状态变化"""
        with self._lock:
            state_copy = SystemState(**self._state.__dict__)
        for listener in self._listeners:
            try:
                listener(state_copy)
            except Exception as e:
                logger.error(f"状态监听器异常: {e}")

    def _update_state(self, **kwargs):
        """线程安全地更新状态"""
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._state, key):
                    setattr(self._state, key, value)
            self._state.last_event_time = datetime.now()
        self._notify_listeners()

    def arm(self) -> tuple[bool, str]:
        """
        武装系统
        Returns: (success, message)
        """
        with self._lock:
            if self._state.arm_state == ArmState.ARMED:
                return False, "系统已处于武装状态"
            if self._state.arm_state == ArmState.DANGER_LOCKED:
                return False, "系统处于危险锁定状态，请先手动恢复"

        self._update_state(
            arm_state=ArmState.ARMED,
            alert_phase=AlertPhase.NONE,
            armed_at=datetime.now(),
            last_event_message="系统已武装"
        )
        logger.info("系统已武装")
        return True, "系统已武装"

    def disarm(self) -> tuple[bool, str]:
        """
        解除武装
        Returns: (success, message)
        """
        with self._lock:
            if self._state.arm_state == ArmState.UNARMED:
                return False, "系统已处于未武装状态"

        self._update_state(
            arm_state=ArmState.UNARMED,
            alert_phase=AlertPhase.NONE,
            armed_at=None,
            last_event_message="系统已解除武装"
        )
        logger.info("系统已解除武装")
        return True, "系统已解除武装"

    def trigger_pre_alert(self) -> bool:
        """
        触发第一阶段：无感预备
        Returns: 是否成功触发
        """
        with self._lock:
            if self._state.arm_state != ArmState.ARMED:
                return False
            if self._state.alert_phase != AlertPhase.NONE:
                return False

        self._update_state(
            alert_phase=AlertPhase.PRE_ALERT,
            last_event_message="检测到人体进入，进入无感预备状态"
        )
        logger.info("触发第一阶段：无感预备")
        return True

    def trigger_full_alert(self) -> bool:
        """
        触发第二阶段：自动切窗
        Returns: 是否成功触发
        """
        with self._lock:
            if self._state.arm_state != ArmState.ARMED:
                return False

        self._update_state(
            alert_phase=AlertPhase.FULL_ALERT,
            last_event_message="触发完全报警，执行安全动作"
        )
        logger.info("触发第二阶段：完全报警")
        return True

    def enter_danger_lock(self) -> bool:
        """
        进入危险锁定状态
        Returns: 是否成功进入
        """
        with self._lock:
            if self._state.arm_state != ArmState.ARMED:
                return False

        self._update_state(
            arm_state=ArmState.DANGER_LOCKED,
            alert_phase=AlertPhase.NONE,
            locked_at=datetime.now(),
            last_event_message="进入危险锁定状态"
        )
        logger.warning("系统已进入危险锁定状态")
        return True

    def recover(self) -> tuple[bool, str]:
        """
        手动恢复系统
        Returns: (success, message)
        """
        with self._lock:
            if self._state.arm_state != ArmState.DANGER_LOCKED:
                return False, "系统未处于危险锁定状态，无需恢复"

        self._update_state(
            arm_state=ArmState.ARMED,
            alert_phase=AlertPhase.NONE,
            locked_at=None,
            last_event_message="用户手动恢复系统"
        )
        logger.info("用户手动恢复系统")
        return True, "系统已恢复武装状态"

    def update_availability(self, camera: bool = None, safe_window: bool = None, action_chain: bool = None):
        """更新系统各组件可用性状态"""
        updates = {}
        if camera is not None:
            updates['camera_available'] = camera
        if safe_window is not None:
            updates['safe_window_available'] = safe_window
        if action_chain is not None:
            updates['action_chain_available'] = action_chain
        
        if updates:
            self._update_state(**updates)

    def reset_alert(self) -> bool:
        """重置报警阶段（内部使用）"""
        with self._lock:
            if self._state.alert_phase == AlertPhase.NONE:
                return False

        self._update_state(alert_phase=AlertPhase.NONE)
        logger.debug("报警阶段已重置为 NONE")
        return True

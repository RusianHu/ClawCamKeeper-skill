"""
系统状态枚举与状态机定义
"""

from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class ArmState(Enum):
    """武装状态枚举"""
    UNARMED = "unarmed"          # 未武装：程序运行中，但当前不承担防护承诺
    ARMED = "armed"              # 已武装：摄像头在线、安全窗口可切、本地动作链可用
    DANGER_LOCKED = "danger_locked"  # 危险锁定：已触发安全动作，保持静默锁定，等待手动恢复


class AlertPhase(Enum):
    """报警阶段枚举"""
    NONE = "none"                # 无报警
    PRE_ALERT = "pre_alert"      # 第一阶段：无感预备
    FULL_ALERT = "full_alert"    # 第二阶段：自动切窗


@dataclass
class SystemState:
    """系统全局状态"""
    arm_state: ArmState = ArmState.UNARMED
    alert_phase: AlertPhase = AlertPhase.NONE
    camera_available: bool = False
    safe_window_available: bool = False
    action_chain_available: bool = False
    last_event_time: Optional[datetime] = None
    last_event_message: Optional[str] = None
    armed_at: Optional[datetime] = None
    locked_at: Optional[datetime] = None

    @property
    def is_protecting(self) -> bool:
        """是否正在提供防护"""
        return self.arm_state == ArmState.ARMED

    @property
    def is_locked(self) -> bool:
        """是否处于危险锁定状态"""
        return self.arm_state == ArmState.DANGER_LOCKED

    def to_dict(self) -> dict:
        """转换为字典，用于 JSON 输出"""
        return {
            "arm_state": self.arm_state.value,
            "alert_phase": self.alert_phase.value,
            "is_protecting": self.is_protecting,
            "is_locked": self.is_locked,
            "camera_available": self.camera_available,
            "safe_window_available": self.safe_window_available,
            "action_chain_available": self.action_chain_available,
            "armed_at": self.armed_at.isoformat() if self.armed_at else None,
            "locked_at": self.locked_at.isoformat() if self.locked_at else None,
            "last_event_time": self.last_event_time.isoformat() if self.last_event_time else None,
            "last_event_message": self.last_event_message,
        }

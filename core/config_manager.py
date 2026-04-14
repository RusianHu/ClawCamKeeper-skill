"""
统一配置读写与差异分析模块
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

import yaml


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"

DEFAULT_CONFIG_TEMPLATE = {
    "camera": {
        "device_index": 0,
        "fps": 30,
        "frame_height": 480,
        "frame_width": 640,
    },
    "detection": {
        "confidence_threshold": 0.5,
        "full_alert_frames": 30,
        "pre_alert_frames": 10,
        "risk_zone": {
            "height": 1.0,
            "width": 1.0,
            "x": 0.0,
            "y": 0.0,
        },
    },
    "logging": {
        "file": "clawcamkeeper.log",
        "level": "INFO",
    },
    "risk_apps": [
        "chrome.exe",
        "msedge.exe",
        "firefox.exe",
        "code.exe",
        "WeChat.exe",
        "QQ.exe",
    ],
    "safe_window": {
        "backup": "calc.exe",
        "primary": "notepad.exe",
    },
    "webui": {
        "allow_lan": False,
        "debug": False,
        "host": "127.0.0.1",
        "port": 8765,
    },
    "openclaw": {
        "notifications": {
            "enabled": False,
            "command": "openclaw",
            "timeout_seconds": 8,
            "context_ttl_seconds": 900,
            "message_prefix": "[ClawCamKeeper]",
            "routes": {
                "qqbot": {
                    "target": "",
                    "account": "",
                },
                "feishu": {
                    "target": "",
                    "account": "",
                },
            },
            "fallback": {
                "channel": "",
                "target": "",
                "account": "",
            },
        },
    },
}

IMMEDIATE_KEYS = {
    "safe_window.primary",
    "safe_window.backup",
    "risk_apps",
    "detection.pre_alert_frames",
    "detection.full_alert_frames",
    "openclaw.notifications",
}

IMMEDIATE_PREFIXES = (
    "openclaw.notifications.",
)

DETECTOR_RESTART_PREFIXES = (
    "camera.",
    "detection.confidence_threshold",
    "detection.risk_zone.",
)

SERVICE_RESTART_PREFIXES = (
    "webui.",
    "logging.",
)


def resolve_config_path(config_path: Optional[str | Path] = None) -> Path:
    """解析配置文件路径"""
    if config_path is None:
        return DEFAULT_CONFIG_PATH
    return Path(config_path).resolve()


def _deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深度合并字典，保留 override 值"""
    merged = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def merge_config(base_config: Optional[dict[str, Any]], override_config: Optional[dict[str, Any]]) -> dict[str, Any]:
    """公开的配置深度合并入口"""
    return _deep_merge_dict(base_config or {}, override_config or {})


def _ensure_dict(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"配置项 {field_name} 必须是对象")
    return dict(value)


def _normalize_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"配置项 {field_name} 必须是布尔值")


def _normalize_int(value: Any, field_name: str, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"配置项 {field_name} 必须是整数") from exc

    if minimum is not None and normalized < minimum:
        raise ValueError(f"配置项 {field_name} 不能小于 {minimum}")
    if maximum is not None and normalized > maximum:
        raise ValueError(f"配置项 {field_name} 不能大于 {maximum}")
    return normalized


def _normalize_float(value: Any, field_name: str, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"配置项 {field_name} 必须是数值") from exc

    if minimum is not None and normalized < minimum:
        raise ValueError(f"配置项 {field_name} 不能小于 {minimum}")
    if maximum is not None and normalized > maximum:
        raise ValueError(f"配置项 {field_name} 不能大于 {maximum}")
    return normalized


def _normalize_str(value: Any, field_name: str) -> str:
    if value is None:
        raise ValueError(f"配置项 {field_name} 不能为空")
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"配置项 {field_name} 不能为空")
    return normalized



def _normalize_optional_str(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None



def _normalize_loopback_host(value: Any, field_name: str, allow_lan: bool = False) -> str:
    normalized = _normalize_str(value, field_name).lower()
    if normalized in {"127.0.0.1", "localhost"}:
        return "127.0.0.1"
    if allow_lan:
        return _normalize_str(value, field_name)
    raise ValueError(f"配置项 {field_name} 在未开启 webui.allow_lan 时仅允许本地回环地址 127.0.0.1（或 localhost），禁止暴露到局域网/外网")



def _normalize_notification_route(value: Any, field_name: str) -> dict[str, Optional[str]]:
    route = _ensure_dict(value, field_name)
    return {
        **route,
        "target": _normalize_optional_str(route.get("target"), f"{field_name}.target"),
        "account": _normalize_optional_str(route.get("account"), f"{field_name}.account"),
    }


def _normalize_risk_apps(value: Any) -> list[str]:
    if value is None:
        return []

    if isinstance(value, str):
        candidates = [part.strip() for part in value.replace(",", "\n").splitlines()]
    elif isinstance(value, (list, tuple, set)):
        candidates = [str(item).strip() for item in value]
    else:
        raise ValueError("配置项 risk_apps 必须是字符串列表")

    result: list[str] = []
    for item in candidates:
        if item and item not in result:
            result.append(item)
    return result


def normalize_config(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    """规范化配置并补齐默认值"""
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("配置内容必须是对象")

    merged = _deep_merge_dict(DEFAULT_CONFIG_TEMPLATE, config)

    camera = _ensure_dict(merged.get("camera"), "camera")
    detection = _ensure_dict(merged.get("detection"), "detection")
    risk_zone = _ensure_dict(detection.get("risk_zone"), "detection.risk_zone")
    logging_cfg = _ensure_dict(merged.get("logging"), "logging")
    safe_window = _ensure_dict(merged.get("safe_window"), "safe_window")
    webui = _ensure_dict(merged.get("webui"), "webui")
    openclaw = _ensure_dict(merged.get("openclaw"), "openclaw")
    openclaw_notifications = _ensure_dict(openclaw.get("notifications"), "openclaw.notifications")
    openclaw_routes = _ensure_dict(openclaw_notifications.get("routes"), "openclaw.notifications.routes")
    openclaw_fallback = _normalize_notification_route(
        openclaw_notifications.get("fallback"),
        "openclaw.notifications.fallback",
    )

    normalized_routes: dict[str, dict[str, Optional[str]]] = {}
    for channel, route_value in openclaw_routes.items():
        normalized_channel = _normalize_str(channel, "openclaw.notifications.routes.<channel>").lower()
        normalized_routes[normalized_channel] = _normalize_notification_route(
            route_value,
            f"openclaw.notifications.routes.{normalized_channel}",
        )

    extra_top_level = {
        key: deepcopy(value)
        for key, value in merged.items()
        if key not in {"camera", "detection", "logging", "risk_apps", "safe_window", "webui", "openclaw"}
    }

    normalized = {
        **extra_top_level,
        "camera": {
            **camera,
            "device_index": _normalize_int(camera.get("device_index", 0), "camera.device_index", minimum=0),
            "fps": _normalize_int(camera.get("fps", 30), "camera.fps", minimum=1, maximum=240),
            "frame_height": _normalize_int(camera.get("frame_height", 480), "camera.frame_height", minimum=1),
            "frame_width": _normalize_int(camera.get("frame_width", 640), "camera.frame_width", minimum=1),
        },
        "detection": {
            **detection,
            "confidence_threshold": _normalize_float(
                detection.get("confidence_threshold", 0.5),
                "detection.confidence_threshold",
                minimum=0.0,
                maximum=1.0,
            ),
            "full_alert_frames": _normalize_int(
                detection.get("full_alert_frames", 30),
                "detection.full_alert_frames",
                minimum=1,
            ),
            "pre_alert_frames": _normalize_int(
                detection.get("pre_alert_frames", 10),
                "detection.pre_alert_frames",
                minimum=1,
            ),
            "risk_zone": {
                **risk_zone,
                "height": _normalize_float(risk_zone.get("height", 1.0), "detection.risk_zone.height", minimum=0.01, maximum=1.0),
                "width": _normalize_float(risk_zone.get("width", 1.0), "detection.risk_zone.width", minimum=0.01, maximum=1.0),
                "x": _normalize_float(risk_zone.get("x", 0.0), "detection.risk_zone.x", minimum=0.0, maximum=1.0),
                "y": _normalize_float(risk_zone.get("y", 0.0), "detection.risk_zone.y", minimum=0.0, maximum=1.0),
            },
        },
        "logging": {
            **logging_cfg,
            "file": _normalize_str(logging_cfg.get("file", "clawcamkeeper.log"), "logging.file"),
            "level": _normalize_str(logging_cfg.get("level", "INFO"), "logging.level").upper(),
        },
        "risk_apps": _normalize_risk_apps(merged.get("risk_apps")),
        "safe_window": {
            **safe_window,
            "backup": _normalize_str(safe_window.get("backup", "calc.exe"), "safe_window.backup"),
            "primary": _normalize_str(safe_window.get("primary", "notepad.exe"), "safe_window.primary"),
        },
        "webui": {
            **webui,
            "allow_lan": _normalize_bool(webui.get("allow_lan", False), "webui.allow_lan"),
            "debug": _normalize_bool(webui.get("debug", False), "webui.debug"),
            "host": _normalize_loopback_host(
                webui.get("host", "127.0.0.1"),
                "webui.host",
                allow_lan=_normalize_bool(webui.get("allow_lan", False), "webui.allow_lan"),
            ),
            "port": _normalize_int(webui.get("port", 8765), "webui.port", minimum=1, maximum=65535),
        },
        "openclaw": {
            **openclaw,
            "notifications": {
                **openclaw_notifications,
                "enabled": _normalize_bool(openclaw_notifications.get("enabled", False), "openclaw.notifications.enabled"),
                "command": _normalize_str(openclaw_notifications.get("command", "openclaw"), "openclaw.notifications.command"),
                "timeout_seconds": _normalize_int(
                    openclaw_notifications.get("timeout_seconds", 8),
                    "openclaw.notifications.timeout_seconds",
                    minimum=1,
                    maximum=120,
                ),
                "context_ttl_seconds": _normalize_int(
                    openclaw_notifications.get("context_ttl_seconds", 900),
                    "openclaw.notifications.context_ttl_seconds",
                    minimum=1,
                    maximum=86400,
                ),
                "message_prefix": _normalize_str(
                    openclaw_notifications.get("message_prefix", "[ClawCamKeeper]"),
                    "openclaw.notifications.message_prefix",
                ),
                "routes": normalized_routes,
                "fallback": {
                    **openclaw_fallback,
                    "channel": _normalize_optional_str(openclaw_fallback.get("channel"), "openclaw.notifications.fallback.channel"),
                    "target": _normalize_optional_str(openclaw_fallback.get("target"), "openclaw.notifications.fallback.target"),
                    "account": _normalize_optional_str(openclaw_fallback.get("account"), "openclaw.notifications.fallback.account"),
                },
            },
        },
    }

    rz = normalized["detection"]["risk_zone"]
    if rz["x"] + rz["width"] > 1.0:
        raise ValueError("配置项 detection.risk_zone.x + width 不能大于 1.0")
    if rz["y"] + rz["height"] > 1.0:
        raise ValueError("配置项 detection.risk_zone.y + height 不能大于 1.0")

    return normalized


def load_config(config_path: Optional[str | Path] = None) -> dict[str, Any]:
    """从 YAML 文件加载配置"""
    path = resolve_config_path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with open(path, "r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}
    return normalize_config(raw)


def save_config(config: dict[str, Any], config_path: Optional[str | Path] = None) -> tuple[dict[str, Any], Path]:
    """保存配置到 YAML 文件"""
    path = resolve_config_path(config_path)
    normalized = normalize_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        yaml.safe_dump(normalized, file, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return normalized, path


def _flatten_config(config: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in config.items():
        current_path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flattened.update(_flatten_config(value, current_path))
        else:
            flattened[current_path] = value
    return flattened


def _classify_changed_key(key: str) -> str:
    if key in IMMEDIATE_KEYS:
        return "immediate"
    if any(key == prefix.rstrip(".") or key.startswith(prefix) for prefix in IMMEDIATE_PREFIXES):
        return "immediate"
    if any(key == prefix.rstrip(".") or key.startswith(prefix) for prefix in DETECTOR_RESTART_PREFIXES):
        return "detector_restart_required"
    if any(key == prefix.rstrip(".") or key.startswith(prefix) for prefix in SERVICE_RESTART_PREFIXES):
        return "service_restart_required"
    return "unknown"


def analyze_config_changes(old_config: dict[str, Any], new_config: dict[str, Any]) -> dict[str, list[str]]:
    """分析配置变更及其生效影响"""
    old_normalized = normalize_config(old_config)
    new_normalized = normalize_config(new_config)

    old_flat = _flatten_config(old_normalized)
    new_flat = _flatten_config(new_normalized)

    all_keys = sorted(set(old_flat) | set(new_flat))
    changed_keys = [key for key in all_keys if old_flat.get(key) != new_flat.get(key)]

    result = {
        "changed_keys": changed_keys,
        "immediate": [],
        "detector_restart_required": [],
        "service_restart_required": [],
        "unknown": [],
    }

    for key in changed_keys:
        category = _classify_changed_key(key)
        result[category].append(key)

    return result

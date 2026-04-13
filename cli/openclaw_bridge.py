"""
OpenClaw / ACP 薄适配层。

设计原则：
- 只通过当前项目 CLI 的 JSON 输出作为稳定边界
- 不直接操作 Core 内部对象
- 为 OpenClaw / ACP 提供统一、机器可读的返回结构
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import click


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIN_SCRIPT = PROJECT_ROOT / "main.py"

DEFAULT_TIMEOUTS = {
    "status": 10,
    "doctor": 15,
    "arm": 10,
    "disarm": 10,
    "recover": 10,
    "config-show": 10,
    "config-reload": 15,
    "config-set": 10,
    "events": 10,
    "notifications": 10,
    "openclaw-context": 10,
    "openclaw-context-show": 10,
    "action-test": 35,
}

SUCCESS_MESSAGES = {
    "status": "状态获取成功",
    "doctor": "健康检查完成",
    "arm": "远程武装请求执行完成",
    "disarm": "远程解除武装请求执行完成",
    "recover": "远程恢复请求执行完成",
    "config-show": "配置读取成功",
    "events": "事件查询成功",
    "notifications": "通知查询成功",
    "set-safe-window": "安全窗口配置更新完成",
    "action-test": "动作链测试完成",
}


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 2)


def _safe_json(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return str(value)


def _extract_json_payload(text: str) -> tuple[Any, Optional[str]]:
    raw = (text or "").strip()
    if not raw:
        return None, "stdout 为空"

    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char not in "[{":
            continue
        try:
            payload, end = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue

        trailing = raw[index + end :].strip()
        if trailing:
            return payload, trailing
        return payload, None

    return None, "stdout 中未找到可解析 JSON"


def _run_cli_json(
    command_name: str,
    command_args: Optional[list[str]] = None,
    *,
    config_path: Optional[str] = None,
    timeout: Optional[int] = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    args = list(command_args or [])
    if "--json" not in args and "-j" not in args:
        args.append("--json")

    command = [sys.executable, str(MAIN_SCRIPT)]
    if config_path:
        command.extend(["--config", config_path])
    command.append(command_name)
    command.extend(args)

    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout or DEFAULT_TIMEOUTS.get(command_name, 15),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "command_name": command_name,
            "command": command,
            "exit_code": None,
            "error_type": "timeout",
            "message": f"CLI 调用超时: {command_name}",
            "payload": None,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "") if isinstance(exc.stderr, str) else "",
            "bridge_perf": {
                "bridge_total_ms": _elapsed_ms(started_at),
                "timeout_s": timeout or DEFAULT_TIMEOUTS.get(command_name, 15),
            },
        }

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    payload, trailing = _extract_json_payload(stdout)

    if completed.returncode == 0 and payload is not None:
        return {
            "ok": True,
            "command_name": command_name,
            "command": command,
            "exit_code": completed.returncode,
            "payload": payload,
            "stdout": stdout,
            "stderr": stderr,
            "trailing_output": trailing,
            "bridge_perf": {
                "bridge_total_ms": _elapsed_ms(started_at),
            },
        }

    message = None
    error_type = "cli_error"
    if isinstance(payload, dict):
        message = payload.get("error") or payload.get("message")
        cli_perf = payload.get("cli_perf", {}) if isinstance(payload, dict) else {}
        if isinstance(cli_perf, dict) and cli_perf.get("http_status") == 0:
            error_type = "service_unavailable"
    if not message:
        message = f"CLI 调用失败: {command_name}"
    if payload is None and trailing:
        message = f"CLI 输出包含非 JSON 尾部内容: {trailing}"

    return {
        "ok": False,
        "command_name": command_name,
        "command": command,
        "exit_code": completed.returncode,
        "error_type": error_type,
        "message": message,
        "payload": payload,
        "stdout": stdout,
        "stderr": stderr,
        "trailing_output": trailing,
        "bridge_perf": {
            "bridge_total_ms": _elapsed_ms(started_at),
        },
    }


def _collect_state_snapshot(config_path: Optional[str]) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    result = _run_cli_json("status", config_path=config_path, timeout=DEFAULT_TIMEOUTS["status"])
    if result.get("ok") and isinstance(result.get("payload"), dict):
        return result["payload"], None

    return None, {
        "message": result.get("message", "状态快照获取失败"),
        "error_type": result.get("error_type", "snapshot_unavailable"),
        "exit_code": result.get("exit_code"),
    }


def _build_timings(result: dict[str, Any], payload: Any) -> dict[str, Any]:
    timings: dict[str, Any] = dict(result.get("bridge_perf", {}))

    if isinstance(payload, dict):
        cli_perf = payload.get("cli_perf", {})
        if isinstance(cli_perf, dict):
            timings["cli_client_total_ms"] = cli_perf.get("client_total_ms", 0.0)
            timings["server_request_ms"] = cli_perf.get("server_request_ms", 0.0)
            timings["http_status"] = cli_perf.get("http_status", 0)

        payload_timings = payload.get("timings", {})
        if isinstance(payload_timings, dict):
            for key, value in payload_timings.items():
                timings[f"payload_{key}"] = value

    return timings


def _build_source(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "layer": "openclaw_bridge",
        "strategy": "subprocess_cli_json",
        "project_root": str(PROJECT_ROOT),
        "main_script": str(MAIN_SCRIPT),
        "command_name": result.get("command_name"),
        "exit_code": result.get("exit_code"),
    }


def _build_response(
    *,
    action: str,
    result: dict[str, Any],
    data: Any = None,
    message: Optional[str] = None,
    state_snapshot: Optional[dict[str, Any]] = None,
    snapshot_error: Optional[dict[str, Any]] = None,
    extra_debug: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = result.get("payload")
    ok = bool(result.get("ok"))

    response = {
        "ok": ok,
        "action": action,
        "message": message or (payload.get("message") if isinstance(payload, dict) else None) or SUCCESS_MESSAGES.get(action, action),
        "data": _safe_json(data if data is not None else payload),
        "timings": _build_timings(result, payload),
        "source": _build_source(result),
        "state_snapshot": _safe_json(state_snapshot),
    }

    if not ok:
        response["error_type"] = result.get("error_type", "cli_error")
        response["message"] = result.get("message") or response["message"]

    debug = {
        "stdout": result.get("stdout", "").strip(),
        "stderr": result.get("stderr", "").strip(),
        "trailing_output": result.get("trailing_output"),
        "command": result.get("command"),
    }
    if snapshot_error is not None:
        debug["snapshot_error"] = snapshot_error
    if extra_debug:
        debug.update(extra_debug)
    response["debug"] = _safe_json(debug)
    return response


def _echo_response(response: dict[str, Any]) -> None:
    click.echo(json.dumps(response, indent=2, ensure_ascii=False))
    if not response.get("ok"):
        raise click.exceptions.Exit(1)



def _normalize_context_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None



def _env_first(*names: str) -> Optional[str]:
    for name in names:
        value = _normalize_context_value(os.environ.get(name))
        if value:
            return value
    return None



def _register_notification_context(
    *,
    config_path: Optional[str],
    session_key: Optional[str],
    session_label: Optional[str],
    channel: Optional[str],
    target: Optional[str],
    account: Optional[str],
) -> dict[str, Any]:
    resolved = {
        "session_key": _normalize_context_value(session_key) or _env_first("OPENCLAW_SESSION_KEY", "OPENCLAW_SESSION"),
        "session_label": _normalize_context_value(session_label) or _env_first("OPENCLAW_SESSION_LABEL"),
        "channel": _normalize_context_value(channel) or _env_first("OPENCLAW_CHANNEL", "OPENCLAW_MESSAGE_CHANNEL"),
        "target": _normalize_context_value(target) or _env_first("OPENCLAW_TARGET", "OPENCLAW_MESSAGE_TARGET"),
        "account": _normalize_context_value(account) or _env_first("OPENCLAW_ACCOUNT", "OPENCLAW_MESSAGE_ACCOUNT"),
    }
    if resolved["channel"]:
        resolved["channel"] = resolved["channel"].lower()

    if not any(resolved.values()):
        return {
            "attempted": False,
            "ok": None,
            "reason": "no_context_hints",
            "context": resolved,
        }

    args: list[str] = ["--source", "openclaw_bridge"]
    if resolved["session_key"]:
        args.extend(["--session-key", resolved["session_key"]])
    if resolved["session_label"]:
        args.extend(["--session-label", resolved["session_label"]])
    if resolved["channel"]:
        args.extend(["--channel", resolved["channel"]])
    if resolved["target"]:
        args.extend(["--target", resolved["target"]])
    if resolved["account"]:
        args.extend(["--account", resolved["account"]])

    result = _run_cli_json(
        "openclaw-context",
        args,
        config_path=config_path,
        timeout=DEFAULT_TIMEOUTS["openclaw-context"],
    )
    return {
        "attempted": True,
        "ok": bool(result.get("ok")),
        "context": resolved,
        "result": {
            "message": result.get("message"),
            "error_type": result.get("error_type"),
            "exit_code": result.get("exit_code"),
            "payload": result.get("payload"),
        },
    }


def _context_debug(ctx: click.Context) -> dict[str, Any]:
    return {
        "notification_context_registration": ctx.obj.get("notification_context_registration"),
    }


@click.group(name="openclaw")
@click.option("--config", "config_path", default=None, help="配置文件路径")
@click.option("--session-key", default=None, help="OpenClaw session key")
@click.option("--session-label", default=None, help="OpenClaw session label")
@click.option("--channel", default=None, help="OpenClaw 渠道，如 qqbot / feishu")
@click.option("--target", default=None, help="OpenClaw 渠道目标")
@click.option("--account", default=None, help="OpenClaw 渠道账号")
@click.pass_context
def openclaw_bridge(
    ctx: click.Context,
    config_path: Optional[str],
    session_key: Optional[str],
    session_label: Optional[str],
    channel: Optional[str],
    target: Optional[str],
    account: Optional[str],
) -> None:
    """OpenClaw / ACP 适配命令。"""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["session_key"] = session_key
    ctx.obj["session_label"] = session_label
    ctx.obj["channel"] = channel
    ctx.obj["target"] = target
    ctx.obj["account"] = account
    ctx.obj["notification_context_registration"] = _register_notification_context(
        config_path=config_path,
        session_key=session_key,
        session_label=session_label,
        channel=channel,
        target=target,
        account=account,
    )


@openclaw_bridge.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    config_path = ctx.obj.get("config_path")
    result = _run_cli_json("status", config_path=config_path)
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else None
    response = _build_response(
        action="status",
        result=result,
        data=payload,
        state_snapshot=payload,
        extra_debug=_context_debug(ctx),
    )
    _echo_response(response)


@openclaw_bridge.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    config_path = ctx.obj.get("config_path")
    result = _run_cli_json("doctor", config_path=config_path)
    snapshot, snapshot_error = _collect_state_snapshot(config_path)
    response = _build_response(
        action="doctor",
        result=result,
        data=result.get("payload"),
        state_snapshot=snapshot,
        snapshot_error=snapshot_error,
        extra_debug=_context_debug(ctx),
    )
    _echo_response(response)


@openclaw_bridge.command()
@click.pass_context
def arm(ctx: click.Context) -> None:
    config_path = ctx.obj.get("config_path")
    result = _run_cli_json("arm", config_path=config_path)
    snapshot, snapshot_error = _collect_state_snapshot(config_path)
    response = _build_response(
        action="arm",
        result=result,
        data=result.get("payload"),
        state_snapshot=snapshot,
        snapshot_error=snapshot_error,
        extra_debug=_context_debug(ctx),
    )
    _echo_response(response)


@openclaw_bridge.command()
@click.pass_context
def disarm(ctx: click.Context) -> None:
    config_path = ctx.obj.get("config_path")
    result = _run_cli_json("disarm", config_path=config_path)
    snapshot, snapshot_error = _collect_state_snapshot(config_path)
    response = _build_response(
        action="disarm",
        result=result,
        data=result.get("payload"),
        state_snapshot=snapshot,
        snapshot_error=snapshot_error,
        extra_debug=_context_debug(ctx),
    )
    _echo_response(response)


@openclaw_bridge.command()
@click.pass_context
def recover(ctx: click.Context) -> None:
    config_path = ctx.obj.get("config_path")
    result = _run_cli_json("recover", config_path=config_path)
    snapshot, snapshot_error = _collect_state_snapshot(config_path)
    response = _build_response(
        action="recover",
        result=result,
        data=result.get("payload"),
        state_snapshot=snapshot,
        snapshot_error=snapshot_error,
        extra_debug=_context_debug(ctx),
    )
    _echo_response(response)


@openclaw_bridge.command("config-show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    config_path = ctx.obj.get("config_path")
    result = _run_cli_json("config-show", config_path=config_path)
    snapshot, snapshot_error = _collect_state_snapshot(config_path)
    response = _build_response(
        action="config-show",
        result=result,
        data=result.get("payload"),
        state_snapshot=snapshot,
        snapshot_error=snapshot_error,
        extra_debug=_context_debug(ctx),
    )
    _echo_response(response)


@openclaw_bridge.command()
@click.option("--limit", default=20, type=int, show_default=True, help="返回事件数量")
@click.pass_context
def events(ctx: click.Context, limit: int) -> None:
    config_path = ctx.obj.get("config_path")
    result = _run_cli_json("events", ["--limit", str(limit)], config_path=config_path)
    payload = result.get("payload")
    data = {
        "events": payload if isinstance(payload, list) else [],
        "count": len(payload) if isinstance(payload, list) else 0,
        "limit": limit,
    }
    snapshot, snapshot_error = _collect_state_snapshot(config_path)
    response = _build_response(
        action="events",
        result=result,
        data=data,
        state_snapshot=snapshot,
        snapshot_error=snapshot_error,
        extra_debug=_context_debug(ctx),
    )
    _echo_response(response)


@openclaw_bridge.command()
@click.option("--since-id", default=0, type=int, show_default=True, help="仅返回大于该 ID 的通知")
@click.option("--limit", default=20, type=int, show_default=True, help="返回通知数量")
@click.pass_context
def notifications(ctx: click.Context, since_id: int, limit: int) -> None:
    config_path = ctx.obj.get("config_path")
    result = _run_cli_json(
        "notifications",
        ["--since-id", str(since_id), "--limit", str(limit)],
        config_path=config_path,
    )
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    data = {
        "notifications": payload.get("notifications", []) if isinstance(payload, dict) else [],
        "since_id": payload.get("since_id", since_id) if isinstance(payload, dict) else since_id,
        "latest_id": payload.get("latest_id", since_id) if isinstance(payload, dict) else since_id,
        "limit": limit,
    }
    snapshot, snapshot_error = _collect_state_snapshot(config_path)
    response = _build_response(
        action="notifications",
        result=result,
        data=data,
        state_snapshot=snapshot,
        snapshot_error=snapshot_error,
        extra_debug=_context_debug(ctx),
    )
    _echo_response(response)


@openclaw_bridge.command("set-safe-window")
@click.option("--primary", default=None, help="主安全窗口进程名")
@click.option("--backup", default=None, help="备选安全窗口进程名")
@click.pass_context
def set_safe_window(ctx: click.Context, primary: Optional[str], backup: Optional[str]) -> None:
    if not primary and not backup:
        response = {
            "ok": False,
            "action": "set-safe-window",
            "message": "至少提供 --primary 或 --backup 其中之一",
            "error_type": "invalid_arguments",
            "data": None,
            "timings": {},
            "source": {
                "layer": "openclaw_bridge",
                "strategy": "subprocess_cli_json",
                "project_root": str(PROJECT_ROOT),
            },
            "state_snapshot": None,
            "debug": {},
        }
        _echo_response(response)

    config_path = ctx.obj.get("config_path")
    config_args: list[str] = []
    if primary:
        config_args.extend(["--safe-window", primary])
    if backup:
        config_args.extend(["--backup-window", backup])

    save_result = _run_cli_json("config-set", config_args, config_path=config_path)
    if not save_result.get("ok"):
        response = _build_response(
            action="set-safe-window",
            result=save_result,
            data={
                "config_set": save_result.get("payload"),
                "config_reload": None,
            },
            extra_debug=_context_debug(ctx),
        )
        _echo_response(response)

    reload_result = _run_cli_json("config-reload", config_path=config_path)
    snapshot, snapshot_error = _collect_state_snapshot(config_path)

    combined_ok = bool(save_result.get("ok")) and bool(reload_result.get("ok"))
    bridge_total_ms = round(
        save_result.get("bridge_perf", {}).get("bridge_total_ms", 0.0)
        + reload_result.get("bridge_perf", {}).get("bridge_total_ms", 0.0),
        2,
    )

    combined_result = {
        "ok": combined_ok,
        "command_name": "set-safe-window",
        "command": {
            "config_set": save_result.get("command"),
            "config_reload": reload_result.get("command"),
        },
        "exit_code": reload_result.get("exit_code") if not combined_ok else 0,
        "payload": {
            "config_set": save_result.get("payload"),
            "config_reload": reload_result.get("payload"),
        },
        "stdout": "",
        "stderr": "\n".join(filter(None, [save_result.get("stderr", ""), reload_result.get("stderr", "")])),
        "trailing_output": None,
        "bridge_perf": {
            "bridge_total_ms": bridge_total_ms,
        },
        "message": (
            reload_result.get("message")
            if not reload_result.get("ok")
            else save_result.get("payload", {}).get("message")
            if isinstance(save_result.get("payload"), dict)
            else SUCCESS_MESSAGES["set-safe-window"]
        ),
        "error_type": reload_result.get("error_type", "cli_error") if not reload_result.get("ok") else None,
    }

    if not reload_result.get("ok"):
        combined_result["message"] = "配置已保存，但未能热加载到运行中的本地服务"
        combined_result["error_type"] = reload_result.get("error_type", "service_unavailable")

    response = _build_response(
        action="set-safe-window",
        result=combined_result,
        data={
            "config_set": save_result.get("payload"),
            "config_reload": reload_result.get("payload"),
            "requested": {
                "primary": primary,
                "backup": backup,
            },
        },
        state_snapshot=snapshot,
        snapshot_error=snapshot_error,
        extra_debug=_context_debug(ctx),
    )
    _echo_response(response)


@openclaw_bridge.command("notification-context")
@click.pass_context
def notification_context(ctx: click.Context) -> None:
    config_path = ctx.obj.get("config_path")
    result = _run_cli_json("openclaw-context-show", config_path=config_path)
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else None
    response = _build_response(
        action="notification-context",
        result=result,
        data=payload,
        extra_debug=_context_debug(ctx),
    )
    _echo_response(response)


@openclaw_bridge.command("action-test")
@click.option("--full-check", is_flag=True, help="执行完整检查（含摄像头探测）")
@click.pass_context
def action_test(ctx: click.Context, full_check: bool) -> None:
    config_path = ctx.obj.get("config_path")
    args = ["--full-check"] if full_check else []
    result = _run_cli_json("action-test", args, config_path=config_path, timeout=DEFAULT_TIMEOUTS["action-test"])
    snapshot, snapshot_error = _collect_state_snapshot(config_path)
    response = _build_response(
        action="action-test",
        result=result,
        data=result.get("payload"),
        state_snapshot=snapshot,
        snapshot_error=snapshot_error,
        extra_debug=_context_debug(ctx),
    )
    _echo_response(response)

"""
CLI 命令实现
使用 click 构建命令行接口
"""

import atexit
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import click
import psutil
from loguru import logger

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config_manager import load_config, resolve_config_path, save_config
from core.engine import MonitorEngine
from cli.openclaw_bridge import openclaw_bridge


# 默认配置路径
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
DEFAULT_API_BASE = "http://127.0.0.1:8765/api"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(tempfile.gettempdir()) / "clawcamkeeper-openclaw"
INSTANCE_FILE = RUNTIME_DIR / "service-instance.json"


def _ensure_runtime_dir() -> Path:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    return RUNTIME_DIR


def _instance_file_path() -> Path:
    _ensure_runtime_dir()
    return INSTANCE_FILE


def _normalize_probe_host(host: str) -> str:
    normalized = str(host or "127.0.0.1").strip().lower()
    if normalized in {"0.0.0.0", "::", "::1", "localhost"}:
        return "127.0.0.1"
    return str(host or "127.0.0.1").strip()


def _is_port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    probe_host = _normalize_probe_host(host)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            return sock.connect_ex((probe_host, int(port))) == 0
        except OSError:
            return False


def _read_instance_record() -> Optional[dict[str, Any]]:
    path = _instance_file_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_instance_record(record: dict[str, Any]) -> None:
    path = _instance_file_path()
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")


def _clear_instance_record(expected_pid: Optional[int] = None) -> None:
    path = _instance_file_path()
    if not path.exists():
        return
    if expected_pid is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return

    record = _read_instance_record()
    if not isinstance(record, dict) or int(record.get("pid") or -1) == int(expected_pid):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        process = psutil.Process(int(pid))
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def _summarize_process(pid: Optional[int]) -> dict[str, Any]:
    if not pid:
        return {"pid": None, "alive": False}
    try:
        process = psutil.Process(int(pid))
        return {
            "pid": process.pid,
            "alive": process.is_running() and process.status() != psutil.STATUS_ZOMBIE,
            "name": process.name(),
            "cmdline": process.cmdline(),
            "create_time": process.create_time(),
        }
    except Exception:
        return {"pid": int(pid), "alive": False}


def _get_listening_pids(host: str, port: int) -> list[int]:
    probe_host = _normalize_probe_host(host)
    results: list[int] = []
    for conn in psutil.net_connections(kind="inet"):
        try:
            if conn.status != psutil.CONN_LISTEN:
                continue
            if not conn.laddr:
                continue
            laddr_ip = getattr(conn.laddr, "ip", None) or conn.laddr[0]
            laddr_port = getattr(conn.laddr, "port", None) or conn.laddr[1]
            if int(laddr_port) != int(port):
                continue
            normalized_ip = _normalize_probe_host(str(laddr_ip))
            if normalized_ip != probe_host:
                continue
            if conn.pid and conn.pid not in results:
                results.append(int(conn.pid))
        except Exception:
            continue
    return results


def _process_matches_project_service(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        process = psutil.Process(int(pid))
        cmdline = [str(part) for part in process.cmdline()]
        joined = " ".join(cmdline).lower()
        cwd = ""
        try:
            cwd = str(process.cwd()).lower()
        except Exception:
            cwd = ""
        project_marker = str(PROJECT_ROOT).lower()
        has_main = any(Path(part).name.lower() == "main.py" for part in cmdline) or ".\\main.py" in joined or " ./main.py" in f" {joined}"
        has_run = any(str(part).strip().lower() == "run" for part in cmdline)
        same_project = project_marker in joined or cwd == project_marker
        return bool(has_main and has_run and same_project)
    except Exception:
        return False


def _find_managed_service_pids(host: str, port: int) -> list[int]:
    pids: list[int] = []
    record = _read_instance_record()
    record_pid = int((record or {}).get("pid") or 0)
    if record_pid and record_pid not in pids and _process_matches_project_service(record_pid):
        pids.append(record_pid)

    for pid in _get_listening_pids(host, port):
        if pid not in pids and _process_matches_project_service(pid):
            pids.append(pid)

    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            pid = int(proc.info.get("pid") or 0)
            if pid and pid not in pids and _process_matches_project_service(pid):
                pids.append(pid)
        except Exception:
            continue
    return pids


def _collect_runtime_targets(config_path: Optional[str] = None) -> tuple[str, int]:
    cfg = load_config(config_path)
    webui_config = cfg.get("webui", {})
    host = webui_config.get("host", "127.0.0.1")
    port = int(webui_config.get("port", 8765))
    return host, port


def _best_effort_graceful_disarm(host: str, port: int, timeout: float = 3.0) -> dict[str, Any]:
    """在停止服务前尽量优雅解除武装，给检测线程释放摄像头句柄的机会。"""
    started_at = time.perf_counter()
    url = f"http://{_normalize_probe_host(host)}:{int(port)}/api/disarm"
    try:
        req = urllib.request.Request(url, data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {
                "attempted": True,
                "ok": True,
                "status": "disarmed",
                "message": data.get("message", "系统已解除武装"),
                "response": data,
                "timings": {"total_ms": round((time.perf_counter() - started_at) * 1000, 2)},
            }
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {"error": f"HTTP {e.code}: {e.reason}"}

        error_text = str(payload.get("error") or payload.get("message") or "")
        benign = any(token in error_text for token in ["未武装", "already", "not armed"])
        return {
            "attempted": True,
            "ok": benign,
            "status": "already_unarmed" if benign else "http_error",
            "message": error_text or f"HTTP {e.code}: {e.reason}",
            "response": payload,
            "timings": {"total_ms": round((time.perf_counter() - started_at) * 1000, 2)},
        }
    except Exception as exc:
        return {
            "attempted": True,
            "ok": False,
            "status": "skipped",
            "message": str(exc),
            "timings": {"total_ms": round((time.perf_counter() - started_at) * 1000, 2)},
        }


def _terminate_managed_service_group(host: str, port: int, timeout: float = 12.0) -> dict[str, Any]:
    started_at = time.perf_counter()
    graceful_stop = _best_effort_graceful_disarm(host, port)
    if graceful_stop.get("ok"):
        time.sleep(0.8)

    targets = _find_managed_service_pids(host, port)
    results: list[dict[str, Any]] = []

    for pid in targets:
        results.append(_terminate_managed_process(pid, timeout=timeout))

    remaining = _find_managed_service_pids(host, port)
    success = len(remaining) == 0
    if success:
        _clear_instance_record()

    return {
        "success": success,
        "message": "所有受管服务进程已停止" if success else "仍有受管服务进程未停止",
        "graceful_stop": graceful_stop,
        "targets": targets,
        "results": results,
        "remaining": remaining,
        "timings": {"total_ms": round((time.perf_counter() - started_at) * 1000, 2)},
    }


def _acquire_single_instance_lock(config_path: str, host: str, port: int, no_webui: bool) -> tuple[bool, Optional[dict[str, Any]]]:
    path = _instance_file_path()
    record = {
        "pid": os.getpid(),
        "started_at": datetime.now().isoformat(),
        "config_path": str(resolve_config_path(config_path)),
        "host": host,
        "port": port,
        "no_webui": bool(no_webui),
        "project_root": str(PROJECT_ROOT),
    }

    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = _read_instance_record()
            existing_pid = int((existing or {}).get("pid") or 0)
            if existing_pid and not _pid_alive(existing_pid):
                _clear_instance_record(expected_pid=existing_pid)
                continue
            return False, existing
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2, ensure_ascii=False)
            return True, record

    return False, _read_instance_record()


def _coerce_config_value(raw: str) -> Any:
    text = str(raw)
    stripped = text.strip()
    if stripped == "":
        return ""

    try:
        return json.loads(stripped)
    except Exception:
        pass

    lowered = stripped.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None

    try:
        return int(stripped)
    except ValueError:
        pass

    try:
        return float(stripped)
    except ValueError:
        pass

    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1]
    return text


def _set_nested_config_value(config: dict[str, Any], dotted_key: str, value: Any) -> None:
    segments = [segment.strip() for segment in str(dotted_key).split(".") if segment.strip()]
    if not segments:
        raise ValueError("配置键不能为空")

    cursor: dict[str, Any] = config
    for segment in segments[:-1]:
        existing = cursor.get(segment)
        if existing is None:
            cursor[segment] = {}
            existing = cursor[segment]
        if not isinstance(existing, dict):
            raise ValueError(f"配置键 {dotted_key} 的父路径 {segment} 不是对象，无法继续写入")
        cursor = existing
    cursor[segments[-1]] = value


def _terminate_managed_process(pid: int, timeout: float = 12.0) -> dict[str, Any]:
    started_at = time.perf_counter()
    try:
        process = psutil.Process(int(pid))
    except psutil.NoSuchProcess:
        _clear_instance_record(expected_pid=pid)
        return {
            "success": True,
            "message": "服务进程已不存在，已清理实例记录",
            "pid": pid,
            "terminated": False,
            "timings": {"total_ms": round((time.perf_counter() - started_at) * 1000, 2)},
        }

    try:
        children = process.children(recursive=True)
    except Exception:
        children = []

    terminated_pids: list[int] = []
    killed_pids: list[int] = []

    try:
        for child in children:
            try:
                child.terminate()
            except Exception:
                continue

        process.terminate()

        gone, alive = psutil.wait_procs([*children, process], timeout=timeout)
        for proc in gone:
            terminated_pids.append(int(proc.pid))

        if alive:
            for proc in alive:
                try:
                    proc.kill()
                    killed_pids.append(int(proc.pid))
                except Exception:
                    continue
            gone2, alive2 = psutil.wait_procs(alive, timeout=max(3.0, timeout / 2))
            for proc in gone2:
                if int(proc.pid) not in terminated_pids:
                    terminated_pids.append(int(proc.pid))
            if alive2:
                return {
                    "success": False,
                    "message": "仍有服务相关进程未停止",
                    "pid": pid,
                    "terminated": False,
                    "remaining_pids": [int(proc.pid) for proc in alive2],
                    "terminated_pids": terminated_pids,
                    "killed_pids": killed_pids,
                    "timings": {"total_ms": round((time.perf_counter() - started_at) * 1000, 2)},
                }
    except Exception as exc:
        return {
            "success": False,
            "message": f"停止服务失败: {exc}",
            "pid": pid,
            "terminated": False,
            "terminated_pids": terminated_pids,
            "killed_pids": killed_pids,
            "timings": {"total_ms": round((time.perf_counter() - started_at) * 1000, 2)},
        }

    _clear_instance_record(expected_pid=pid)
    return {
        "success": True,
        "message": "服务已停止",
        "pid": pid,
        "terminated": True,
        "terminated_pids": terminated_pids,
        "killed_pids": killed_pids,
        "timings": {"total_ms": round((time.perf_counter() - started_at) * 1000, 2)},
    }


def _spawn_detached_service(config_path: Optional[str], no_webui: bool) -> tuple[int, list[str]]:
    command = [sys.executable, str(PROJECT_ROOT / "main.py")]
    if config_path:
        command.extend(["--config", str(resolve_config_path(config_path))])
    command.append("run")
    if no_webui:
        command.append("--no-webui")

    creationflags = 0
    creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)

    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    return process.pid, command


def _service_launch_options_from_record(record: Optional[dict[str, Any]]) -> tuple[Optional[str], bool]:
    if not isinstance(record, dict):
        return None, False
    return record.get("config_path"), bool(record.get("no_webui", False))


def _rollback_service_start(previous_record: Optional[dict[str, Any]]) -> dict[str, Any]:
    config_path, no_webui = _service_launch_options_from_record(previous_record)
    pid, command = _spawn_detached_service(config_path, no_webui)
    ready, status_payload = _wait_for_service_ready(config_path, timeout_s=25.0, expected_pid=pid)
    return {
        "attempted": True,
        "success": ready,
        "pid": pid,
        "command": command,
        "config_path": config_path,
        "no_webui": no_webui,
        "status": status_payload,
    }


def _wait_for_service_ready(config_path: Optional[str], timeout_s: float = 20.0, expected_pid: Optional[int] = None) -> tuple[bool, dict[str, Any]]:
    deadline = time.time() + timeout_s
    last_error: dict[str, Any] = {"error": "服务尚未就绪"}
    host, port = _collect_runtime_targets(config_path)
    while time.time() < deadline:
        listening_pids = _get_listening_pids(host, port)
        if expected_pid and listening_pids and int(expected_pid) not in listening_pids:
            last_error = {
                "error": "检测到端口已被其他进程占用，新进程未接管监听",
                "expected_pid": int(expected_pid),
                "listening_pids": listening_pids,
            }
            time.sleep(0.6)
            continue

        success, data = api_request("GET", "/status", config_path=config_path, timeout=2)
        if success:
            if expected_pid:
                confirmed_listening_pids = _get_listening_pids(host, port)
                data.setdefault("runtime_validation", {})
                data["runtime_validation"].update(
                    {
                        "expected_pid": int(expected_pid),
                        "listening_pids": confirmed_listening_pids,
                        "pid_match": int(expected_pid) in confirmed_listening_pids if confirmed_listening_pids else False,
                    }
                )
                if confirmed_listening_pids and int(expected_pid) not in confirmed_listening_pids:
                    last_error = {
                        "error": "健康检查通过，但监听端口仍不是新进程，判定为旧代码未完成替换",
                        "expected_pid": int(expected_pid),
                        "listening_pids": confirmed_listening_pids,
                    }
                    time.sleep(0.6)
                    continue
            return True, data
        last_error = data
        time.sleep(0.6)
    return False, last_error


def get_api_base(config_path: Optional[str] = None) -> str:
    """根据配置文件解析 WebUI API 地址"""
    try:
        cfg = load_config(config_path)
        webui_config = cfg.get("webui", {})
        host = webui_config.get("host", "127.0.0.1")
        port = webui_config.get("port", 8765)
        return f"http://{host}:{port}/api"
    except Exception:
        return DEFAULT_API_BASE


def _build_cli_perf(url: str, started_at: float, status_code: int = 0, response_data: Optional[dict] = None) -> dict:
    response_data = response_data or {}
    meta = response_data.get("meta", {}) if isinstance(response_data, dict) else {}
    meta_perf = meta.get("perf", {}) if isinstance(meta, dict) else {}
    return {
        "url": url,
        "http_status": status_code,
        "client_total_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "server_request_ms": meta_perf.get("request_ms", 0.0),
        "endpoint": meta.get("endpoint"),
        "meta": meta,
    }


def api_request(method: str, path: str, data: dict = None, timeout: int = 5, config_path: Optional[str] = None) -> tuple[bool, dict]:
    """
    发送 API 请求到 WebUI 后端
    Returns: (success, response_data)
    """
    url = f"{get_api_base(config_path)}{path}"
    started_at = time.perf_counter()

    try:
        if data is not None:
            req_data = json.dumps(data).encode("utf-8")
            req = urllib.request.Request(url, data=req_data, method=method)
            req.add_header("Content-Type", "application/json")
        else:
            req = urllib.request.Request(url, method=method)

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            if isinstance(resp_data, dict):
                resp_data.setdefault("cli_perf", _build_cli_perf(url, started_at, getattr(resp, "status", 200), resp_data))
            return True, resp_data
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8"))
            if isinstance(payload, dict):
                payload.setdefault("cli_perf", _build_cli_perf(url, started_at, e.code, payload))
            return False, payload
        except Exception:
            return False, {
                "error": f"HTTP {e.code}: {e.reason}",
                "cli_perf": _build_cli_perf(url, started_at, e.code),
            }
    except urllib.error.URLError as e:
        return False, {
            "error": f"无法连接到服务: {e}",
            "cli_perf": _build_cli_perf(url, started_at),
        }
    except Exception as e:
        return False, {
            "error": str(e),
            "cli_perf": _build_cli_perf(url, started_at),
        }


def print_perf_summary(data: dict):
    """打印 CLI 性能摘要"""
    cli_perf = data.get("cli_perf", {}) if isinstance(data, dict) else {}
    if not cli_perf:
        return

    client_total_ms = cli_perf.get("client_total_ms", 0.0)
    server_request_ms = cli_perf.get("server_request_ms", 0.0)
    http_status = cli_perf.get("http_status", "-")
    endpoint = cli_perf.get("endpoint") or cli_perf.get("url") or "-"
    click.echo(f"⏱️  CLI总耗时: {client_total_ms}ms | 服务端路由耗时: {server_request_ms}ms | HTTP: {http_status} | 端点: {endpoint}")


def format_status(status: dict, json_output: bool = False):
    """格式化状态输出"""
    if json_output:
        click.echo(json.dumps(status, indent=2, ensure_ascii=False))
        return

    arm_state = status.get("arm_state", "unknown")
    alert_phase = status.get("alert_phase", "unknown")
    is_protecting = status.get("is_protecting", False)
    is_locked = status.get("is_locked", False)
    timings = status.get("timings", {})
    perf = status.get("perf", {})

    state_icons = {
        "unarmed": "⚪",
        "armed": "🟢",
        "danger_locked": "🔴",
    }
    icon = state_icons.get(arm_state, "❓")

    click.echo(f"\n{'=' * 40}")
    click.echo("  ClawCamKeeper 状态")
    click.echo(f"{'=' * 40}")
    click.echo(f"  武装状态: {icon} {arm_state}")
    click.echo(f"  报警阶段: {alert_phase}")
    click.echo(f"  正在防护: {'是' if is_protecting else '否'}")
    click.echo(f"  危险锁定: {'是' if is_locked else '否'}")
    click.echo(f"  状态采样耗时: {timings.get('total_ms', 0.0)}ms")

    engine_perf = perf.get("engine", {})
    detector_perf = perf.get("detector", {})
    frame_perf = detector_perf.get("frame", {}) if isinstance(detector_perf, dict) else {}
    click.echo(f"  最近 doctor 耗时: {engine_perf.get('last_doctor_ms', 0.0)}ms")
    click.echo(f"  最近可用性刷新耗时: {engine_perf.get('last_availability_refresh_ms', 0.0)}ms")
    click.echo(f"  最近检测循环耗时: {frame_perf.get('last_total_loop_ms', 0.0)}ms")
    click.echo(f"{'=' * 40}\n")


def format_doctor(report: dict, json_output: bool = False):
    """格式化健康检查输出"""
    if json_output:
        click.echo(json.dumps(report, indent=2, ensure_ascii=False))
        return

    healthy = report.get("healthy", False)
    issues = report.get("issues", [])
    warnings = report.get("warnings", [])
    components = report.get("components", {})
    timings = report.get("timings", {})
    perf = report.get("perf", {})

    def component_available(component: dict) -> bool:
        if isinstance(component, dict):
            available = component.get("available")
            if available is not None:
                return bool(available)
            status = component.get("status")
            if isinstance(status, str):
                return status.startswith("✅")
        return bool(component)

    click.echo(f"\n{'=' * 40}")
    click.echo("  ClawCamKeeper 健康检查")
    click.echo(f"{'=' * 40}")
    click.echo(f"  健康状态: {'✅ 正常' if healthy else '❌ 异常'}")
    click.echo(f"  健康检查耗时: {timings.get('total_ms', 0.0)}ms")
    click.echo("")
    click.echo("  组件状态:")

    camera = components.get("camera", {})
    safe_window = components.get("safe_window", {})
    action_chain = components.get("action_chain", {})

    click.echo(
        f"    {'✅' if component_available(camera) else '❌'} 摄像头"
        f" | {camera.get('status', '-') if isinstance(camera, dict) else camera}"
    )
    if isinstance(camera, dict):
        runtime = camera.get("runtime", {})
        backend = runtime.get("backend") or "-"
        last_error = runtime.get("last_error")
        detector_perf = runtime.get("perf", {}) if isinstance(runtime, dict) else {}
        click.echo(f"      backend: {backend}")
        if last_error:
            click.echo(f"      last_error: {last_error}")
        if detector_perf:
            frame_perf = detector_perf.get("frame", {})
            open_perf = detector_perf.get("open_capture", {})
            click.echo(f"      open_capture_ms: {open_perf.get('last_total_ms', 0.0)}")
            click.echo(f"      frame_read_ms: {frame_perf.get('last_read_ms', 0.0)}")
            click.echo(f"      frame_process_ms: {frame_perf.get('last_process_ms', 0.0)}")

    click.echo(
        f"    {'✅' if component_available(safe_window) else '❌'} 安全窗口"
        f" | 主: {safe_window.get('primary', '-') if isinstance(safe_window, dict) else '-'}"
        f" | 备: {safe_window.get('backup', '-') if isinstance(safe_window, dict) else '-'}"
    )
    if isinstance(safe_window, dict):
        safe_status = safe_window.get("status", {})
        if isinstance(safe_status, dict) and safe_status:
            for app_name, available in safe_status.items():
                click.echo(f"      {'✅' if available else '❌'} {app_name}")

    click.echo(
        f"    {'✅' if component_available(action_chain) else '❌'} 动作链路"
        f" | {action_chain.get('status', '-') if isinstance(action_chain, dict) else action_chain}"
    )

    action_perf = perf.get("action_chain", {})
    if action_perf:
        last_switch = action_perf.get("last_switch", {})
        last_minimize = action_perf.get("last_minimize", {})
        click.echo(f"      last_switch_ms: {last_switch.get('total_ms', 0.0)}")
        click.echo(f"      last_minimize_ms: {last_minimize.get('elapsed_ms', 0.0)}")

    if issues:
        click.echo("\n  问题列表:")
        for issue in issues:
            click.echo(f"    ⚠️  {issue}")

    if warnings:
        click.echo("\n  警告列表:")
        for warning in warnings:
            click.echo(f"    ⚠️  {warning}")

    click.echo(f"{'=' * 40}\n")


def format_events(events: list, json_output: bool = False):
    """格式化事件输出"""
    if json_output:
        click.echo(json.dumps(events, indent=2, ensure_ascii=False))
        return

    click.echo(f"\n{'=' * 60}")
    click.echo("  ClawCamKeeper 事件记录")
    click.echo(f"{'=' * 60}")

    if not events:
        click.echo("  暂无事件记录")
    else:
        for event in events:
            ts = event.get("timestamp", "")[:19]
            etype = event.get("event_type", "")
            msg = event.get("message", "")
            click.echo(f"  [{ts}] {etype}: {msg}")

    click.echo(f"{'=' * 60}\n")


def format_notifications(payload: dict, json_output: bool = False):
    """格式化轻量通知输出"""
    if json_output:
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    notifications = payload.get("notifications", []) if isinstance(payload, dict) else []
    latest_id = payload.get("latest_id", 0) if isinstance(payload, dict) else 0
    since_id = payload.get("since_id", 0) if isinstance(payload, dict) else 0

    click.echo(f"\n{'=' * 60}")
    click.echo("  ClawCamKeeper 轻量通知")
    click.echo(f"{'=' * 60}")
    click.echo(f"  since_id: {since_id} | latest_id: {latest_id} | count: {len(notifications)}")

    if not notifications:
        click.echo("  暂无新增通知")
    else:
        for item in notifications:
            ts = item.get("timestamp", "")[:19]
            severity = item.get("severity", "info")
            etype = item.get("event_type", "")
            msg = item.get("message", "")
            click.echo(f"  [{ts}] ({severity}) {etype}: {msg}")

    click.echo(f"{'=' * 60}\n")


@click.group()
@click.option("--config", "-c", default=None, help="配置文件路径")
@click.pass_context
def cli(ctx, config):
    """ClawCamKeeper - 工位摸鱼防护预警技能"""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = str(resolve_config_path(config)) if config else None


@cli.command()
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.pass_context
def status(ctx, json_output):
    """查看当前系统状态"""
    config_path = ctx.obj.get("config_path")
    success, data = api_request("GET", "/status", config_path=config_path)
    if success:
        format_status(data, json_output)
        if not json_output:
            print_perf_summary(data)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误"), "cli_perf": data.get("cli_perf", {})}, indent=2, ensure_ascii=False))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
            print_perf_summary(data)
            click.echo("提示: 服务可能未运行，使用 'clawcamkeeper run' 启动")
        ctx.exit(1)


@cli.command()
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.pass_context
def doctor(ctx, json_output):
    """健康检查"""
    config_path = ctx.obj.get("config_path")
    success, data = api_request("GET", "/doctor", config_path=config_path)
    if success:
        format_doctor(data, json_output)
        if not json_output:
            print_perf_summary(data)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误"), "cli_perf": data.get("cli_perf", {})}, indent=2, ensure_ascii=False))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
            print_perf_summary(data)
        ctx.exit(1)


@cli.command()
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.option("--limit", "-l", default=20, help="显示事件数量")
@click.pass_context
def events(ctx, json_output, limit):
    """查看事件记录"""
    config_path = ctx.obj.get("config_path")
    success, data = api_request("GET", f"/events?limit={limit}", config_path=config_path)
    if success:
        payload = data.get("events", []) if isinstance(data, dict) else []
        format_events(payload, json_output)
        if not json_output:
            print_perf_summary(data)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误"), "cli_perf": data.get("cli_perf", {})}, indent=2, ensure_ascii=False))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
            print_perf_summary(data)
        ctx.exit(1)


@cli.command()
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.option("--since-id", default=0, type=int, show_default=True, help="仅返回大于该 ID 的通知")
@click.option("--limit", "-l", default=20, type=int, show_default=True, help="返回通知数量")
@click.pass_context
def notifications(ctx, json_output, since_id, limit):
    """查看轻量通知队列"""
    config_path = ctx.obj.get("config_path")
    success, data = api_request("GET", f"/notifications?since_id={since_id}&limit={limit}", config_path=config_path)
    if success:
        format_notifications(data, json_output)
        if not json_output:
            print_perf_summary(data)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误"), "cli_perf": data.get("cli_perf", {})}, indent=2, ensure_ascii=False))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
            print_perf_summary(data)
        ctx.exit(1)


@cli.command()
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.pass_context
def arm(ctx, json_output):
    """武装系统"""
    config_path = ctx.obj.get("config_path")
    success, data = api_request("POST", "/arm", config_path=config_path)
    if success:
        if json_output:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            click.echo(f"✅ {data.get('message', '系统已武装')}")
            print_perf_summary(data)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误"), "cli_perf": data.get("cli_perf", {})}, indent=2, ensure_ascii=False))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
            print_perf_summary(data)
        ctx.exit(1)


@cli.command()
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.pass_context
def disarm(ctx, json_output):
    """解除武装"""
    config_path = ctx.obj.get("config_path")
    success, data = api_request("POST", "/disarm", config_path=config_path)
    if success:
        if json_output:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            click.echo(f"✅ {data.get('message', '系统已解除武装')}")
            print_perf_summary(data)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误"), "cli_perf": data.get("cli_perf", {})}, indent=2, ensure_ascii=False))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
            print_perf_summary(data)
        ctx.exit(1)


@cli.command()
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.pass_context
def recover(ctx, json_output):
    """手动恢复系统（从危险锁定状态）"""
    config_path = ctx.obj.get("config_path")
    success, data = api_request("POST", "/recover", config_path=config_path)
    if success:
        if json_output:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            click.echo(f"✅ {data.get('message', '系统已恢复')}")
            print_perf_summary(data)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误"), "cli_perf": data.get("cli_perf", {})}, indent=2, ensure_ascii=False))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
            print_perf_summary(data)
        ctx.exit(1)


@cli.command("action-test")
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.option("--full-check", is_flag=True, help="执行完整检查（含摄像头探测）")
@click.pass_context
def action_test(ctx, json_output, full_check):
    """测试安全窗口切换/风险程序最小化"""
    config_path = ctx.obj.get("config_path")
    suffix = "?full_check=true" if full_check else ""
    success, data = api_request("POST", f"/action-chain/test{suffix}", config_path=config_path, timeout=30)
    if success:
        if json_output:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            click.echo(f"✅ {data.get('message', '测试完成')}")
            timings = data.get("timings", {})
            click.echo(
                f"   模式={data.get('probe_mode', '-')}, 可用性检查={timings.get('availability_refresh_ms', 0.0)}ms, "
                f"切换={timings.get('switch_ms', 0.0)}ms, 最小化={timings.get('minimize_ms', 0.0)}ms, 总计={timings.get('total_ms', 0.0)}ms"
            )
            print_perf_summary(data)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error") or data.get("message", "未知错误"), "response": data}, indent=2, ensure_ascii=False))
        else:
            click.echo(f"❌ {data.get('error') or data.get('message', '未知错误')}")
            if isinstance(data, dict) and data.get("timings"):
                timings = data.get("timings", {})
                click.echo(
                    f"   模式={data.get('probe_mode', '-')}, 可用性检查={timings.get('availability_refresh_ms', 0.0)}ms, "
                    f"切换={timings.get('switch_ms', 0.0)}ms, 最小化={timings.get('minimize_ms', 0.0)}ms, 总计={timings.get('total_ms', 0.0)}ms"
                )
            print_perf_summary(data)
        ctx.exit(1)


@cli.command("notification-test")
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.option("--message", default=None, help="测试消息内容")
@click.option("--severity", default="warning", show_default=True, help="测试消息级别")
@click.option("--event-type", default="notification_test", show_default=True, help="测试事件类型")
@click.pass_context
def notification_test(ctx, json_output, message, severity, event_type):
    """测试主动通知链路，不改变业务状态"""
    config_path = ctx.obj.get("config_path")
    payload = {
        "message": message,
        "severity": severity,
        "event_type": event_type,
    }
    success, data = api_request("POST", "/openclaw/notification-test", data=payload, config_path=config_path, timeout=20)
    if success:
        if json_output:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            click.echo(f"✅ {data.get('message', '通知链路测试完成')}")
            dispatch = data.get("dispatch", {}) if isinstance(data, dict) else {}
            click.echo(
                f"   dispatch={dispatch.get('status') or '-'} | channel={dispatch.get('channel') or '-'} | target={dispatch.get('target') or '-'}"
            )
            print_perf_summary(data)
    else:
        if json_output:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            click.echo(f"❌ {data.get('error') or data.get('message', '未知错误')}")
            print_perf_summary(data)
        ctx.exit(1)


@cli.command("openclaw-context-clear")
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.pass_context
def openclaw_context_clear(ctx, json_output):
    """清空当前 OpenClaw 主动通知上下文"""
    config_path = ctx.obj.get("config_path")
    success, data = api_request("DELETE", "/openclaw/notification-context", timeout=10, config_path=config_path)
    if success:
        if json_output:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            click.echo("✅ OpenClaw 通知上下文已清空")
            print_perf_summary(data)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误"), "cli_perf": data.get("cli_perf", {})}, indent=2, ensure_ascii=False))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
            print_perf_summary(data)
        ctx.exit(1)


@cli.command("config-set")
@click.option("--safe-window", "-s", default=None, help="主安全窗口进程名")
@click.option("--backup-window", "-b", default=None, help="备选安全窗口进程名")
@click.option("--risk-app", "-r", multiple=True, help="风险程序名称（可多次指定）")
@click.option("--set", "sets", multiple=True, help="按 key=value 写入任意配置，例如 webui.port=8765")
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.pass_context
def config_set(ctx, safe_window, backup_window, risk_app, sets, json_output):
    """修改配置"""
    started_at = time.perf_counter()
    config_path = ctx.obj.get("config_path")
    cfg = load_config(config_path)

    changed = False
    applied_changes: list[dict[str, Any]] = []

    if safe_window:
        cfg.setdefault("safe_window", {})["primary"] = safe_window
        applied_changes.append({"key": "safe_window.primary", "value": safe_window})
        changed = True
        if not json_output:
            click.echo(f"📝 主安全窗口: {safe_window}")

    if backup_window:
        cfg.setdefault("safe_window", {})["backup"] = backup_window
        applied_changes.append({"key": "safe_window.backup", "value": backup_window})
        changed = True
        if not json_output:
            click.echo(f"📝 备选安全窗口: {backup_window}")

    if risk_app:
        cfg["risk_apps"] = list(risk_app)
        applied_changes.append({"key": "risk_apps", "value": list(risk_app)})
        changed = True
        if not json_output:
            click.echo(f"📝 风险程序: {', '.join(risk_app)}")

    for item in sets:
        if "=" not in item:
            raise click.BadParameter(f"--set 参数必须是 key=value 形式，收到: {item}")
        key, raw_value = item.split("=", 1)
        normalized_key = key.strip()
        if not normalized_key:
            raise click.BadParameter(f"--set 参数键不能为空，收到: {item}")
        value = _coerce_config_value(raw_value)
        _set_nested_config_value(cfg, normalized_key, value)
        applied_changes.append({"key": normalized_key, "value": value})
        changed = True
        if not json_output:
            click.echo(f"📝 {normalized_key} = {value}")

    if changed:
        normalized_config, saved_path = save_config(cfg, config_path)

        payload = {
            "message": "配置已保存",
            "path": str(saved_path),
            "config": normalized_config,
            "applied_changes": applied_changes,
            "timings": {
                "total_ms": round((time.perf_counter() - started_at) * 1000, 2),
            },
        }
        if json_output:
            click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            click.echo(f"✅ 配置已保存到: {saved_path}")
            click.echo(f"⏱️  本地保存耗时: {payload['timings']['total_ms']}ms")
    else:
        payload = {
            "message": "未修改任何配置",
            "applied_changes": [],
            "timings": {
                "total_ms": round((time.perf_counter() - started_at) * 1000, 2),
            },
        }
        if json_output:
            click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            click.echo("⚠️  未指定任何配置更改")
            click.echo(f"⏱️  本地检查耗时: {payload['timings']['total_ms']}ms")


@cli.command("service-stop")
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
def service_stop(json_output):
    """停止当前受管服务进程"""
    host, port = _collect_runtime_targets()
    record = _read_instance_record()
    result = _terminate_managed_service_group(host, port)
    payload = {
        **result,
        "instance": record,
        "listening_pids": _get_listening_pids(host, port),
    }
    if json_output:
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        prefix = "✅" if result.get("success") else "❌"
        click.echo(f"{prefix} {result.get('message', '服务停止完成')}")
        click.echo(f"   targets={result.get('targets', [])}")
        if payload.get("listening_pids"):
            click.echo(f"   listening_pids={payload.get('listening_pids')}")
    if not result.get("success"):
        raise click.exceptions.Exit(1)


@cli.command("service-restart")
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.option("--config", "service_config", default=None, help="重启时使用的配置文件路径")
@click.option("--no-webui", is_flag=True, help="重启后不启动 WebUI")
def service_restart(json_output, service_config, no_webui):
    """完全重启服务并等待新代码生效；若新实例启动失败则自动回滚旧配置"""
    started_at = time.perf_counter()
    host, port = _collect_runtime_targets(service_config)
    existing = {
        "instance": _read_instance_record(),
        "listening_pids": _get_listening_pids(host, port),
        "managed_pids": _find_managed_service_pids(host, port),
    }
    had_previous_runtime = bool(existing.get("instance") or existing.get("managed_pids") or existing.get("listening_pids"))

    stop_payload = _terminate_managed_service_group(host, port)
    if not stop_payload.get("success"):
        if json_output:
            click.echo(json.dumps(stop_payload, indent=2, ensure_ascii=False))
        else:
            click.echo(f"❌ {stop_payload.get('message', '停止旧实例失败')}")
        raise click.exceptions.Exit(1)

    time.sleep(1.0)
    new_pid, command = _spawn_detached_service(service_config, no_webui)
    ready, status_payload = _wait_for_service_ready(service_config, timeout_s=25.0, expected_pid=new_pid)

    rollback_payload = {
        "attempted": False,
        "success": False,
    }
    final_success = ready
    message = "服务已完全重启并加载新代码"

    if not ready and had_previous_runtime:
        rollback_payload = _rollback_service_start(existing.get("instance"))
        final_success = rollback_payload.get("success", False)
        if final_success:
            message = "新实例启动失败，已自动回滚并恢复旧服务"
        else:
            message = "新实例启动失败，且自动回滚也未恢复服务"
    elif not ready:
        message = "服务已重启进程，但新进程未真正接管监听/健康检查未在超时内通过"

    payload = {
        "success": final_success,
        "message": message,
        "previous_runtime": existing,
        "stop": stop_payload,
        "start": {
            "pid": new_pid,
            "command": command,
            "listening_pids": _get_listening_pids(host, port),
        },
        "status": status_payload,
        "rollback": rollback_payload,
        "timings": {
            "total_ms": round((time.perf_counter() - started_at) * 1000, 2),
        },
    }
    if json_output:
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        prefix = "✅" if final_success else "⚠️"
        click.echo(f"{prefix} {payload['message']}")
        click.echo(f"   new_pid={new_pid}")
        click.echo(f"   listening_pids={payload['start'].get('listening_pids', [])}")
        if rollback_payload.get("attempted"):
            click.echo(
                f"   rollback={'ok' if rollback_payload.get('success') else 'failed'}"
                f" | rollback_pid={rollback_payload.get('pid') or '-'}"
            )
        if isinstance(status_payload, dict):
            click.echo(f"   arm_state={status_payload.get('arm_state') or '-'} | alert_phase={status_payload.get('alert_phase') or '-'}")
    if not final_success:
        raise click.exceptions.Exit(1)


@cli.command()
@click.option("--config", "-c", default=None, help="配置文件路径")
@click.option("--no-webui", is_flag=True, help="不启动 WebUI")
@click.option("--force-replace", is_flag=True, help="若发现旧实例，则先停止旧实例再启动新实例")
def run(config, no_webui, force_replace):
    """运行 ClawCamKeeper 核心服务（默认单实例）"""
    started_at = time.perf_counter()
    resolved_config_path = resolve_config_path(config)
    cfg = load_config(resolved_config_path)

    webui_config = cfg.get("webui", {})
    host = webui_config.get("host", "127.0.0.1")
    port = webui_config.get("port", 8765)

    acquired, existing = _acquire_single_instance_lock(str(resolved_config_path), host, port, no_webui)
    if not acquired:
        existing_pid = int((existing or {}).get("pid") or 0)
        existing_summary = _summarize_process(existing_pid)
        managed_port_open = _is_port_open(host, port) if not no_webui else False

        if force_replace:
            click.echo("♻️  检测到旧实例/旧监听，先清理再替换启动...")
            stop_result = _terminate_managed_service_group(host, port)
            if not stop_result.get("success"):
                click.echo(f"❌ {stop_result.get('message', '旧实例停止失败')}")
                sys.exit(1)
            acquired, existing = _acquire_single_instance_lock(str(resolved_config_path), host, port, no_webui)

        if not acquired:
            click.echo("⚠️  已检测到运行中的 ClawCamKeeper 实例，默认拒绝启动第二个进程")
            click.echo(f"   pid={existing_summary.get('pid')} | alive={existing_summary.get('alive')} | port_open={managed_port_open}")
            click.echo("   如需替换当前实例，请使用: python .\\main.py run --force-replace")
            sys.exit(1)

    atexit.register(_clear_instance_record, os.getpid())

    log_config = cfg.get("logging", {})
    logger.remove()
    logger.add(sys.stderr, level=log_config.get("level", "INFO"))
    logger.add(log_config.get("file", "clawcamkeeper.log"), rotation="10 MB")

    click.echo("🛡️  ClawCamKeeper 启动中...")
    click.echo(f"🔒 单实例保护已启用 | instance_file={_instance_file_path()}")

    engine = MonitorEngine(cfg)

    init_started_at = time.perf_counter()
    success, msg = engine.initialize()
    init_ms = round((time.perf_counter() - init_started_at) * 1000, 2)
    if not success:
        _clear_instance_record(expected_pid=os.getpid())
        click.echo(f"❌ 初始化失败: {msg}")
        click.echo(f"⏱️  初始化耗时: {init_ms}ms")
        sys.exit(1)

    click.echo(f"✅ {msg}")
    click.echo(f"⏱️  初始化耗时: {init_ms}ms")

    if not no_webui:
        import threading

        from webui.app import create_app

        app = create_app(engine, str(resolved_config_path))

        def run_webui():
            import uvicorn

            uvicorn.run(app, host=host, port=port, log_level="warning")

        webui_thread = threading.Thread(target=run_webui, daemon=True, name="WebUIThread")
        webui_thread.start()

        click.echo(f"🌐 WebUI 已启动: http://{host}:{port}")

    click.echo(f"⏱️  服务启动总耗时: {round((time.perf_counter() - started_at) * 1000, 2)}ms")
    click.echo("📷 监控服务运行中... (Ctrl+C 停止)")
    click.echo("💡 使用 'clawcamkeeper arm' 武装系统")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown_started_at = time.perf_counter()
        click.echo("\n👋 正在关闭...")
        engine.shutdown()
        _clear_instance_record(expected_pid=os.getpid())
        click.echo(f"✅ 已安全关闭 (耗时: {round((time.perf_counter() - shutdown_started_at) * 1000, 2)}ms)")


@cli.command()
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.pass_context
def config_show(ctx, json_output):
    """查看当前配置"""
    started_at = time.perf_counter()
    config_path = ctx.obj.get("config_path")
    resolved_path = resolve_config_path(config_path)
    cfg = load_config(resolved_path)
    total_ms = round((time.perf_counter() - started_at) * 1000, 2)

    if json_output:
        payload = dict(cfg)
        payload["timings"] = {"total_ms": total_ms}
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        click.echo(f"\n{'=' * 40}")
        click.echo("  ClawCamKeeper 配置")
        click.echo(f"{'=' * 40}")
        click.echo(f"  配置文件: {resolved_path}")
        sw = cfg.get("safe_window", {})
        click.echo(f"  主安全窗口: {sw.get('primary', '-')}")
        click.echo(f"  备选安全窗口: {sw.get('backup', '-')}")
        click.echo(f"  风险程序: {', '.join(cfg.get('risk_apps', []))}")
        cam = cfg.get("camera", {})
        click.echo(f"  摄像头: 设备{cam.get('device_index', 0)}")
        det = cfg.get("detection", {})
        click.echo(f"  预报警帧数: {det.get('pre_alert_frames', 10)}")
        click.echo(f"  完全报警帧数: {det.get('full_alert_frames', 30)}")
        click.echo(f"  读取耗时: {total_ms}ms")
        click.echo(f"{'=' * 40}\n")


@cli.command("config-reload")
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.pass_context
def config_reload(ctx, json_output):
    """请求运行中的本地服务从磁盘重载配置"""
    config_path = ctx.obj.get("config_path")
    success, data = api_request("POST", "/config/reload", config_path=config_path, timeout=15)
    if success:
        if json_output:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            click.echo("✅ 配置已从磁盘重载")
            changed_keys = data.get("changed_keys", []) if isinstance(data, dict) else []
            if changed_keys:
                click.echo(f"   变更键: {', '.join(changed_keys)}")
            print_perf_summary(data)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误"), "cli_perf": data.get("cli_perf", {})}, indent=2, ensure_ascii=False))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
            print_perf_summary(data)
        ctx.exit(1)


@cli.command("openclaw-context")
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.option("--session-key", default=None, help="OpenClaw session key")
@click.option("--session-label", default=None, help="OpenClaw session label")
@click.option("--channel", default=None, help="OpenClaw channel，如 qqbot / feishu")
@click.option("--target", default=None, help="OpenClaw channel target")
@click.option("--account", default=None, help="OpenClaw channel account")
@click.option("--source", default="openclaw_bridge", show_default=True, help="上下文来源标记")
@click.pass_context
def openclaw_context(ctx, json_output, session_key, session_label, channel, target, account, source):
    """注册当前 OpenClaw 主动通知上下文"""
    config_path = ctx.obj.get("config_path")
    payload = {
        "context": {
            "session_key": session_key,
            "session_label": session_label,
            "channel": channel,
            "target": target,
            "account": account,
            "source": source,
        }
    }
    success, data = api_request(
        "POST",
        "/openclaw/notification-context",
        data=payload,
        timeout=10,
        config_path=config_path,
    )
    if success:
        if json_output:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            context = data.get("context", {}) if isinstance(data, dict) else {}
            click.echo("✅ OpenClaw 通知上下文已注册")
            click.echo(
                f"   channel={context.get('channel') or '-'} | target={context.get('target') or '-'} | "
                f"session_key={context.get('session_key') or '-'} | session_label={context.get('session_label') or '-'}"
            )
            print_perf_summary(data)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误"), "cli_perf": data.get("cli_perf", {})}, indent=2, ensure_ascii=False))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
            print_perf_summary(data)
        ctx.exit(1)


@cli.command("openclaw-context-show")
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.pass_context
def openclaw_context_show(ctx, json_output):
    """查看当前 OpenClaw 主动通知上下文"""
    config_path = ctx.obj.get("config_path")
    success, data = api_request("GET", "/openclaw/notification-context", timeout=10, config_path=config_path)
    if success:
        if json_output:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            context = data.get("context", {}) if isinstance(data, dict) else {}
            dispatch = data.get("dispatch", {}) if isinstance(data, dict) else {}
            click.echo("✅ OpenClaw 通知上下文")
            click.echo(
                f"   active={context.get('active')} | channel={context.get('channel') or '-'} | target={context.get('target') or '-'}"
            )
            click.echo(
                f"   session_key={context.get('session_key') or '-'} | session_label={context.get('session_label') or '-'}"
            )
            click.echo(
                f"   last_dispatch={dispatch.get('status') or '-'} | message={dispatch.get('message') or '-'}"
            )
            print_perf_summary(data)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误"), "cli_perf": data.get("cli_perf", {})}, indent=2, ensure_ascii=False))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
            print_perf_summary(data)
        ctx.exit(1)


cli.add_command(openclaw_bridge)


if __name__ == "__main__":
    cli()

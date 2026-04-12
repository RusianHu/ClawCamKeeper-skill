"""
CLI 命令实现
使用 click 构建命令行接口
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import click
from loguru import logger

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config_manager import load_config, resolve_config_path, save_config
from core.engine import MonitorEngine


# 默认配置路径
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
DEFAULT_API_BASE = "http://127.0.0.1:8765/api"


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


@cli.command()
@click.option("--config", "-c", default=None, help="配置文件路径")
@click.option("--no-webui", is_flag=True, help="不启动 WebUI")
def run(config, no_webui):
    """运行 ClawCamKeeper 核心服务"""
    started_at = time.perf_counter()
    resolved_config_path = resolve_config_path(config)
    cfg = load_config(resolved_config_path)

    log_config = cfg.get("logging", {})
    logger.remove()
    logger.add(sys.stderr, level=log_config.get("level", "INFO"))
    logger.add(log_config.get("file", "clawcamkeeper.log"), rotation="10 MB")

    click.echo("🛡️  ClawCamKeeper 启动中...")

    engine = MonitorEngine(cfg)

    init_started_at = time.perf_counter()
    success, msg = engine.initialize()
    init_ms = round((time.perf_counter() - init_started_at) * 1000, 2)
    if not success:
        click.echo(f"❌ 初始化失败: {msg}")
        click.echo(f"⏱️  初始化耗时: {init_ms}ms")
        sys.exit(1)

    click.echo(f"✅ {msg}")
    click.echo(f"⏱️  初始化耗时: {init_ms}ms")

    if not no_webui:
        import threading

        from webui.app import create_app

        webui_config = cfg.get("webui", {})
        host = webui_config.get("host", "127.0.0.1")
        port = webui_config.get("port", 8765)

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
        click.echo(f"✅ 已安全关闭 (耗时: {round((time.perf_counter() - shutdown_started_at) * 1000, 2)}ms)")


@cli.command()
@click.option("--safe-window", "-s", default=None, help="主安全窗口进程名")
@click.option("--backup-window", "-b", default=None, help="备选安全窗口进程名")
@click.option("--risk-app", "-r", multiple=True, help="风险程序名称（可多次指定）")
@click.option("--json", "-j", "json_output", is_flag=True, help="JSON 格式输出")
@click.pass_context
def config_set(ctx, safe_window, backup_window, risk_app, json_output):
    """修改配置"""
    started_at = time.perf_counter()
    config_path = ctx.obj.get("config_path")
    cfg = load_config(config_path)

    changed = False

    if safe_window:
        cfg.setdefault("safe_window", {})["primary"] = safe_window
        changed = True
        click.echo(f"📝 主安全窗口: {safe_window}")

    if backup_window:
        cfg.setdefault("safe_window", {})["backup"] = backup_window
        changed = True
        click.echo(f"📝 备选安全窗口: {backup_window}")

    if risk_app:
        cfg["risk_apps"] = list(risk_app)
        changed = True
        click.echo(f"📝 风险程序: {', '.join(risk_app)}")

    if changed:
        normalized_config, saved_path = save_config(cfg, config_path)

        payload = {
            "message": "配置已保存",
            "path": str(saved_path),
            "config": normalized_config,
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
            "timings": {
                "total_ms": round((time.perf_counter() - started_at) * 1000, 2),
            },
        }
        if json_output:
            click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            click.echo("⚠️  未指定任何配置更改")
            click.echo(f"⏱️  本地检查耗时: {payload['timings']['total_ms']}ms")


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


if __name__ == "__main__":
    cli()

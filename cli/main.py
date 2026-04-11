"""
CLI 命令实现
使用 click 构建命令行接口
"""

import json
import sys
import os
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

import click
import yaml
from loguru import logger

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.engine import MonitorEngine
from core.state import ArmState


# 默认配置路径
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"

# WebUI API 端点（CLI 通过 WebUI 的 API 与引擎通信）
API_BASE = "http://127.0.0.1:8765/api"


def load_config(config_path: Optional[str] = None) -> dict:
    """加载配置文件"""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        click.echo(f"配置文件不存在: {path}")
        sys.exit(1)
    
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def api_request(method: str, path: str, data: dict = None, timeout: int = 5) -> tuple[bool, dict]:
    """
    发送 API 请求到 WebUI 后端
    Returns: (success, response_data)
    """
    url = f"{API_BASE}{path}"
    
    try:
        if data:
            req_data = json.dumps(data).encode('utf-8')
            req = urllib.request.Request(url, data=req_data, method=method)
            req.add_header('Content-Type', 'application/json')
        else:
            req = urllib.request.Request(url, method=method)
        
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_data = json.loads(resp.read().decode('utf-8'))
            return True, resp_data
    except urllib.error.URLError as e:
        return False, {"error": f"无法连接到服务: {e}"}
    except Exception as e:
        return False, {"error": str(e)}


def format_status(status: dict, json_output: bool = False):
    """格式化状态输出"""
    if json_output:
        click.echo(json.dumps(status, indent=2, ensure_ascii=False))
        return
    
    # 人类可读格式
    arm_state = status.get('arm_state', 'unknown')
    alert_phase = status.get('alert_phase', 'unknown')
    is_protecting = status.get('is_protecting', False)
    is_locked = status.get('is_locked', False)
    
    # 状态图标
    state_icons = {
        'unarmed': '⚪',
        'armed': '🟢',
        'danger_locked': '🔴'
    }
    icon = state_icons.get(arm_state, '❓')
    
    click.echo(f"\n{'='*40}")
    click.echo(f"  ClawCamKeeper 状态")
    click.echo(f"{'='*40}")
    click.echo(f"  武装状态: {icon} {arm_state}")
    click.echo(f"  报警阶段: {alert_phase}")
    click.echo(f"  正在防护: {'是' if is_protecting else '否'}")
    click.echo(f"  危险锁定: {'是' if is_locked else '否'}")
    click.echo(f"{'='*40}\n")


def format_doctor(report: dict, json_output: bool = False):
    """格式化健康检查输出"""
    if json_output:
        click.echo(json.dumps(report, indent=2, ensure_ascii=False))
        return
    
    healthy = report.get('healthy', False)
    issues = report.get('issues', [])
    components = report.get('components', {})
    
    click.echo(f"\n{'='*40}")
    click.echo(f"  ClawCamKeeper 健康检查")
    click.echo(f"{'='*40}")
    click.echo(f"  健康状态: {'✅ 正常' if healthy else '❌ 异常'}")
    click.echo(f"")
    click.echo(f"  组件状态:")
    for comp, available in components.items():
        icon = '✅' if available else '❌'
        click.echo(f"    {icon} {comp}")
    
    if issues:
        click.echo(f"\n  问题列表:")
        for issue in issues:
            click.echo(f"    ⚠️  {issue}")
    
    click.echo(f"{'='*40}\n")


def format_events(events: list, json_output: bool = False):
    """格式化事件输出"""
    if json_output:
        click.echo(json.dumps(events, indent=2, ensure_ascii=False))
        return
    
    click.echo(f"\n{'='*60}")
    click.echo(f"  ClawCamKeeper 事件记录")
    click.echo(f"{'='*60}")
    
    if not events:
        click.echo("  暂无事件记录")
    else:
        for event in events:
            ts = event.get('timestamp', '')[:19]
            etype = event.get('event_type', '')
            msg = event.get('message', '')
            click.echo(f"  [{ts}] {etype}: {msg}")
    
    click.echo(f"{'='*60}\n")


@click.group()
@click.option('--config', '-c', default=None, help='配置文件路径')
@click.pass_context
def cli(ctx, config):
    """ClawCamKeeper - 工位摸鱼防护预警技能"""
    ctx.ensure_object(dict)
    ctx.obj['config_path'] = config


@cli.command()
@click.option('--json', '-j', 'json_output', is_flag=True, help='JSON 格式输出')
@click.pass_context
def status(ctx, json_output):
    """查看当前系统状态"""
    success, data = api_request('GET', '/status')
    if success:
        format_status(data, json_output)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误")}, indent=2))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
            click.echo("提示: 服务可能未运行，使用 'clawcamkeeper run' 启动")
        ctx.exit(1)


@cli.command()
@click.option('--json', '-j', 'json_output', is_flag=True, help='JSON 格式输出')
@click.pass_context
def doctor(ctx, json_output):
    """健康检查"""
    success, data = api_request('GET', '/doctor')
    if success:
        format_doctor(data, json_output)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误")}, indent=2))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
        ctx.exit(1)


@cli.command()
@click.option('--json', '-j', 'json_output', is_flag=True, help='JSON 格式输出')
@click.option('--limit', '-l', default=20, help='显示事件数量')
@click.pass_context
def events(ctx, json_output, limit):
    """查看事件记录"""
    success, data = api_request('GET', f'/events?limit={limit}')
    if success:
        format_events(data.get('events', []), json_output)
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误")}, indent=2))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
        ctx.exit(1)


@cli.command()
@click.option('--json', '-j', 'json_output', is_flag=True, help='JSON 格式输出')
@click.pass_context
def arm(ctx, json_output):
    """武装系统"""
    success, data = api_request('POST', '/arm')
    if success:
        if json_output:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            click.echo(f"✅ {data.get('message', '系统已武装')}")
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误")}, indent=2))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
        ctx.exit(1)


@cli.command()
@click.option('--json', '-j', 'json_output', is_flag=True, help='JSON 格式输出')
@click.pass_context
def disarm(ctx, json_output):
    """解除武装"""
    success, data = api_request('POST', '/disarm')
    if success:
        if json_output:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            click.echo(f"✅ {data.get('message', '系统已解除武装')}")
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误")}, indent=2))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
        ctx.exit(1)


@cli.command()
@click.option('--json', '-j', 'json_output', is_flag=True, help='JSON 格式输出')
@click.pass_context
def recover(ctx, json_output):
    """手动恢复系统（从危险锁定状态）"""
    success, data = api_request('POST', '/recover')
    if success:
        if json_output:
            click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            click.echo(f"✅ {data.get('message', '系统已恢复')}")
    else:
        if json_output:
            click.echo(json.dumps({"error": data.get("error", "未知错误")}, indent=2))
        else:
            click.echo(f"❌ {data.get('error', '未知错误')}")
        ctx.exit(1)


@cli.command()
@click.option('--config', '-c', default=None, help='配置文件路径')
@click.option('--no-webui', is_flag=True, help='不启动 WebUI')
def run(config, no_webui):
    """运行 ClawCamKeeper 核心服务"""
    cfg = load_config(config)
    
    # 配置日志
    log_config = cfg.get('logging', {})
    logger.remove()
    logger.add(sys.stderr, level=log_config.get('level', 'INFO'))
    logger.add(log_config.get('file', 'clawcamkeeper.log'), rotation="10 MB")
    
    click.echo("🛡️  ClawCamKeeper 启动中...")
    
    # 创建引擎
    engine = MonitorEngine(cfg)
    
    # 初始化
    success, msg = engine.initialize()
    if not success:
        click.echo(f"❌ 初始化失败: {msg}")
        sys.exit(1)
    
    click.echo(f"✅ {msg}")
    
    # 启动 WebUI（如果需要）
    if not no_webui:
        from webui.app import create_app
        import threading
        
        webui_config = cfg.get('webui', {})
        host = webui_config.get('host', '127.0.0.1')
        port = webui_config.get('port', 8765)
        
        app = create_app(engine)
        
        # 在后台线程启动 WebUI
        def run_webui():
            import uvicorn
            uvicorn.run(app, host=host, port=port, log_level="warning")
        
        webui_thread = threading.Thread(target=run_webui, daemon=True, name="WebUIThread")
        webui_thread.start()
        
        click.echo(f"🌐 WebUI 已启动: http://{host}:{port}")
    
    click.echo("📷 监控服务运行中... (Ctrl+C 停止)")
    click.echo("💡 使用 'clawcamkeeper arm' 武装系统")
    
    try:
        # 主线程保持运行
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        click.echo("\n👋 正在关闭...")
        engine.shutdown()
        click.echo("✅ 已安全关闭")


@cli.command()
@click.option('--safe-window', '-s', default=None, help='主安全窗口进程名')
@click.option('--backup-window', '-b', default=None, help='备选安全窗口进程名')
@click.option('--risk-app', '-r', multiple=True, help='风险程序名称（可多次指定）')
@click.option('--json', '-j', 'json_output', is_flag=True, help='JSON 格式输出')
@click.pass_context
def config_set(ctx, safe_window, backup_window, risk_app, json_output):
    """修改配置"""
    config_path = ctx.obj.get('config_path')
    cfg = load_config(config_path)
    
    changed = False
    
    if safe_window:
        cfg.setdefault('safe_window', {})['primary'] = safe_window
        changed = True
        click.echo(f"📝 主安全窗口: {safe_window}")
    
    if backup_window:
        cfg.setdefault('safe_window', {})['backup'] = backup_window
        changed = True
        click.echo(f"📝 备选安全窗口: {backup_window}")
    
    if risk_app:
        cfg['risk_apps'] = list(risk_app)
        changed = True
        click.echo(f"📝 风险程序: {', '.join(risk_app)}")
    
    if changed:
        # 保存配置
        path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        with open(path, 'w', encoding='utf-8') as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        
        if json_output:
            click.echo(json.dumps({"message": "配置已保存", "path": str(path)}, indent=2))
        else:
            click.echo(f"✅ 配置已保存到: {path}")
    else:
        if json_output:
            click.echo(json.dumps({"message": "未修改任何配置"}, indent=2))
        else:
            click.echo("⚠️  未指定任何配置更改")


@cli.command()
@click.option('--json', '-j', 'json_output', is_flag=True, help='JSON 格式输出')
@click.pass_context
def config_show(ctx, json_output):
    """查看当前配置"""
    config_path = ctx.obj.get('config_path')
    cfg = load_config(config_path)
    
    if json_output:
        click.echo(json.dumps(cfg, indent=2, ensure_ascii=False))
    else:
        click.echo(f"\n{'='*40}")
        click.echo(f"  ClawCamKeeper 配置")
        click.echo(f"{'='*40}")
        click.echo(f"  配置文件: {DEFAULT_CONFIG_PATH}")
        sw = cfg.get('safe_window', {})
        click.echo(f"  主安全窗口: {sw.get('primary', '-')}")
        click.echo(f"  备选安全窗口: {sw.get('backup', '-')}")
        click.echo(f"  风险程序: {', '.join(cfg.get('risk_apps', []))}")
        cam = cfg.get('camera', {})
        click.echo(f"  摄像头: 设备{cam.get('device_index', 0)}")
        det = cfg.get('detection', {})
        click.echo(f"  预报警帧数: {det.get('pre_alert_frames', 10)}")
        click.echo(f"  完全报警帧数: {det.get('full_alert_frames', 30)}")
        click.echo(f"{'='*40}\n")


if __name__ == '__main__':
    cli()

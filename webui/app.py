"""
WebUI FastAPI 应用
提供 API 端点和前端页面
"""

import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import psutil
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.config_manager import load_config, merge_config, resolve_config_path, save_config
from core.engine import MonitorEngine


def create_app(engine: MonitorEngine, config_path: Optional[str] = None) -> FastAPI:
    """创建 FastAPI 应用实例"""
    app = FastAPI(title="ClawCamKeeper", version="0.1.0")
    app.state.config_path = resolve_config_path(config_path)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def _elapsed_ms(started_at: float) -> float:
        return round((time.perf_counter() - started_at) * 1000, 2)

    def _build_meta(endpoint: str, started_at: float, perf: Optional[dict] = None) -> dict:
        meta_perf = {
            "request_ms": _elapsed_ms(started_at),
        }
        if perf:
            meta_perf.update(perf)
        return {
            "endpoint": endpoint,
            "perf": meta_perf,
        }

    def _with_meta(payload: dict, endpoint: str, started_at: float, perf: Optional[dict] = None) -> dict:
        result = dict(payload)
        result["meta"] = _build_meta(endpoint, started_at, perf)
        return result

    @app.get("/api/status")
    async def api_status():
        """获取当前状态"""
        started_at = time.perf_counter()
        status = engine.get_status()
        return _with_meta(
            status,
            "/api/status",
            started_at,
            {
                "engine_status_ms": status.get("timings", {}).get("total_ms", 0.0),
                "is_protecting": status.get("is_protecting", False),
            },
        )

    @app.get("/api/doctor")
    async def api_doctor():
        """健康检查"""
        started_at = time.perf_counter()
        report = engine.doctor()
        return _with_meta(
            report,
            "/api/doctor",
            started_at,
            {
                "engine_doctor_ms": report.get("timings", {}).get("total_ms", 0.0),
                "healthy": report.get("healthy", False),
            },
        )

    @app.get("/api/events")
    async def api_events(limit: int = 20):
        """获取事件记录"""
        started_at = time.perf_counter()
        events = engine.get_events(limit=limit)
        return _with_meta(
            {"events": events},
            "/api/events",
            started_at,
            {
                "limit": limit,
                "count": len(events),
                "engine_events_ms": engine.get_perf_snapshot().get("engine", {}).get("last_events_ms", 0.0),
            },
        )

    @app.get("/api/notifications")
    async def api_notifications(since_id: int = 0, limit: int = 20):
        """获取轻量通知队列（供 WebUI / OpenClaw 轮询）"""
        started_at = time.perf_counter()
        notifications = engine.get_notifications(since_id=since_id, limit=limit)
        latest_id = notifications[-1]["id"] if notifications else since_id
        return _with_meta(
            {
                "notifications": notifications,
                "since_id": since_id,
                "latest_id": latest_id,
            },
            "/api/notifications",
            started_at,
            {
                "since_id": since_id,
                "limit": limit,
                "count": len(notifications),
            },
        )

    @app.get("/api/openclaw/notification-context")
    async def api_get_openclaw_notification_context():
        """查询当前 OpenClaw 主动通知上下文。"""
        started_at = time.perf_counter()
        context = engine.get_openclaw_notification_context()
        dispatch = engine.get_notification_dispatch_status()
        return _with_meta(
            {
                "success": True,
                "context": context,
                "dispatch": dispatch,
            },
            "/api/openclaw/notification-context",
            started_at,
            {
                "context_active": bool(context.get("active")),
            },
        )

    @app.post("/api/openclaw/notification-context")
    async def api_register_openclaw_notification_context(request: Request):
        """注册当前 OpenClaw 会话/渠道上下文，供后续预警主动回推。"""
        started_at = time.perf_counter()
        try:
            payload = await request.json()
            context_payload = payload.get("context", payload)
            context = engine.register_openclaw_notification_context(context_payload)
            dispatch = engine.get_notification_dispatch_status()
            return _with_meta(
                {
                    "success": True,
                    "message": "OpenClaw 通知上下文已注册",
                    "context": context,
                    "dispatch": dispatch,
                },
                "/api/openclaw/notification-context",
                started_at,
                {
                    "context_active": bool(context.get("active")),
                },
            )
        except Exception as exc:
            return JSONResponse(
                status_code=400,
                content=_with_meta(
                    {
                        "success": False,
                        "error": f"注册 OpenClaw 通知上下文失败: {exc}",
                    },
                    "/api/openclaw/notification-context",
                    started_at,
                ),
            )

    @app.get("/api/processes")
    async def api_get_processes(search: str = ""):
        """获取系统进程列表，支持搜索过滤"""
        started_at = time.perf_counter()
        processes = []
        seen_names = set()
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                proc_info = proc.info
                proc_name = proc_info.get("name", "")
                if proc_name and proc_name not in seen_names:
                    if not search or search.lower() in proc_name.lower():
                        seen_names.add(proc_name)
                        processes.append(
                            {
                                "name": proc_name,
                                "pid": proc_info.get("pid"),
                            }
                        )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        processes.sort(key=lambda x: x["name"].lower())
        return _with_meta(
            {"processes": processes},
            "/api/processes",
            started_at,
            {
                "search": search,
                "count": len(processes),
            },
        )

    @app.get("/api/config")
    async def api_get_config():
        """获取当前配置"""
        started_at = time.perf_counter()
        config_snapshot = engine.get_config()
        return _with_meta(
            {
                "success": True,
                "config": config_snapshot,
                "path": str(app.state.config_path),
            },
            "/api/config",
            started_at,
            {
                "top_level_keys": len(config_snapshot.keys()),
            },
        )

    @app.put("/api/config")
    async def api_update_config(request: Request):
        """保存配置并热加载"""
        started_at = time.perf_counter()
        try:
            payload = await request.json()
            config_payload = payload.get("config", payload)
            merged_config = merge_config(engine.get_config(), config_payload)
            normalized_config, saved_path = save_config(merged_config, app.state.config_path)
            apply_result = engine.reload_config(normalized_config)
            return _with_meta(
                {
                    "success": True,
                    "saved": True,
                    "path": str(saved_path),
                    "config": engine.get_config(),
                    **apply_result,
                },
                "/api/config",
                started_at,
                {
                    "changed_keys_count": len(apply_result.get("changed_keys", [])),
                    "engine_reload_ms": apply_result.get("timings", {}).get("total_ms", 0.0),
                },
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content=_with_meta(
                    {"success": False, "error": str(exc)},
                    "/api/config",
                    started_at,
                ),
            )
        except FileNotFoundError as exc:
            return JSONResponse(
                status_code=404,
                content=_with_meta(
                    {"success": False, "error": str(exc)},
                    "/api/config",
                    started_at,
                ),
            )
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content=_with_meta(
                    {"success": False, "error": f"保存配置失败: {exc}"},
                    "/api/config",
                    started_at,
                ),
            )

    @app.post("/api/config/reload")
    async def api_reload_config_from_disk():
        """从磁盘重新读取配置并热加载"""
        started_at = time.perf_counter()
        try:
            disk_config = load_config(app.state.config_path)
            apply_result = engine.reload_config(disk_config)
            return _with_meta(
                {
                    "success": True,
                    "config": engine.get_config(),
                    "path": str(app.state.config_path),
                    **apply_result,
                },
                "/api/config/reload",
                started_at,
                {
                    "changed_keys_count": len(apply_result.get("changed_keys", [])),
                    "engine_reload_ms": apply_result.get("timings", {}).get("total_ms", 0.0),
                },
            )
        except FileNotFoundError as exc:
            return JSONResponse(
                status_code=404,
                content=_with_meta(
                    {"success": False, "error": str(exc)},
                    "/api/config/reload",
                    started_at,
                ),
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content=_with_meta(
                    {"success": False, "error": str(exc)},
                    "/api/config/reload",
                    started_at,
                ),
            )
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content=_with_meta(
                    {"success": False, "error": f"重载配置失败: {exc}"},
                    "/api/config/reload",
                    started_at,
                ),
            )

    @app.post("/api/arm")
    async def api_arm():
        """武装系统"""
        started_at = time.perf_counter()
        success, msg = engine.arm()
        engine_perf = engine.get_perf_snapshot().get("engine", {})
        if success:
            return _with_meta(
                {"success": True, "message": msg},
                "/api/arm",
                started_at,
                {
                    "engine_arm_ms": engine_perf.get("last_arm_ms", 0.0),
                },
            )
        return JSONResponse(
            status_code=400,
            content=_with_meta(
                {"success": False, "error": msg},
                "/api/arm",
                started_at,
                {
                    "engine_arm_ms": engine_perf.get("last_arm_ms", 0.0),
                },
            ),
        )

    @app.post("/api/disarm")
    async def api_disarm():
        """解除武装"""
        started_at = time.perf_counter()
        success, msg = engine.disarm()
        engine_perf = engine.get_perf_snapshot().get("engine", {})
        if success:
            return _with_meta(
                {"success": True, "message": msg},
                "/api/disarm",
                started_at,
                {
                    "engine_disarm_ms": engine_perf.get("last_disarm_ms", 0.0),
                },
            )
        return JSONResponse(
            status_code=400,
            content=_with_meta(
                {"success": False, "error": msg},
                "/api/disarm",
                started_at,
                {
                    "engine_disarm_ms": engine_perf.get("last_disarm_ms", 0.0),
                },
            ),
        )

    @app.post("/api/recover")
    async def api_recover():
        """手动恢复"""
        started_at = time.perf_counter()
        success, msg = engine.recover()
        engine_perf = engine.get_perf_snapshot().get("engine", {})
        if success:
            return _with_meta(
                {"success": True, "message": msg},
                "/api/recover",
                started_at,
                {
                    "engine_recover_ms": engine_perf.get("last_recover_ms", 0.0),
                },
            )
        return JSONResponse(
            status_code=400,
            content=_with_meta(
                {"success": False, "error": msg},
                "/api/recover",
                started_at,
                {
                    "engine_recover_ms": engine_perf.get("last_recover_ms", 0.0),
                },
            ),
        )

    @app.post("/api/action-chain/test")
    async def api_test_action_chain(full_check: bool = False):
        """手动测试安全窗口切换/风险程序最小化，不改变状态机"""
        started_at = time.perf_counter()
        try:
            result = engine.test_action_chain(full_check=full_check)
            perf = {
                "probe_mode": result.get("probe_mode"),
                "engine_action_test_ms": result.get("timings", {}).get("total_ms", 0.0),
            }
            if result.get("success"):
                return _with_meta(
                    {"success": True, **result},
                    "/api/action-chain/test",
                    started_at,
                    perf,
                )
            return JSONResponse(
                status_code=400,
                content=_with_meta(
                    {"success": False, **result},
                    "/api/action-chain/test",
                    started_at,
                    perf,
                ),
            )
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content=_with_meta(
                    {"success": False, "error": f"动作链测试失败: {exc}"},
                    "/api/action-chain/test",
                    started_at,
                    {"probe_mode": "unknown"},
                ),
            )

    @app.get("/api/frame")
    async def api_frame():
        """获取当前检测帧（JPEG）"""
        started_at = time.perf_counter()
        headers = {
            "X-Endpoint-Latency-Ms": str(_elapsed_ms(started_at)),
            "X-Endpoint-Name": "/api/frame",
        }
        if engine.detector and engine.detector.latest_result:
            result = engine.detector.latest_result
            if result.frame is not None:
                _, buffer = cv2.imencode(".jpg", result.frame)
                headers["X-Endpoint-Latency-Ms"] = str(_elapsed_ms(started_at))
                return Response(content=buffer.tobytes(), media_type="image/jpeg", headers=headers)

        black = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(
            black,
            "No Frame Available",
            (150, 240),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (255, 255, 255),
            2,
        )
        _, buffer = cv2.imencode(".jpg", black)
        headers["X-Endpoint-Latency-Ms"] = str(_elapsed_ms(started_at))
        return Response(content=buffer.tobytes(), media_type="image/jpeg", headers=headers)

    @app.get("/api/stream")
    async def api_stream():
        """MJPEG 视频流"""
        started_at = time.perf_counter()

        async def generate():
            while True:
                if engine.detector and engine.detector.latest_result:
                    result = engine.detector.latest_result
                    if result.frame is not None:
                        _, buffer = cv2.imencode(".jpg", result.frame)
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n\r\n"
                            + buffer.tobytes()
                            + b"\r\n"
                        )
                import asyncio

                await asyncio.sleep(0.1)

        return StreamingResponse(
            generate(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={
                "X-Stream-Init-Ms": str(_elapsed_ms(started_at)),
                "X-Endpoint-Name": "/api/stream",
            },
        )

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """主页面"""
        html_path = Path(__file__).parent / "templates" / "index.html"
        if html_path.exists():
            return html_path.read_text(encoding="utf-8")
        return HTMLResponse(content="<h1>Template not found</h1>")

    return app

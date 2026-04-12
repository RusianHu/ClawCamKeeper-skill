"""
WebUI FastAPI 应用
提供 API 端点和前端页面
"""

from pathlib import Path
from typing import Optional

import cv2
import numpy as np
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

    @app.get("/api/status")
    async def api_status():
        """获取当前状态"""
        return engine.get_status()

    @app.get("/api/doctor")
    async def api_doctor():
        """健康检查"""
        return engine.doctor()

    @app.get("/api/events")
    async def api_events(limit: int = 20):
        """获取事件记录"""
        events = engine.get_events(limit=limit)
        return {"events": events}

    @app.get("/api/config")
    async def api_get_config():
        """获取当前配置"""
        return {
            "success": True,
            "config": engine.get_config(),
            "path": str(app.state.config_path),
        }

    @app.put("/api/config")
    async def api_update_config(request: Request):
        """保存配置并热加载"""
        try:
            payload = await request.json()
            config_payload = payload.get("config", payload)
            merged_config = merge_config(engine.get_config(), config_payload)
            normalized_config, saved_path = save_config(merged_config, app.state.config_path)
            apply_result = engine.reload_config(normalized_config)
            return {
                "success": True,
                "saved": True,
                "path": str(saved_path),
                "config": engine.get_config(),
                **apply_result,
            }
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={"success": False,"error": str(exc)},
            )
        except FileNotFoundError as exc:
            return JSONResponse(
                status_code=404,
                content={"success": False,"error": str(exc)},
            )
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content={"success": False,"error": f"保存配置失败: {exc}"},
            )

    @app.post("/api/config/reload")
    async def api_reload_config_from_disk():
        """从磁盘重新读取配置并热加载"""
        try:
            disk_config = load_config(app.state.config_path)
            apply_result = engine.reload_config(disk_config)
            return {
                "success": True,
                "config": engine.get_config(),
                "path": str(app.state.config_path),
                **apply_result,
            }
        except FileNotFoundError as exc:
            return JSONResponse(
                status_code=404,
                content={"success": False, "error": str(exc)},
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": str(exc)},
            )
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content={"success": False, "error": f"重载配置失败: {exc}"},
            )

    @app.post("/api/arm")
    async def api_arm():
        """武装系统"""
        success, msg = engine.arm()
        if success:
            return {"success": True, "message": msg}
        return JSONResponse(status_code=400, content={"success": False, "error": msg})

    @app.post("/api/disarm")
    async def api_disarm():
        """解除武装"""
        success, msg = engine.disarm()
        if success:
            return {"success": True, "message": msg}
        return JSONResponse(status_code=400, content={"success": False, "error": msg})

    @app.post("/api/recover")
    async def api_recover():
        """手动恢复"""
        success, msg = engine.recover()
        if success:
            return {"success": True, "message": msg}
        return JSONResponse(status_code=400, content={"success": False, "error": msg})

    @app.get("/api/frame")
    async def api_frame():
        """获取当前检测帧（JPEG）"""
        if engine.detector and engine.detector.latest_result:
            result = engine.detector.latest_result
            if result.frame is not None:
                _, buffer = cv2.imencode(".jpg", result.frame)
                return Response(content=buffer.tobytes(), media_type="image/jpeg")

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
        return Response(content=buffer.tobytes(), media_type="image/jpeg")

    @app.get("/api/stream")
    async def api_stream():
        """MJPEG 视频流"""

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
        )

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """主页面"""
        html_path = Path(__file__).parent / "templates" / "index.html"
        if html_path.exists():
            return html_path.read_text(encoding="utf-8")
        return HTMLResponse(content="<h1>Template not found</h1>")

    return app

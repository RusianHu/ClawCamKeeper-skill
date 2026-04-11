"""
WebUI FastAPI 应用
提供 API 端点和前端页面
"""

import base64
import cv2
import numpy as np
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.engine import MonitorEngine
from core.state import ArmState


def create_app(engine: MonitorEngine) -> FastAPI:
    """创建 FastAPI 应用实例"""
    app = FastAPI(title="ClawCamKeeper", version="0.1.0")
    
    # 静态文件
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    
    # ========== API 路由 ==========
    
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
                _, buffer = cv2.imencode('.jpg', result.frame)
                return Response(content=buffer.tobytes(), media_type="image/jpeg")
        
        # 无可用帧，返回黑色图像
        black = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(black, "No Frame Available", (150, 240), 
                   cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        _, buffer = cv2.imencode('.jpg', black)
        return Response(content=buffer.tobytes(), media_type="image/jpeg")
    
    @app.get("/api/stream")
    async def api_stream():
        """MJPEG 视频流"""
        async def generate():
            while True:
                if engine.detector and engine.detector.latest_result:
                    result = engine.detector.latest_result
                    if result.frame is not None:
                        _, buffer = cv2.imencode('.jpg', result.frame)
                        yield (b'--frame\r\n'
                               b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
                import asyncio
                await asyncio.sleep(0.1)
        
        return StreamingResponse(generate(), media_type="multipart/x-mixed-replace; boundary=frame")
    
    # ========== 前端路由 ==========
    
    @app.get("/", response_class=HTMLResponse)
    async def index():
        """主页面"""
        html_path = Path(__file__).parent / "templates" / "index.html"
        if html_path.exists():
            return html_path.read_text(encoding='utf-8')
        return HTMLResponse(content="<h1>Template not found</h1>")
    
    return app


# 需要导入 Response
from fastapi.responses import Response

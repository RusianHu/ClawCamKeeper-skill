"""
动作链路模块
负责窗口切换、焦点切换、风险程序最小化等 Windows 桌面控制
Phase 2 增强：主备窗口自动切换、失效提示、风险程序管理
"""

import ctypes
import time
import shutil
import subprocess
from pathlib import Path
from typing import Optional, List, Dict
from loguru import logger

try:
    import win32api
    import win32gui
    import win32con
    import win32process
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False
    logger.warning("pywin32 未安装，Windows 窗口控制不可用")

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False
    logger.warning("psutil 未安装，将使用受限窗口枚举模式")


class WindowInfo:
    """窗口信息"""

    def __init__(self, hwnd: int, title: str, pid: int, exe_name: str = ""):
        self.hwnd = hwnd
        self.title = title
        self.pid = pid
        self.exe_name = exe_name

    def __repr__(self):
        return f"WindowInfo(hwnd={self.hwnd}, title='{self.title}', pid={self.pid}, exe='{self.exe_name}')"


class ActionChain:
    """
    动作链路
    执行安全窗口切换、焦点切换、风险程序最小化
    Phase 2 增强：主备窗口自动切换、失效提示、风险程序管理
    """

    def __init__(self, config: dict):
        self.config = config
        self.safe_window_config = config.get("safe_window", {})
        self.risk_apps_config = list(config.get("risk_apps", []))

        self.primary_safe_app = self.safe_window_config.get("primary", "notepad.exe")
        self.backup_safe_app = self.safe_window_config.get("backup", "calc.exe")

        self._last_error: Optional[str] = None
        self._safe_window_status: Dict[str, bool] = {}  # 记录每个安全窗口的可用性
        self._action_chain_errors: List[str] = []  # 动作链路错误历史
        self._last_switch_diagnostics: Dict = {}
        self._last_minimize_diagnostics: Dict = {}

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def is_available(self) -> bool:
        """检查动作链路是否可用"""
        return HAS_WIN32

    def _elapsed_ms(self, start: float, end: Optional[float] = None) -> float:
        """将 perf_counter 时间差换算为毫秒"""
        if end is None:
            end = time.perf_counter()
        return round((end - start) * 1000, 2)

    def _serialize_window_info(self, window: Optional[WindowInfo]) -> Optional[dict]:
        """将窗口对象转成可序列化字典"""
        if not window:
            return None

        return {
            "hwnd": window.hwnd,
            "title": window.title,
            "pid": window.pid,
            "exe_name": window.exe_name,
        }

    def _is_app_launchable(self, app_name: str) -> bool:
        """检查应用是否可启动（存在于 PATH 或为有效路径）"""
        if not app_name:
            return False

        candidate = Path(app_name)
        if candidate.exists():
            return True

        resolved = shutil.which(app_name)
        if resolved:
            return True

        logger.warning(f"安全窗口目标不可启动: {app_name}")
        return False

    def find_windows_by_exe(self, exe_name: str) -> List[WindowInfo]:
        """根据进程名查找窗口"""
        if not HAS_WIN32:
            return []

        if not HAS_PSUTIL:
            logger.debug(f"psutil 不可用，无法按进程名精确枚举窗口: {exe_name}")
            return []

        results = []

        def enum_callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                try:
                    process = psutil.Process(pid)
                    if process.name().lower() == exe_name.lower():
                        title = win32gui.GetWindowText(hwnd)
                        results.append(WindowInfo(hwnd, title, pid, process.name()))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return True

        try:
            win32gui.EnumWindows(enum_callback, None)
        except Exception as e:
            logger.error(f"枚举窗口失败: {e}")

        return results

    def find_foreground_window(self) -> Optional[WindowInfo]:
        """获取当前前台窗口"""
        if not HAS_WIN32:
            return None

        try:
            hwnd = win32gui.GetForegroundWindow()
            if hwnd:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                title = win32gui.GetWindowText(hwnd)
                if HAS_PSUTIL:
                    try:
                        process = psutil.Process(pid)
                        return WindowInfo(hwnd, title, pid, process.name())
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        return WindowInfo(hwnd, title, pid, "")
                return WindowInfo(hwnd, title, pid, "")
        except Exception as e:
            logger.error(f"获取前台窗口失败: {e}")
        return None

    def launch_safe_window(self) -> tuple[bool, str]:
        """
        启动安全窗口（主目标优先，备选次之）
        Returns: (success, message)
        """
        if self._launch_app(self.primary_safe_app):
            self._safe_window_status[self.primary_safe_app] = True
            msg = f"已启动主安全窗口: {self.primary_safe_app}"
            logger.info(msg)
            return True, msg

        logger.warning(f"主安全窗口 {self.primary_safe_app} 启动失败，尝试备选...")
        if self._launch_app(self.backup_safe_app):
            self._safe_window_status[self.primary_safe_app] = False
            self._safe_window_status[self.backup_safe_app] = True
            msg = f"主目标不可用，已启动备选安全窗口: {self.backup_safe_app}"
            logger.warning(msg)
            return True, msg

        self._safe_window_status[self.primary_safe_app] = False
        self._safe_window_status[self.backup_safe_app] = False
        self._last_error = f"无法启动任何安全窗口（主: {self.primary_safe_app}, 备: {self.backup_safe_app}）"
        logger.error(self._last_error)
        return False, self._last_error

    def _launch_app(self, app_name: str) -> bool:
        """尝试启动指定应用"""
        try:
            subprocess.Popen(app_name, shell=True)
            time.sleep(0.25)  # 给予进程一个较短的启动缓冲
            return True
        except Exception as e:
            logger.error(f"启动 {app_name} 失败: {e}")
            return False

    def _wait_for_window_details(
        self,
        app_name: str,
        timeout: float = 2.0,
        interval: float = 0.1,
    ) -> tuple[Optional[WindowInfo], Dict]:
        """等待指定应用窗口出现，并返回诊断信息"""
        started_at = time.perf_counter()
        details = {
            "app_name": app_name,
            "timeout_ms": round(timeout * 1000, 2),
            "poll_interval_ms": round(interval * 1000, 2),
            "polls": 0,
            "found": False,
            "window": None,
            "elapsed_ms": 0.0,
        }

        if not HAS_PSUTIL:
            details["reason"] = "psutil_unavailable"
            details["elapsed_ms"] = self._elapsed_ms(started_at)
            return None, details

        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            details["polls"] += 1
            windows = self.find_windows_by_exe(app_name)
            if windows:
                details["found"] = True
                details["window"] = self._serialize_window_info(windows[0])
                details["elapsed_ms"] = self._elapsed_ms(started_at)
                return windows[0], details
            time.sleep(interval)

        details["elapsed_ms"] = self._elapsed_ms(started_at)
        return None, details

    def _wait_for_window(self, app_name: str, timeout: float = 2.0, interval: float = 0.1) -> Optional[WindowInfo]:
        """等待指定应用窗口出现"""
        window, _ = self._wait_for_window_details(app_name, timeout=timeout, interval=interval)
        return window

    def switch_to_safe_window_detailed(self) -> tuple[bool, str, Dict]:
        """切换到安全窗口，并返回详细诊断数据"""
        started_at = time.perf_counter()
        diagnostics = {
            "path": "existing_window",
            "target_app": None,
            "launch_attempted": False,
            "searches": [],
            "waits": [],
            "bring_to_front": [],
            "foreground_before": self._serialize_window_info(self.find_foreground_window()),
            "foreground_after": None,
            "launch_elapsed_ms": 0.0,
            "success": False,
            "error": None,
            "total_ms": 0.0,
        }

        for app_name in [self.primary_safe_app, self.backup_safe_app]:
            search_started_at = time.perf_counter()
            windows = self.find_windows_by_exe(app_name)
            diagnostics["searches"].append(
                {
                    "app_name": app_name,
                    "matches": len(windows),
                    "elapsed_ms": self._elapsed_ms(search_started_at),
                }
            )

            for window in windows:
                success, bring_details = self._bring_window_to_front_detailed(window.hwnd)
                bring_details.update(
                    {
                        "app_name": app_name,
                        "window": self._serialize_window_info(window),
                    }
                )
                diagnostics["bring_to_front"].append(bring_details)
                if success:
                    self._safe_window_status[app_name] = True
                    diagnostics["target_app"] = app_name
                    diagnostics["success"] = True
                    diagnostics["foreground_after"] = self._serialize_window_info(self.find_foreground_window())
                    diagnostics["total_ms"] = self._elapsed_ms(started_at)
                    self._last_switch_diagnostics = diagnostics
                    msg = f"已切换到安全窗口: {app_name}"
                    logger.info(f"{msg}，路径=existing_window，总耗时={diagnostics['total_ms']}ms")
                    return True, msg, diagnostics

        diagnostics["launch_attempted"] = True
        diagnostics["path"] = "launch_and_wait"
        launch_started_at = time.perf_counter()
        success, msg = self.launch_safe_window()
        diagnostics["launch_elapsed_ms"] = self._elapsed_ms(launch_started_at)

        if success:
            if not HAS_PSUTIL:
                diagnostics["success"] = True
                diagnostics["target_app"] = self.primary_safe_app
                diagnostics["foreground_after"] = self._serialize_window_info(self.find_foreground_window())
                diagnostics["total_ms"] = self._elapsed_ms(started_at)
                self._last_switch_diagnostics = diagnostics
                logger.info(f"{msg}，路径=launch_without_psutil，总耗时={diagnostics['total_ms']}ms")
                return True, msg, diagnostics

            for app_name in [self.primary_safe_app, self.backup_safe_app]:
                window, wait_details = self._wait_for_window_details(app_name)
                diagnostics["waits"].append(wait_details)
                if window:
                    success, bring_details = self._bring_window_to_front_detailed(window.hwnd)
                    bring_details.update(
                        {
                            "app_name": app_name,
                            "window": self._serialize_window_info(window),
                        }
                    )
                    diagnostics["bring_to_front"].append(bring_details)
                    if success:
                        self._safe_window_status[app_name] = True
                        diagnostics["target_app"] = app_name
                        diagnostics["success"] = True
                        diagnostics["foreground_after"] = self._serialize_window_info(self.find_foreground_window())
                        diagnostics["total_ms"] = self._elapsed_ms(started_at)
                        self._last_switch_diagnostics = diagnostics
                        success_msg = f"已启动并切换到: {app_name}"
                        logger.info(f"{success_msg}，路径=launch_and_wait，总耗时={diagnostics['total_ms']}ms")
                        return True, success_msg, diagnostics

        self._last_error = "无法切换到安全窗口"
        diagnostics["error"] = self._last_error
        diagnostics["foreground_after"] = self._serialize_window_info(self.find_foreground_window())
        diagnostics["total_ms"] = self._elapsed_ms(started_at)
        self._last_switch_diagnostics = diagnostics
        logger.error(f"无法切换到安全窗口，路径={diagnostics['path']}，总耗时={diagnostics['total_ms']}ms")
        return False, self._last_error, diagnostics

    def switch_to_safe_window(self) -> tuple[bool, str]:
        """
        切换到安全窗口
        Phase 2: 支持主备自动切换，返回详细状态信息
        Returns: (success, message)
        """
        success, msg, _ = self.switch_to_safe_window_detailed()
        return success, msg

    def _bring_window_to_front_detailed(self, hwnd: int) -> tuple[bool, Dict]:
        """将窗口带到前台，并返回详细诊断信息"""
        started_at = time.perf_counter()
        details = {
            "hwnd": hwnd,
            "success": False,
            "attempts": [],
            "error": None,
            "elapsed_ms": 0.0,
            "final_foreground_hwnd": None,
        }

        if not HAS_WIN32:
            details["error"] = "win32_unavailable"
            details["elapsed_ms"] = self._elapsed_ms(started_at)
            return False, details

        if not win32gui.IsWindow(hwnd):
            details["error"] = f"无效窗口句柄 {hwnd}"
            details["elapsed_ms"] = self._elapsed_ms(started_at)
            logger.error(f"切换窗口失败: {details['error']}")
            return False, details

        user32 = ctypes.windll.user32
        last_error = None
        topmost_settle = 0.02
        alt_key_settle = 0.01
        foreground_wait_base = 0.03
        retry_wait_base = 0.04

        for attempt in range(1, 5):
            attempt_started_at = time.perf_counter()
            foreground_hwnd = None
            foreground_thread_id = None
            target_thread_id = None
            attached = False
            attempt_info = {
                "attempt": attempt,
                "attached_input": False,
                "success": False,
                "elapsed_ms": 0.0,
                "sleep_after_foreground_ms": round(foreground_wait_base * attempt * 1000, 2),
                "sleep_before_retry_ms": round(retry_wait_base * attempt * 1000, 2),
            }

            try:
                foreground_hwnd = win32gui.GetForegroundWindow()
                attempt_info["foreground_before_hwnd"] = foreground_hwnd
                if foreground_hwnd and win32gui.IsWindow(foreground_hwnd):
                    foreground_thread_id = win32process.GetWindowThreadProcessId(foreground_hwnd)[0]
                target_thread_id = win32process.GetWindowThreadProcessId(hwnd)[0]

                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    attempt_info["restored"] = True
                else:
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                    attempt_info["restored"] = False

                win32gui.BringWindowToTop(hwnd)
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_TOPMOST,
                    0,
                    0,
                    0,
                    0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )
                time.sleep(topmost_settle)
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_NOTOPMOST,
                    0,
                    0,
                    0,
                    0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )

                if foreground_thread_id and target_thread_id and foreground_thread_id != target_thread_id:
                    win32process.AttachThreadInput(foreground_thread_id, target_thread_id, True)
                    attached = True
                    attempt_info["attached_input"] = True

                user32.keybd_event(win32con.VK_MENU, 0, 0, 0)
                user32.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(alt_key_settle)

                try:
                    win32gui.SetActiveWindow(hwnd)
                except Exception:
                    pass

                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception as e:
                    last_error = e
                    attempt_info["set_foreground_error"] = str(e)

                user32.SetForegroundWindow(hwnd)
                time.sleep(foreground_wait_base * attempt)

                current_foreground = win32gui.GetForegroundWindow()
                attempt_info["foreground_after_hwnd"] = current_foreground
                attempt_info["success"] = current_foreground == hwnd
                attempt_info["elapsed_ms"] = self._elapsed_ms(attempt_started_at)
                details["attempts"].append(attempt_info)

                if attempt_info["success"]:
                    details["success"] = True
                    details["final_foreground_hwnd"] = current_foreground
                    details["elapsed_ms"] = self._elapsed_ms(started_at)
                    return True, details
            except Exception as e:
                last_error = e
                attempt_info["error"] = str(e)
                attempt_info["elapsed_ms"] = self._elapsed_ms(attempt_started_at)
                details["attempts"].append(attempt_info)
            finally:
                if attached and foreground_thread_id and target_thread_id:
                    try:
                        win32process.AttachThreadInput(foreground_thread_id, target_thread_id, False)
                    except Exception:
                        pass

            time.sleep(retry_wait_base * attempt)

        details["error"] = str(last_error) if last_error else "unknown_error"
        try:
            details["final_foreground_hwnd"] = win32gui.GetForegroundWindow()
        except Exception:
            details["final_foreground_hwnd"] = None
        details["elapsed_ms"] = self._elapsed_ms(started_at)
        logger.error(f"切换窗口失败: {details['error']}")
        return False, details

    def _bring_window_to_front(self, hwnd: int) -> bool:
        """将窗口带到前台"""
        success, _ = self._bring_window_to_front_detailed(hwnd)
        return success

    def minimize_risk_apps_with_details(self) -> tuple[int, Dict]:
        """最小化所有风险程序窗口，并返回详细诊断信息"""
        started_at = time.perf_counter()
        details = {
            "apps": [],
            "minimized_count": 0,
            "elapsed_ms": 0.0,
        }

        if not HAS_WIN32:
            details["reason"] = "win32_unavailable"
            details["elapsed_ms"] = self._elapsed_ms(started_at)
            self._last_minimize_diagnostics = details
            return 0, details

        minimized_count = 0
        for app_name in self.risk_apps_config:
            app_started_at = time.perf_counter()
            app_details = {
                "app_name": app_name,
                "windows_found": 0,
                "minimized": 0,
                "errors": [],
                "elapsed_ms": 0.0,
            }
            windows = self.find_windows_by_exe(app_name)
            app_details["windows_found"] = len(windows)
            for window in windows:
                try:
                    win32gui.ShowWindow(window.hwnd, win32con.SW_MINIMIZE)
                    minimized_count += 1
                    app_details["minimized"] += 1
                    logger.info(f"已最小化风险程序: {app_name}")
                except Exception as e:
                    logger.error(f"最小化 {app_name} 失败: {e}")
                    app_details["errors"].append(str(e))
            app_details["elapsed_ms"] = self._elapsed_ms(app_started_at)
            details["apps"].append(app_details)

        details["minimized_count"] = minimized_count
        details["elapsed_ms"] = self._elapsed_ms(started_at)
        self._last_minimize_diagnostics = details
        logger.info(f"风险程序最小化完成: 数量={minimized_count}, 总耗时={details['elapsed_ms']}ms")
        return minimized_count, details

    def minimize_risk_apps(self) -> int:
        """最小化所有风险程序窗口"""
        minimized_count, _ = self.minimize_risk_apps_with_details()
        return minimized_count

    def execute_full_alert(self) -> dict:
        """
        执行完整的报警动作链
        Phase 2: 增强错误报告和主备切换
        Returns: 动作执行结果
        """
        started_at = time.perf_counter()
        result = {
            "success": False,
            "safe_window_switched": False,
            "safe_window_used": None,
            "risk_apps_minimized": 0,
            "errors": [],
            "warnings": [],
            "timings": {
                "switch_ms": 0.0,
                "minimize_ms": 0.0,
                "total_ms": 0.0,
            },
            "diagnostics": {
                "switch": {},
                "minimize": {},
            },
        }

        success, msg, switch_details = self.switch_to_safe_window_detailed()
        result["diagnostics"]["switch"] = switch_details
        result["timings"]["switch_ms"] = switch_details.get("total_ms", 0.0)
        if success:
            result["safe_window_switched"] = True
            result["safe_window_used"] = switch_details.get("target_app")
            if (
                result["safe_window_used"] == self.backup_safe_app
                and self.backup_safe_app != self.primary_safe_app
            ):
                result["warnings"].append(f"主安全窗口不可用，使用备选: {self.backup_safe_app}")
        else:
            result["errors"].append(msg or "安全窗口切换失败")

        minimized_count, minimize_details = self.minimize_risk_apps_with_details()
        result["risk_apps_minimized"] = minimized_count
        result["diagnostics"]["minimize"] = minimize_details
        result["timings"]["minimize_ms"] = minimize_details.get("elapsed_ms", 0.0)

        result["success"] = result["safe_window_switched"]
        result["timings"]["total_ms"] = self._elapsed_ms(started_at)

        if result["success"]:
            logger.info(
                f"报警动作链执行成功: 使用窗口={result['safe_window_used']}, "
                f"最小化风险程序={result['risk_apps_minimized']}, "
                f"切窗={result['timings']['switch_ms']}ms, "
                f"最小化={result['timings']['minimize_ms']}ms, "
                f"总计={result['timings']['total_ms']}ms"
            )
        else:
            logger.error(f"报警动作链执行失败: {result['errors']}")
            self._action_chain_errors.extend(result["errors"])

        return result

    def check_safe_window_available(self) -> bool:
        """
        检查安全窗口是否可用
        Phase 2: 检查主备至少一个可用（已运行或至少可启动）
        """
        self._safe_window_status[self.primary_safe_app] = False
        self._safe_window_status[self.backup_safe_app] = False

        if not HAS_WIN32:
            logger.debug("pywin32 不可用，安全窗口链路不可用")
            self._last_error = "Windows 窗口控制不可用，无法切换安全窗口"
            return False

        if HAS_PSUTIL:
            for app_name in [self.primary_safe_app, self.backup_safe_app]:
                windows = self.find_windows_by_exe(app_name)
                if windows:
                    self._safe_window_status[app_name] = True

        if any(self._safe_window_status.values()):
            self._last_error = None
            return True

        launchable = any(
            self._is_app_launchable(app_name)
            for app_name in [self.primary_safe_app, self.backup_safe_app]
        )
        if launchable:
            self._last_error = None
            if not HAS_PSUTIL:
                logger.debug("psutil 不可用，未探测运行中窗口，按可启动降级模式处理安全窗口")
            return True

        self._last_error = f"主备安全窗口均不可用（主: {self.primary_safe_app}, 备: {self.backup_safe_app}）"
        logger.error(self._last_error)
        return False

    def get_safe_window_status(self) -> Dict[str, bool]:
        """获取安全窗口状态"""
        return self._safe_window_status.copy()

    def get_action_chain_errors(self) -> List[str]:
        """获取动作链路错误历史"""
        return self._action_chain_errors.copy()

    def get_last_switch_diagnostics(self) -> Dict:
        """获取最近一次切窗诊断信息"""
        return dict(self._last_switch_diagnostics)

    def get_last_minimize_diagnostics(self) -> Dict:
        """获取最近一次最小化诊断信息"""
        return dict(self._last_minimize_diagnostics)

    def set_risk_apps(self, apps: List[str]):
        """设置风险程序列表"""
        self.risk_apps_config = list(apps)
        logger.info(f"风险程序列表已更新: {apps}")

    def add_risk_app(self, app: str):
        """添加风险程序"""
        if app not in self.risk_apps_config:
            self.risk_apps_config.append(app)
            logger.info(f"已添加风险程序: {app}")

    def remove_risk_app(self, app: str):
        """移除风险程序"""
        if app in self.risk_apps_config:
            self.risk_apps_config.remove(app)
            logger.info(f"已移除风险程序: {app}")

    def get_risk_apps(self) -> List[str]:
        """获取风险程序列表"""
        return self.risk_apps_config.copy()

    def check_action_chain_health(self) -> Dict:
        """
        检查动作链路健康状态
        Returns: 健康报告
        """
        report = {
            "healthy": True,
            "issues": [],
            "safe_windows": self.get_safe_window_status(),
            "risk_apps_count": len(self.risk_apps_config),
            "recent_errors": self.get_action_chain_errors()[-5:]
        }

        if not self.is_available():
            report["healthy"] = False
            report["issues"].append("Windows 窗口控制不可用（pywin32 未安装）")
            return report

        if not HAS_PSUTIL:
            logger.debug("psutil 不可用，动作链路健康检查按降级模式判定为可用")
            return report

        primary_ok = self._safe_window_status.get(self.primary_safe_app, False)
        backup_ok = self._safe_window_status.get(self.backup_safe_app, False)

        if not primary_ok and not backup_ok:
            report["healthy"] = False
            report["issues"].append(f"主备安全窗口均不可用（主: {self.primary_safe_app}, 备: {self.backup_safe_app}）")
        elif not primary_ok:
            report["issues"].append(f"主安全窗口不可用，使用备选: {self.backup_safe_app}")

        return report

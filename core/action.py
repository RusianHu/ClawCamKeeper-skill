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
        self.safe_window_config = config.get('safe_window', {})
        self.risk_apps_config = list(config.get('risk_apps', []))
        
        self.primary_safe_app = self.safe_window_config.get('primary', 'notepad.exe')
        self.backup_safe_app = self.safe_window_config.get('backup', 'calc.exe')
        
        self._last_error: Optional[str] = None
        self._safe_window_status: Dict[str, bool] = {}  # 记录每个安全窗口的可用性
        self._action_chain_errors: List[str] = []  # 动作链路错误历史

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def is_available(self) -> bool:
        """检查动作链路是否可用"""
        return HAS_WIN32

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
        # 尝试主目标
        if self._launch_app(self.primary_safe_app):
            self._safe_window_status[self.primary_safe_app] = True
            msg = f"已启动主安全窗口: {self.primary_safe_app}"
            logger.info(msg)
            return True, msg
        
        # 主目标失败，尝试备选
        logger.warning(f"主安全窗口 {self.primary_safe_app} 启动失败，尝试备选...")
        if self._launch_app(self.backup_safe_app):
            self._safe_window_status[self.primary_safe_app] = False
            self._safe_window_status[self.backup_safe_app] = True
            msg = f"主目标不可用，已启动备选安全窗口: {self.backup_safe_app}"
            logger.warning(msg)
            return True, msg
        
        # 都失败
        self._safe_window_status[self.primary_safe_app] = False
        self._safe_window_status[self.backup_safe_app] = False
        self._last_error = f"无法启动任何安全窗口（主: {self.primary_safe_app}, 备: {self.backup_safe_app}）"
        logger.error(self._last_error)
        return False, self._last_error

    def _launch_app(self, app_name: str) -> bool:
        """尝试启动指定应用"""
        try:
            subprocess.Popen(app_name, shell=True)
            time.sleep(0.5)  # 等待窗口出现
            return True
        except Exception as e:
            logger.error(f"启动 {app_name} 失败: {e}")
            return False

    def _wait_for_window(self, app_name: str, timeout: float = 3.0, interval: float = 0.2) -> Optional[WindowInfo]:
        """等待指定应用窗口出现"""
        if not HAS_PSUTIL:
            return None

        deadline = time.time() + timeout
        while time.time() < deadline:
            windows = self.find_windows_by_exe(app_name)
            if windows:
                return windows[0]
            time.sleep(interval)
        return None

    def switch_to_safe_window(self) -> tuple[bool, str]:
        """
        切换到安全窗口
        Phase 2: 支持主备自动切换，返回详细状态信息
        Returns: (success, message)
        """
        # 先尝试查找已存在的安全窗口（优先主目标）
        for app_name in [self.primary_safe_app, self.backup_safe_app]:
            windows = self.find_windows_by_exe(app_name)
            for window in windows:
                if self._bring_window_to_front(window.hwnd):
                    self._safe_window_status[app_name] = True
                    msg = f"已切换到安全窗口: {app_name}"
                    logger.info(msg)
                    return True, msg
        
        # 如果没有找到，启动新的
        success, msg = self.launch_safe_window()
        if success:
            if not HAS_PSUTIL:
                logger.debug("psutil 不可用，无法验证窗口句柄，按应用已成功启动处理")
                return True, msg

            # 启动后等待窗口句柄出现并重试切换
            for app_name in [self.primary_safe_app, self.backup_safe_app]:
                window = self._wait_for_window(app_name)
                if window and self._bring_window_to_front(window.hwnd):
                    self._safe_window_status[app_name] = True
                    return True, f"已启动并切换到: {app_name}"
        
        self._last_error = "无法切换到安全窗口"
        return False, self._last_error

    def _bring_window_to_front(self, hwnd: int) -> bool:
        """将窗口带到前台"""
        if not HAS_WIN32:
            return False

        if not win32gui.IsWindow(hwnd):
            logger.error(f"切换窗口失败: 无效窗口句柄 {hwnd}")
            return False

        user32 = ctypes.windll.user32
        last_error = None

        for attempt in range(1, 5):
            foreground_hwnd = None
            foreground_thread_id = None
            target_thread_id = None
            attached = False

            try:
                foreground_hwnd = win32gui.GetForegroundWindow()
                if foreground_hwnd and win32gui.IsWindow(foreground_hwnd):
                    foreground_thread_id = win32process.GetWindowThreadProcessId(foreground_hwnd)[0]
                target_thread_id = win32process.GetWindowThreadProcessId(hwnd)[0]

                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                else:
                    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

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
                time.sleep(0.05)
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

                user32.keybd_event(win32con.VK_MENU, 0, 0, 0)
                user32.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                time.sleep(0.02)

                try:
                    win32gui.SetActiveWindow(hwnd)
                except Exception:
                    pass

                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception as e:
                    last_error = e

                user32.SetForegroundWindow(hwnd)
                time.sleep(0.1 * attempt)

                if win32gui.GetForegroundWindow() == hwnd:
                    return True
            except Exception as e:
                last_error = e
            finally:
                if attached and foreground_thread_id and target_thread_id:
                    try:
                        win32process.AttachThreadInput(foreground_thread_id, target_thread_id, False)
                    except Exception:
                        pass

            time.sleep(0.15 * attempt)

        logger.error(f"切换窗口失败: {last_error}")
        return False

    def minimize_risk_apps(self) -> int:
        """最小化所有风险程序窗口"""
        if not HAS_WIN32:
            return 0
        
        minimized_count = 0
        for app_name in self.risk_apps_config:
            windows = self.find_windows_by_exe(app_name)
            for window in windows:
                try:
                    win32gui.ShowWindow(window.hwnd, win32con.SW_MINIMIZE)
                    minimized_count += 1
                    logger.info(f"已最小化风险程序: {app_name}")
                except Exception as e:
                    logger.error(f"最小化 {app_name} 失败: {e}")
        
        return minimized_count

    def execute_full_alert(self) -> dict:
        """
        执行完整的报警动作链
        Phase 2: 增强错误报告和主备切换
        Returns: 动作执行结果
        """
        result = {
            "success": False,
            "safe_window_switched": False,
            "safe_window_used": None,
            "risk_apps_minimized": 0,
            "errors": [],
            "warnings": []
        }
        
        # 1. 切换输入焦点到安全窗口
        success, msg = self.switch_to_safe_window()
        if success:
            result["safe_window_switched"] = True
            # 判断使用的是主还是备
            for app_name in [self.primary_safe_app, self.backup_safe_app]:
                windows = self.find_windows_by_exe(app_name)
                if windows:
                    result["safe_window_used"] = app_name
                    break
            if "备选" in msg or "备" in msg:
                result["warnings"].append(f"主安全窗口不可用，使用备选: {self.backup_safe_app}")
        else:
            result["errors"].append(msg or "安全窗口切换失败")
        
        # 2. 最小化风险程序
        result["risk_apps_minimized"] = self.minimize_risk_apps()
        
        # 判断整体成功
        result["success"] = result["safe_window_switched"]
        
        if result["success"]:
            logger.info(f"报警动作链执行成功: 使用窗口={result['safe_window_used']}, "
                       f"最小化风险程序={result['risk_apps_minimized']}")
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
        
        # 检查主备安全窗口
        primary_ok = self._safe_window_status.get(self.primary_safe_app, False)
        backup_ok = self._safe_window_status.get(self.backup_safe_app, False)
        
        if not primary_ok and not backup_ok:
            report["healthy"] = False
            report["issues"].append(f"主备安全窗口均不可用（主: {self.primary_safe_app}, 备: {self.backup_safe_app}）")
        elif not primary_ok:
            report["issues"].append(f"主安全窗口不可用，使用备选: {self.backup_safe_app}")
        
        return report

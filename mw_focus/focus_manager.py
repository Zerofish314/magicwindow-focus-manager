"""焦点管理器（被动服务）：观测 + 执行 + 归因 + 日志。

- 观测：可见窗口快照 + 前台窗口轮询；
- 执行：set_foreground（四级回退）/ minimize / pseudo_minimize（预留）；
- 归因：低级键鼠钩子靠 INJECTED 标志区分 用户/自动化，自己调原语记 self；
- 日志：焦点变化事件写 focus_events.jsonl，同时进内存队列供 GUI 实时显示。

本模块不含任何策略（暂停/恢复/回切由调用方实现）。
"""
from __future__ import annotations

import ctypes
import json
import queue
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import win32

user32 = ctypes.windll.user32

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
GW_OWNER = 4
GA_ROOT = 2


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    process: str          # 可执行文件路径，取不到为 ""
    title: str
    minimized: bool
    rect: tuple[int, int, int, int]


@dataclass
class FocusEvent:
    ts: float
    type: str             # foreground_changed / input / window_opened / action
    hwnd: int | None
    process: str
    source: str           # user / self / automation / unknown
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": time.strftime("%H:%M:%S", time.localtime(self.ts)),
            "type": self.type,
            "hwnd": self.hwnd,
            "process": self.process,
            "source": self.source,
            "detail": self.detail,
        }


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", _POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

# CallNextHookEx 的 lParam 是指针宽整数，不显式声明 argtypes 会按 32 位 int 转换而溢出
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_long


class FocusManager:
    """焦点管理器。start() 开两个后台线程，stop() 回收。"""

    def __init__(self, poll_interval_ms: int = 800, run_dir: Path | None = None) -> None:
        self.poll_interval_ms = poll_interval_ms
        self.run_dir = run_dir
        self.events: queue.Queue[FocusEvent] = queue.Queue()
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._hook_thread: threading.Thread | None = None
        self._hooks: list[Any] = []           # 保活回调引用
        self._last_foreground: int = 0
        self._recent_inputs: list[tuple[float, str]] = []  # (时间, user/automation)
        self.input_counts = {"user": 0, "automation": 0}  # 累计计数，供测试与 GUI 观察
        self._self_marks: list[float] = []    # 自己调 set_foreground 的时间
        self._known_windows: set[int] = set()
        self._lock = threading.Lock()

    # ---------------- 观测 ----------------

    def snapshot(self) -> list[WindowInfo]:
        """当前所有可见顶层窗口。"""
        result: list[WindowInfo] = []
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd: int, _l: int) -> bool:
            if user32.IsWindowVisible(hwnd) and user32.GetWindow(hwnd, GW_OWNER) == 0:
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                name = win32.pid_process_name(pid.value) or ""
                title_len = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(title_len + 1)
                user32.GetWindowTextW(hwnd, buf, title_len + 1)
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                result.append(WindowInfo(
                    hwnd=hwnd, pid=pid.value, process=name,
                    title=buf.value,
                    minimized=user32.IsIconic(hwnd) != 0,
                    rect=(rect.left, rect.top, rect.right, rect.bottom),
                ))
            return True

        user32.EnumWindows(enum_proc(callback), 0)
        return result

    def foreground(self) -> WindowInfo | None:
        """当前前台窗口，没有（如锁屏）返回 None。"""
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        for info in self.snapshot():
            if info.hwnd == hwnd:
                return info
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return WindowInfo(hwnd=hwnd, pid=pid.value,
                          process=win32.pid_process_name(pid.value) or "",
                          title="", minimized=False, rect=(0, 0, 0, 0))

    def find_game_window(self, process_names: list[str]) -> int | None:
        """按进程名（可执行文件名，如 ZenlessZoneZero.exe）找可见主窗口。"""
        wanted = {name.lower() for name in process_names}
        best: WindowInfo | None = None
        for info in self.snapshot():
            exe = Path(info.process).name.lower()
            if exe in wanted and info.title:
                # 取面积最大的那个窗口，避开启动器小窗
                area = (info.rect[2] - info.rect[0]) * (info.rect[3] - info.rect[1])
                if best is None:
                    best = info
                else:
                    best_area = (best.rect[2] - best.rect[0]) * (best.rect[3] - best.rect[1])
                    if area > best_area:
                        best = info
        return best.hwnd if best else None

    # ---------------- 执行 ----------------

    def set_foreground(self, hwnd: int) -> bool:
        """四级回退切前台。幂等：已在前景直接返回 True。"""
        if user32.GetForegroundWindow() == hwnd and user32.GetForegroundWindow():
            return True
        with self._lock:
            self._self_marks.append(time.time())
        if user32.SetForegroundWindow(hwnd):
            return True
        # 第二级：绑到当前前台线程再 Set
        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        self_thread = kernel32_Get_current_thread_id()
        if user32.AttachThreadInput(self_thread, fg_thread, True):
            user32.SetForegroundWindow(hwnd)
            user32.AttachThreadInput(self_thread, fg_thread, False)
            if user32.GetForegroundWindow() == hwnd:
                return True
        # 第三级：模拟一次 Alt 键唤醒前台切换许可
        user32.keybd_event(0x12, 0, 0, 0)      # Alt down
        user32.keybd_event(0x12, 0, 2, 0)      # Alt up
        user32.SetForegroundWindow(hwnd)
        if user32.GetForegroundWindow() == hwnd:
            return True
        # 第四级：SwitchToThisWindow
        user32.SwitchToThisWindow(hwnd, True)
        return user32.GetForegroundWindow() == hwnd

    def minimize(self, hwnd: int) -> bool:
        """最小化窗口，返回是否成功。"""
        with self._lock:
            self._self_marks.append(time.time())
        return bool(user32.ShowWindow(hwnd, win32.SW_MINIMIZE))

    def pseudo_minimize(self, hwnd: int) -> None:
        """伪最小化（设备层方向），本期不实现。"""
        raise NotImplementedError("伪最小化属设备层方向，未实现")

    # ---------------- 归因 ----------------

    def _record_input(self, source: str) -> None:
        with self._lock:
            self.input_counts[source] += 1
            self._recent_inputs.append((time.time(), source))
            self._recent_inputs = self._recent_inputs[-20:]

    def _classify(self) -> str:
        """对一次前台变化归因：最近的输入/自操作里谁最可疑。"""
        now = time.time()
        with self._lock:
            inputs = [(t, s) for t, s in self._recent_inputs if now - t < 2.0]
            self_marks = [t for t in self._self_marks if now - t < 2.0]
        if any(s == "user" for _, s in inputs):
            return "user"
        if self_marks:
            return "self"
        if inputs:
            return "automation"
        return "unknown"

    # ---------------- 后台线程 ----------------

    def start(self) -> None:
        self._stop.clear()
        self._last_foreground = user32.GetForegroundWindow()
        self._known_windows = {w.hwnd for w in self.snapshot()}
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True,
                                             name="fm-poll")
        self._poll_thread.start()
        self._hook_thread = threading.Thread(target=self._hook_loop, daemon=True,
                                             name="fm-hooks")
        self._hook_thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(self.poll_interval_ms / 1000)

    def _tick(self) -> None:
        fg = user32.GetForegroundWindow()
        if fg != self._last_foreground:
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            process = win32.pid_process_name(pid.value) or ""
            event = FocusEvent(ts=time.time(), type="foreground_changed",
                               hwnd=fg, process=process, source=self._classify())
            self._emit(event)
            self._last_foreground = fg
        # 新出现的可见窗口
        current = {w.hwnd for w in self.snapshot()}
        for hwnd in current - self._known_windows:
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            self._emit(FocusEvent(ts=time.time(), type="window_opened",
                                  hwnd=hwnd,
                                  process=win32.pid_process_name(pid.value) or "",
                                  source=self._classify()))
        self._known_windows |= current

    def _hook_loop(self) -> None:
        """装低级键鼠钩子并跑消息泵。装不上（杀软拦截）就静默降级。"""
        key_cb = _HOOKPROC(self._kbd_proc)
        mouse_cb = _HOOKPROC(self._mouse_proc)
        self._hooks = [key_cb, mouse_cb]
        key_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, key_cb, None, 0)
        mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, mouse_cb, None, 0)
        if not key_hook and not mouse_hook:
            self._emit(FocusEvent(ts=time.time(), type="input", hwnd=None,
                                  process="", source="unknown",
                                  detail="低级钩子安装失败，事件归因将降级为 unknown"))
            return
        msg = wintypes.MSG()
        # 消息泵：GetMessageW 阻塞直到 WM_QUIT
        while not self._stop.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if key_hook:
            user32.UnhookWindowsHookEx(key_hook)
        if mouse_hook:
            user32.UnhookWindowsHookEx(mouse_hook)

    def _kbd_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == HC_ACTION:
            struct = ctypes.cast(l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            source = "automation" if (struct.flags & LLKHF_INJECTED) else "user"
            self._record_input(source)
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _mouse_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code == HC_ACTION:
            struct = ctypes.cast(l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            source = "automation" if (struct.flags & LLMHF_INJECTED) else "user"
            self._record_input(source)
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    # ---------------- 日志 ----------------

    def _emit(self, event: FocusEvent) -> None:
        self.events.put(event)
        if self.run_dir is not None:
            line = json.dumps(event.to_dict(), ensure_ascii=False)
            try:
                with (Path(self.run_dir) / "focus_events.jsonl").open(
                        "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass  # 日志写失败不阻断观测


def kernel32_Get_current_thread_id() -> int:
    return ctypes.windll.kernel32.GetCurrentThreadId()

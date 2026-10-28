"""MagicWindow Focus Manager — 可复用的 Windows 焦点管理被动服务。

导出：FocusManager / WindowInfo / FocusEvent / win32 原语模块。
"""
from .focus_manager import FocusEvent, FocusManager, WindowInfo
from . import win32

__version__ = "0.1.0"

__all__ = ["FocusManager", "WindowInfo", "FocusEvent", "win32"]

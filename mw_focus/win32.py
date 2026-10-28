"""Windows 底层原语封装（ctypes，无第三方依赖）。

集中放所有 kernel32/user32 调用，业务代码不直接碰 ctypes。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
THREAD_SUSPEND_RESUME = 0x0002
TH32CS_SNAPPROCESS = 0x0002
TH32CS_SNAPTHREAD = 0x0004
SW_MINIMIZE = 6
SW_SHOW = 5


def pid_process_name(pid: int) -> str | None:
    """取进程的可执行文件路径，取不到返回 None。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def _process_entries() -> list[tuple[int, int]]:
    """快照所有进程，返回 [(pid, ppid)]。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    entries: list[tuple[int, int]] = []
    if snap == -1:
        return entries
    try:
        # PROCESSENTRY32W 结构：手动按字段布局声明
        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                entries.append((pe.th32ProcessID, pe.th32ParentProcessID))
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
        return entries
    finally:
        kernel32.CloseHandle(snap)


def process_tree(root_pid: int) -> set[int]:
    """取 root_pid 及其全部子孙进程的 pid 集合。"""
    entries = _process_entries()
    children: dict[int, list[int]] = {}
    for pid, ppid in entries:
        children.setdefault(ppid, []).append(pid)
    result: set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in result:
            continue
        result.add(pid)
        stack.extend(children.get(pid, []))
    return result


def _threads_of(pids: set[int]) -> list[int]:
    """取这些进程拥有的全部线程 id。"""
    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    tids: list[int] = []
    if snap == -1:
        return tids
    try:
        te = THREADENTRY32()
        te.dwSize = ctypes.sizeof(THREADENTRY32)
        if kernel32.Thread32First(snap, ctypes.byref(te)):
            while True:
                if te.th32OwnerProcessID in pids:
                    tids.append(te.th32ThreadID)
                if not kernel32.Thread32Next(snap, ctypes.byref(te)):
                    break
        return tids
    finally:
        kernel32.CloseHandle(snap)


def suspend_tree(root_pid: int) -> int:
    """挂起进程树（逐线程 SuspendThread），返回挂起的线程数。"""
    pids = process_tree(root_pid)
    count = 0
    for tid in _threads_of(pids):
        handle = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, tid)
        if handle:
            kernel32.SuspendThread(handle)
            kernel32.CloseHandle(handle)
            count += 1
    return count


def resume_tree(root_pid: int) -> int:
    """恢复进程树，返回恢复的线程数。"""
    pids = process_tree(root_pid)
    count = 0
    for tid in _threads_of(pids):
        handle = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, tid)
        if handle:
            kernel32.ResumeThread(handle)
            kernel32.CloseHandle(handle)
            count += 1
    return count

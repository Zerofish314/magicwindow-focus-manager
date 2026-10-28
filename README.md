# MagicWindow Focus Manager

MagicWindow（MW）项目的焦点管理器组件，抽离为**可独立复用**的 Python 包。

设计定位：**被动服务**——只观测、只执行、只归因、只记录，**不含任何策略**。
"何时暂停/恢复/回切"是调用方（你的业务程序）的决策，通过本服务提供的原语执行。

## 能力

| 类别 | 内容 |
|---|---|
| 观测 | 可见窗口快照（`snapshot`）、前台窗口查询（`foreground`）、按进程名找主窗口（`find_game_window`） |
| 执行 | `set_foreground(hwnd)` 四级回退切前台（幂等）、`minimize(hwnd)`、`pseudo_minimize`（预留） |
| 归因 | 低级键鼠钩子（`WH_KEYBOARD_LL`/`WH_MOUSE_LL`）按 `INJECTED` 标志区分 **user / automation**，本服务自身操作记 **self** |
| 日志 | 焦点变化事件写 `focus_events.jsonl`，同时进内存队列供 GUI 实时消费 |

另有 `mw_focus.win32` 提供 Windows 进程树挂起/恢复（`suspend_tree`/`resume_tree`）等原语。

## 环境

- Windows only；Python 3.11+
- **零第三方依赖**（纯 ctypes + 标准库）

## 快速开始

```bash
pip install git+https://github.com/Zerofish314/magicwindow-focus-manager.git
```

```python
from mw_focus import FocusManager

fm = FocusManager(poll_interval_ms=800, run_dir=Path("runs/001"))
fm.start()

# 观测
game_hwnd = fm.find_game_window(["ZenlessZoneZero.exe"])
print(fm.foreground())

# 执行
fm.set_foreground(game_hwnd)   # 四级回退
fm.minimize(some_hwnd)

# 消费事件（foreground_changed / window_opened / input，带 source 归因）
while True:
    event = fm.events.get()
    print(event.to_dict())   # -> logs/focus_events.jsonl 同行
```

完整示例见 `examples/watch_foreground.py`。

## 设计说明

- **为什么被动**：策略（误触暂停、20s 回切、弹窗防御）随业务而变，服务层保持稳定接口。
  MW 的 runner 侧策略实现（`focus_policy.py`，应用专属）可作为参考自己实现，不在本包内。
- **归因原理**：Windows 低级钩子的事件结构带 `LLKHF_INJECTED` / `LLMHF_INJECTED` 标志——
  SendInput/keybd_event 等注入输入带标志（automation），真实用户输入不带（user）。
- **已知边界**：UAC/安全桌面无法干预；钩子被安全软件拦截时事件归因降级为 `unknown`。

## 来源与许可

提取自 MagicWindow 项目（`gdaily/focus_manager.py` + `gdaily/win32.py`）。
MIT License，可自由用于任何项目。

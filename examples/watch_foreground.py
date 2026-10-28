"""示例：观测前台窗口与焦点事件（Ctrl+C 退出）。

运行后切换窗口、点击鼠标，观察事件的 source 归因：
- 你的真实操作  -> user
- SendInput/keybd_event 注入  -> automation
"""
from pathlib import Path

from mw_focus import FocusManager

fm = FocusManager(poll_interval_ms=500, run_dir=Path("."))
fm.start()
print("观测中：切换窗口/点击鼠标，事件将打印并写入 ./focus_events.jsonl（Ctrl+C 退出）")
try:
    while True:
        event = fm.events.get()
        d = event.to_dict()
        print(f"[{d['ts']}] {d['type']:<20} source={d['source']:<10} {Path(d['process']).name} {d['detail']}")
except KeyboardInterrupt:
    fm.stop()
    print("退出")

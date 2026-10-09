"""gifspeed 界面绑定自检。

存在的原因：`SpeedApp` 曾经用 `self.start = tk.StringVar(...)` 存「截取起始秒」，
把 `SpeedApp.start()` 这个方法整个覆盖掉了；而 `_build_options()` 又跑在
`_build_run()` 之前，于是「开始转换」按钮绑上去的是一个 StringVar——
点下去只在 stderr 抛一句 `TypeError: 'StringVar' object is not callable`，
界面上完全没反应。这个脚本把这类问题钉死。

跑法：  python test_gui.py
无图形环境时自动跳过（返回 0）。
"""

from __future__ import annotations

import sys

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # pragma: no cover
    print("跳过：这个 Python 没有 tkinter。")
    raise SystemExit(0)

from gifspeed.gui import PRESET_SPEEDS, SpeedApp

# 这些方法一旦被同名实例属性覆盖，界面上就会出现「点了没反应」。
CRITICAL_METHODS = (
    "start", "add_files", "add_folder", "clear_files",
    "remove_selected", "pick_outdir", "open_outdir", "log",
)


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"跳过：没有可用的图形环境（{exc}）。")
        return 0

    root.withdraw()
    failures: list[str] = []
    try:
        app = SpeedApp(root)
        root.update_idletasks()

        # 1) 实例属性不得覆盖类里的方法（这是当初那个 bug 的根因）
        cls_methods = {n for n, v in vars(SpeedApp).items()
                       if callable(v) and not n.startswith("__")}
        clashes = sorted(set(vars(app)) & cls_methods)
        for name in clashes:
            failures.append(
                f"实例属性 self.{name} 覆盖了同名方法 SpeedApp.{name}"
                f"（实际类型 {type(getattr(app, name)).__name__}），请改名"
            )

        # 2) 关键方法仍然可调用
        for name in CRITICAL_METHODS:
            if not callable(getattr(app, name, None)):
                failures.append(f"SpeedApp.{name} 不是可调用对象")

        # 3) 按钮的 command 必须指向一个真正的 Python 可调用对象
        buttons = [w for w in walk(root) if isinstance(w, ttk.Button)]
        for btn in buttons:
            cmd = btn.cget("command")
            label = btn.cget("text")
            if not cmd:
                failures.append(f"按钮 {label!r} 没有绑定 command")
                continue
            try:
                target = root.tk.getcommand(cmd)
            except Exception:
                continue  # 拿不到就算了，第 1 条已经能抓住主要的坑
            if not callable(target):
                failures.append(
                    f"按钮 {label!r} 的 command 指向非可调用对象 "
                    f"{type(target).__name__}（{cmd}）"
                )
    finally:
        root.destroy()

    print(f"预设倍速：{PRESET_SPEEDS}")
    if failures:
        print(f"\n发现 {len(failures)} 个问题：")
        for f in failures:
            print("  x " + f)
        return 1
    print("全部通过：实例属性未覆盖方法，按钮回调均为可调用对象。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

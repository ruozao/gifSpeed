"""gifspeed.gui —— tkinter 桌面窗口。

界面结构：
    上  文件列表（可加文件 / 加目录 / 递归 / 移除 / 清空）
    中  倍速与选项（预设勾选 + 任意倍速、模式、最小延时、画面变换、输出命名）
    下  运行按钮 + 进度条 + 日志
转换在后台线程里跑，通过 queue 把日志传回主线程，界面不会卡死。
"""

from __future__ import annotations

import os
import queue
import threading
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .core import (
    BROWSER_CLAMP_MS,
    CS_MS,
    EncodeOptions,
    Transform,
    convert_many,
    format_speed,
    load_gif,
)
from .report import format_result, format_summary, results_to_json

PRESET_SPEEDS = ("0.5", "2", "5", "10")


class SpeedApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.msg_queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None

        root.title(f"gifspeed {__version__} —— GIF 变速工具")
        root.geometry("980x760")
        root.minsize(860, 640)

        self._build_style()
        self._build_menu()
        self._build_files()
        self._build_options()
        self._build_run()

        # 兜底：回调里抛出的异常不再只往 stderr 里一写了事，而是写进日志区并弹窗。
        # （「开始转换」按钮曾经因为变量名覆盖方法名而静默失效，就是被这条抓住的。）
        self.root.report_callback_exception = self._report_callback_exception

        self.log(
            "就绪。先添加 GIF，再勾选倍速，然后点「开始转换」。\n"
            f"说明：GIF 延时最小刻度是 {CS_MS}ms，浏览器会把小于 {BROWSER_CLAMP_MS}ms 的延时当作 100ms，\n"
            "所以默认用「真加速（丢帧）」模式，保证高倍速在任何浏览器里都真的变快。\n"
        )
        self.root.after(100, self._pump)

    # ------------------------------------------------------------------ 样式
    def _build_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
        style.configure("Run.TButton", font=("Segoe UI", 10, "bold"))

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="添加文件…", command=self.add_files)
        m_file.add_command(label="添加目录…", command=self.add_folder)
        m_file.add_separator()
        m_file.add_command(label="清空列表", command=self.clear_files)
        m_file.add_separator()
        m_file.add_command(label="退出", command=self.root.destroy)
        menubar.add_cascade(label="文件", menu=m_file)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="关于", command=self.show_about)
        menubar.add_cascade(label="帮助", menu=m_help)
        self.root.config(menu=menubar)

    def show_about(self) -> None:
        messagebox.showinfo(
            "关于 gifspeed",
            f"gifspeed {__version__}\n\n"
            "GIF 加速 / 减速工具。\n"
            f"• GIF 延时以 {CS_MS}ms 为单位存储\n"
            f"• 浏览器把 <{BROWSER_CLAMP_MS}ms 的延时当作 100ms（IE10+ 起下限 {BROWSER_CLAMP_MS}ms）\n"
            "• 因此高倍速必须丢帧，才能真正变快\n"
            "• 默认逐像素无损，并自动做帧间差异压缩控制体积",
        )

    # ------------------------------------------------------------ 文件列表
    def _build_files(self) -> None:
        frame = ttk.LabelFrame(self.root, text="输入文件")
        frame.pack(fill="both", expand=False, padx=8, pady=(8, 4))

        inner = ttk.Frame(frame)
        inner.pack(fill="both", expand=True, padx=6, pady=6)

        self.file_list = tk.Listbox(inner, height=6, selectmode="extended",
                                    activestyle="none", font=("Consolas", 9))
        self.file_list.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.file_list.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.file_list.config(yscrollcommand=sb.set)
        inner.rowconfigure(0, weight=1)
        inner.columnconfigure(0, weight=1)

        btns = ttk.Frame(inner)
        btns.grid(row=0, column=2, sticky="ns", padx=(8, 0))
        ttk.Button(btns, text="添加文件", command=self.add_files).pack(fill="x", pady=1)
        ttk.Button(btns, text="添加目录", command=self.add_folder).pack(fill="x", pady=1)
        ttk.Button(btns, text="移除选中", command=self.remove_selected).pack(fill="x", pady=1)
        ttk.Button(btns, text="清空", command=self.clear_files).pack(fill="x", pady=1)

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=6, pady=(0, 6))
        self.recursive = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="添加目录时递归子目录", variable=self.recursive).pack(side="left")
        self.file_count = ttk.Label(row, text="共 0 个文件")
        self.file_count.pack(side="right")

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择 GIF 文件",
            filetypes=[("GIF 动图", "*.gif"), ("所有文件", "*.*")],
        )
        self._add_paths(paths)

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择包含 GIF 的目录")
        if not folder:
            return
        pattern = "**/*.gif" if self.recursive.get() else "*.gif"
        import glob

        found = sorted(glob.glob(os.path.join(folder, pattern), recursive=self.recursive.get()))
        found += sorted(glob.glob(os.path.join(folder, pattern.upper()), recursive=self.recursive.get()))
        if not found:
            messagebox.showinfo("提示", "该目录里没有找到 .gif 文件。")
            return
        self._add_paths(found)

    def _add_paths(self, paths) -> None:
        existing = set(self.file_list.get(0, "end"))
        added = 0
        for p in paths or ():
            p = os.path.abspath(p)
            if p not in existing and os.path.isfile(p):
                self.file_list.insert("end", p)
                existing.add(p)
                added += 1
        self._refresh_count()
        if added:
            self.log(f"已添加 {added} 个文件。")

    def remove_selected(self) -> None:
        for i in reversed(self.file_list.curselection()):
            self.file_list.delete(i)
        self._refresh_count()

    def clear_files(self) -> None:
        self.file_list.delete(0, "end")
        self._refresh_count()

    def _refresh_count(self) -> None:
        self.file_count.config(text=f"共 {self.file_list.size()} 个文件")

    # -------------------------------------------------------------- 选项区
    def _build_options(self) -> None:
        outer = ttk.Frame(self.root)
        outer.pack(fill="x", padx=8, pady=4)

        # --- 倍速
        box = ttk.LabelFrame(outer, text="倍速")
        box.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        inner = ttk.Frame(box)
        inner.pack(padx=6, pady=6, anchor="w")

        self.preset_vars: dict[str, tk.BooleanVar] = {}
        for i, sp in enumerate(PRESET_SPEEDS):
            var = tk.BooleanVar(value=sp in ("2",))
            self.preset_vars[sp] = var
            ttk.Checkbutton(inner, text=f"{sp}x", variable=var).grid(row=0, column=i, padx=3)

        ttk.Label(inner, text="自定义倍速：").grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.custom_speeds = tk.StringVar(value="")
        entry = ttk.Entry(inner, textvariable=self.custom_speeds, width=26)
        entry.grid(row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(inner, text="（逗号分隔，可为任意正数，如 1.5,3.33）",
                  foreground="#666").grid(row=2, column=0, columnspan=4, sticky="w")

        # --- 模式
        box = ttk.LabelFrame(outer, text="变速模式")
        box.grid(row=0, column=1, sticky="nsew", padx=(0, 6))
        inner = ttk.Frame(box)
        inner.pack(padx=6, pady=6, anchor="w")

        self.mode = tk.StringVar(value="drop")
        ttk.Radiobutton(inner, text="真加速（丢帧，浏览器里确实变快）",
                        variable=self.mode, value="drop", command=self._sync_mode).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(inner, text="纯延时（保留全部帧，高倍速浏览器可能不认）",
                        variable=self.mode, value="delay", command=self._sync_mode).grid(row=1, column=0, sticky="w")

        row = ttk.Frame(inner)
        row.grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(row, text=f"最小帧延时(ms)：").pack(side="left")
        self.min_delay = tk.IntVar(value=BROWSER_CLAMP_MS)
        self.min_delay_spin = ttk.Spinbox(row, from_=CS_MS, to=500, increment=CS_MS,
                                          textvariable=self.min_delay, width=6)
        self.min_delay_spin.pack(side="left")
        ttk.Label(row, text="  循环：").pack(side="left")
        self.loop_mode = tk.StringVar(value="keep")
        ttk.Combobox(row, textvariable=self.loop_mode, width=8, state="readonly",
                     values=("keep", "infinite", "once", "2", "3", "5")).pack(side="left")

        # --- 画面
        box = ttk.LabelFrame(outer, text="画面变换")
        box.grid(row=0, column=2, sticky="nsew")
        inner = ttk.Frame(box)
        inner.pack(padx=6, pady=6, anchor="w")

        self.reverse = tk.BooleanVar(value=False)
        ttk.Checkbutton(inner, text="反向播放", variable=self.reverse).grid(row=0, column=0, sticky="w")

        row = ttk.Frame(inner)
        row.grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(row, text="缩放：").pack(side="left")
        self.scale = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.scale, width=6).pack(side="left")
        ttk.Label(row, text="或宽/高：").pack(side="left")
        self.width = tk.StringVar(value="")
        self.height = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.width, width=5).pack(side="left")
        ttk.Label(row, text="x").pack(side="left")
        ttk.Entry(row, textvariable=self.height, width=5).pack(side="left")

        row = ttk.Frame(inner)
        row.grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Label(row, text="裁剪 x,y,w,h：").pack(side="left")
        self.crop = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.crop, width=18).pack(side="left")

        row = ttk.Frame(inner)
        row.grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Label(row, text="截取 起/止(秒)：").pack(side="left")
        # 注意：这两个变量曾经叫 self.start / self.end，其中 self.start 会覆盖
        # SpeedApp.start() 这个方法，导致「开始转换」按钮绑到一个 StringVar 上，
        # 点下去只会抛 TypeError 而界面毫无反应。命名上必须绕开方法名。
        self.trim_start = tk.StringVar(value="")
        self.trim_end = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.trim_start, width=6).pack(side="left")
        ttk.Label(row, text="~").pack(side="left")
        ttk.Entry(row, textvariable=self.trim_end, width=6).pack(side="left")

        row = ttk.Frame(inner)
        row.grid(row=4, column=0, sticky="w", pady=(4, 0))
        ttk.Label(row, text="减色（有损）：").pack(side="left")
        self.colors = tk.StringVar(value="")
        ttk.Combobox(row, textvariable=self.colors, width=6,
                     values=("", "32", "64", "128", "256")).pack(side="left")
        ttk.Label(row, text=" 色").pack(side="left")

        # --- 输出
        box = ttk.LabelFrame(outer, text="输出")
        box.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        inner = ttk.Frame(box)
        inner.pack(fill="x", padx=6, pady=6)

        self.same_dir = tk.BooleanVar(value=True)
        ttk.Checkbutton(inner, text="输出到源文件所在目录", variable=self.same_dir,
                        command=self._sync_outdir).grid(row=0, column=0, sticky="w")

        row = ttk.Frame(inner)
        row.grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Label(row, text="目录：").pack(side="left")
        self.outdir = tk.StringVar(value="")
        self.outdir_entry = ttk.Entry(row, textvariable=self.outdir, width=48)
        self.outdir_entry.pack(side="left")
        self.outdir_btn = ttk.Button(row, text="浏览…", command=self.pick_outdir)
        self.outdir_btn.pack(side="left", padx=4)

        row = ttk.Frame(inner)
        row.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(row, text="命名模板：").pack(side="left")
        self.template = tk.StringVar(value="{stem}_{speed}x")
        ttk.Entry(row, textvariable=self.template, width=26).pack(side="left")
        ttk.Label(row, text="  可用 {stem} {speed} {ext}").pack(side="left")
        self.overwrite = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="覆盖同名文件", variable=self.overwrite).pack(side="left", padx=(10, 0))
        self.verify = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="写回逐像素自检", variable=self.verify).pack(side="left", padx=(10, 0))

    def _sync_mode(self) -> None:
        state = "normal" if self.mode.get() == "drop" else "disabled"
        self.min_delay_spin.config(state=state)

    def _sync_outdir(self) -> None:
        state = "disabled" if self.same_dir.get() else "normal"
        self.outdir_entry.config(state=state)
        self.outdir_btn.config(state=state)

    def pick_outdir(self) -> None:
        folder = filedialog.askdirectory(title="选择输出目录")
        if folder:
            self.outdir.set(folder)
            self.same_dir.set(False)
            self._sync_outdir()

    # -------------------------------------------------------------- 运行区
    def _build_run(self) -> None:
        frame = ttk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        row = ttk.Frame(frame)
        row.pack(fill="x")
        self.run_btn = ttk.Button(row, text="开始转换", style="Run.TButton", command=self.start)
        self.run_btn.pack(side="left")
        self.open_btn = ttk.Button(row, text="打开输出目录", command=self.open_outdir)
        self.open_btn.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(row, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        self.status = ttk.Label(row, text="空闲")
        self.status.pack(side="right")

        log_frame = ttk.LabelFrame(frame, text="日志 / 自检报告")
        log_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.log_text = tk.Text(log_frame, wrap="none", font=("Consolas", 9),
                                background="#111", foreground="#ddd", insertbackground="#ddd")
        sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.config(yscrollcommand=sb.set, state="disabled")

    def log(self, text: str) -> None:
        self.log_text.config(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _report_callback_exception(self, exc_type, exc_value, exc_tb) -> None:
        """tkinter 回调异常的兜底处理：进日志 + 弹窗 + 复位按钮。"""
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            self.log("!! 界面操作出错：\n" + text)
            self.run_btn.config(state="normal")
            self.status.config(text="出错")
        except tk.TclError:
            pass
        messagebox.showerror(
            "gifspeed 出错",
            f"{exc_type.__name__}: {exc_value}\n\n详细信息见窗口下方的日志区。",
        )

    def open_outdir(self) -> None:
        target = self.outdir.get().strip()
        if self.same_dir.get() or not target:
            sel = self.file_list.curselection()
            if sel:
                target = os.path.dirname(self.file_list.get(sel[0]))
            elif self.file_list.size():
                target = os.path.dirname(self.file_list.get(0))
            else:
                messagebox.showinfo("提示", "还没有选择文件。")
                return
        if os.path.isdir(target):
            os.startfile(target)  # noqa: S606 - Windows 专用
        else:
            messagebox.showinfo("提示", f"目录不存在：{target}")

    # ---------------------------------------------------------------- 启动
    def _collect_speeds(self) -> list[float]:
        speeds: list[float] = []
        for sp, var in self.preset_vars.items():
            if var.get():
                speeds.append(float(sp))
        raw = self.custom_speeds.get().replace("，", ",").replace(";", ",")
        for part in raw.split(","):
            part = part.strip().rstrip("xX")
            if not part:
                continue
            try:
                v = float(part)
            except ValueError:
                raise ValueError(f"无法解析自定义倍速：{part!r}")
            if v <= 0:
                raise ValueError(f"倍速必须为正数：{v}")
            if v not in speeds:
                speeds.append(v)
        if not speeds:
            raise ValueError("请至少勾选或填写一个倍速。")
        return speeds

    def _collect_transform(self) -> Transform:
        def opt_float(var: tk.StringVar) -> float | None:
            text = var.get().strip()
            return float(text) if text else None

        def opt_int(var: tk.StringVar) -> int | None:
            text = var.get().strip()
            return int(float(text)) if text else None

        crop = None
        text = self.crop.get().strip()
        if text:
            parts = [p.strip() for p in text.replace(";", ",").split(",")]
            if len(parts) != 4:
                raise ValueError("裁剪需要 4 个数字：x,y,w,h")
            x, y, w, h = (int(float(p)) for p in parts)
            crop = (x, y, x + w, y + h)

        return Transform(
            reverse=self.reverse.get(),
            scale=opt_float(self.scale),
            width=opt_int(self.width),
            height=opt_int(self.height),
            crop=crop,
            start_s=opt_float(self.trim_start),
            end_s=opt_float(self.trim_end),
            colors=opt_int(self.colors),
        )

    def _collect_encode(self) -> EncodeOptions:
        mode = self.loop_mode.get().strip()
        if mode == "keep":
            loop: int | None = None
        elif mode == "infinite":
            loop = 0
        elif mode == "once":
            loop = -1
        else:
            loop = int(mode)
        return EncodeOptions(loop=loop, optimize=True, disposal=None)

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "正在转换中，请稍候。")
            return
        files = list(self.file_list.get(0, "end"))
        if not files:
            messagebox.showwarning("提示", "请先添加至少一个 GIF 文件。")
            return
        try:
            speeds = self._collect_speeds()
            transform = self._collect_transform()
            encode = self._collect_encode()
        except ValueError as exc:
            messagebox.showerror("参数有误", str(exc))
            return

        outdir = None if self.same_dir.get() else (self.outdir.get().strip() or None)
        if outdir and not os.path.isdir(outdir):
            try:
                os.makedirs(outdir, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("无法创建输出目录", str(exc))
                return

        self.run_btn.config(state="disabled")
        self.progress.config(maximum=len(files) * len(speeds), value=0)
        self.status.config(text="运行中…")
        self.log("")
        self.log(f"=== 开始：{len(files)} 个文件 × {len(speeds)} 个倍速 = "
                 f"{[format_speed(s) for s in speeds]} ===")
        if transform.touches_pixels:
            self.log("注意：本次包含缩放/减色等有损变换，将不做逐像素无损比对。")

        # 所有 tk 变量必须在这里（主线程）读成普通值再交给工作线程：
        # Tcl 不是线程安全的，在后台线程里调 var.get() 可能直接死锁。
        job = {
            "name_template": self.template.get().strip() or None,
            "mode": self.mode.get(),
            "min_delay_ms": int(self.min_delay.get()),
            "verify": bool(self.verify.get()),
            "overwrite": bool(self.overwrite.get()),
        }

        self.worker = threading.Thread(
            target=self._worker,
            args=(files, speeds, outdir, transform, encode, job),
            daemon=True,
        )
        self.worker.start()

    def _worker(self, files, speeds, outdir, transform, encode, job) -> None:
        counter = {"done": 0}

        def progress(msg: str) -> None:
            if msg.startswith("["):
                counter["done"] += 1
                self.msg_queue.put(("progress", counter["done"]))
            self.msg_queue.put(("log", msg))

        try:
            results = convert_many(
                files,
                speeds,
                outdir=outdir,
                name_template=job["name_template"],
                mode=job["mode"],
                min_delay_ms=job["min_delay_ms"],
                transform=transform,
                encode=encode,
                verify=job["verify"],
                overwrite=job["overwrite"],
                progress=progress,
            )
            for r in results:
                self.msg_queue.put(("log", format_result(r)))
            self.msg_queue.put(("log", format_summary(results)))
            self.msg_queue.put(("done", results))
        except Exception:
            self.msg_queue.put(("log", "!! 发生未处理异常：\n" + traceback.format_exc()))
            self.msg_queue.put(("done", None))

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self.log(str(payload))
                elif kind == "progress":
                    self.progress.config(value=payload)
                    self.status.config(text=f"运行中… {payload}/{int(self.progress['maximum'])}")
                elif kind == "done":
                    self.run_btn.config(state="normal")
                    self.status.config(text="完成" if payload else "出错")
                    if payload:
                        ok = sum(1 for r in payload if r.ok)
                        self.log(f"完成：{ok}/{len(payload)} 成功。")
        except queue.Empty:
            pass
        self.root.after(100, self._pump)


def launch() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"无法启动图形界面（{exc}）。请改用命令行：python -m gifspeed --help")
        return 1
    SpeedApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch())

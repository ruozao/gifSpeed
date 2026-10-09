"""gifspeed.cli —— 命令行入口。

用法示例
--------
    # 单个文件，4 个倍速一次出齐
    python -m gifspeed demo.gif --speeds 0.5,2,5,10

    # 真加速（丢帧，浏览器里确实变快）
    python -m gifspeed demo.gif -s 10 --mode drop

    # 整目录批量，输出到 out/，并写 JSON 报告
    python -m gifspeed ./gifs -r -o ./out --speeds 0.5,2,5,10 --json report.json

    # 顺带裁剪 + 缩小 + 减色
    python -m gifspeed demo.gif -s 2 --crop 0,0,300,300 --scale 0.5 --colors 128

    # 只看信息不动文件
    python -m gifspeed demo.gif --info

    # 打开图形界面
    python -m gifspeed --gui
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Sequence

from . import __version__
from .core import (
    BROWSER_CLAMP_MS,
    BROWSER_CLAMP_TO_MS,
    EncodeOptions,
    Transform,
    convert_many,
    format_speed,
    load_gif,
    plan_speed,
)
from .report import format_result, format_summary, read_gif_raw, results_to_json

GIF_EXTENSIONS = (".gif",)


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #


def _speed_list(text: str) -> list[float]:
    out: list[float] = []
    for part in text.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip().rstrip("xX")
        if not part:
            continue
        try:
            v = float(part)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"无法解析倍速 {part!r}") from exc
        if v <= 0:
            raise argparse.ArgumentTypeError(f"倍速必须为正数，收到 {v}")
        out.append(v)
    if not out:
        raise argparse.ArgumentTypeError("没有解析出任何倍速")
    return out


def _crop_box(text: str) -> tuple[int, int, int, int]:
    parts = [p.strip() for p in text.replace(";", ",").split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--crop 需要 4 个数字：x,y,w,h（宽高，不是右下角坐标）")
    try:
        x, y, w, h = (int(float(p)) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"无法解析 --crop {text!r}") from exc
    if w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("--crop 的宽高必须为正")
    return x, y, x + w, y + h


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gifspeed",
        description="GIF 加速 / 减速工具：改延时，必要时自动丢帧，保证浏览器里是真正的倍速。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("inputs", nargs="*", help="GIF 文件或目录")
    p.add_argument("--gui", action="store_true", help="打开图形界面")
    p.add_argument("--version", action="version", version=f"gifspeed {__version__}")

    g = p.add_argument_group("倍速")
    g.add_argument("-s", "--speed", type=float, default=None, help="单个倍速，例如 2 或 0.5")
    g.add_argument(
        "--speeds",
        type=_speed_list,
        default=None,
        help="多个倍速，逗号分隔，例如 0.5,2,5,10（每个倍速产出一个文件）",
    )
    g.add_argument(
        "--mode",
        choices=("drop", "delay"),
        default="drop",
        help="drop=真加速(丢帧, 延时守住 20ms, 浏览器里确实变快, 默认)；"
        "delay=保留全部帧只改延时(最平滑, 但 <20ms 会被浏览器抬到 100ms)",
    )
    g.add_argument(
        "--min-delay",
        type=int,
        default=BROWSER_CLAMP_MS,
        metavar="MS",
        help=f"drop 模式的最小帧延时，默认 {BROWSER_CLAMP_MS}ms（浏览器下限）。"
        "调大更保险但帧数更少，调小可能被浏览器抬到 100ms",
    )

    g = p.add_argument_group("输出")
    g.add_argument("-o", "--outdir", default=None, help="输出目录（默认与源文件同目录）")
    g.add_argument(
        "--name-template",
        default=None,
        metavar="TPL",
        help="输出文件名模板，可用 {stem} {speed} {ext}，默认 {stem}_{speed}x.gif",
    )
    g.add_argument("--overwrite", dest="overwrite", action="store_true", default=True,
                   help="覆盖已存在的输出（默认）")
    g.add_argument("--no-overwrite", dest="overwrite", action="store_false",
                   help="已存在则跳过")
    g.add_argument("--json", dest="json_path", default=None, metavar="PATH",
                   help="把自检结果写成 JSON")

    g = p.add_argument_group("画面变换")
    g.add_argument("--reverse", action="store_true", help="反向播放")
    g.add_argument("--colors", type=int, default=None, metavar="N",
                   help="减色到 N 色（有损，默认保持源调色板）")
    g.add_argument("--scale", type=float, default=None, help="缩放比例，例如 0.5")
    g.add_argument("--width", type=int, default=None, help="目标宽度（高按比例）")
    g.add_argument("--height", type=int, default=None, help="目标高度（宽按比例）")
    g.add_argument("--crop", type=_crop_box, default=None, metavar="x,y,w,h", help="裁剪区域")
    g.add_argument("--start", type=float, default=None, metavar="SEC", help="只处理该秒之后的部分")
    g.add_argument("--end", type=float, default=None, metavar="SEC", help="只处理该秒之前的部分")

    g = p.add_argument_group("编码")
    g.add_argument("--loop", type=int, default=None, metavar="N",
                   help="循环次数，0=无限（默认沿用源文件）")
    g.add_argument("--no-loop", action="store_true", help="不循环，只播一次")
    g.add_argument("--disposal", type=int, choices=(0, 1, 2, 3), default=None,
                   help="强制 disposal 方式（默认 1 = 不还原上一帧，这样才能做帧间差异压缩，体积最小）")
    g.add_argument("--no-optimize", action="store_true",
                   help="关闭 Pillow 的调色板优化（体积通常更大）")
    g.add_argument("--no-verify", action="store_true", help="跳过写回逐像素自检（更快）")

    g = p.add_argument_group("批量与诊断")
    g.add_argument("-r", "--recursive", action="store_true", help="递归扫描子目录")
    g.add_argument("--pattern", default="*.gif", help="目录扫描用的通配符，默认 *.gif")
    g.add_argument("--info", action="store_true", help="只打印源文件信息，不转换")
    g.add_argument("-v", "--verbose", action="store_true", help="打印字节级细节")
    g.add_argument("--quiet", action="store_true", help="只输出结果路径")
    g.add_argument("--dry-run", action="store_true", help="只显示将要做什么，不写文件")
    return p


# --------------------------------------------------------------------------- #
# 输入收集
# --------------------------------------------------------------------------- #


def collect_inputs(paths: Sequence[str], recursive: bool, pattern: str) -> list[str]:
    files: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            glob_pat = os.path.join(path, "**", pattern) if recursive else os.path.join(path, pattern)
            found = sorted(glob.glob(glob_pat, recursive=recursive))
            # 顺手把大写扩展名也收进来
            found += sorted(glob.glob(glob_pat.upper().replace("*.GIF", "*.GIF"), recursive=recursive))
            files.extend(f for f in dict.fromkeys(found) if os.path.isfile(f))
        elif os.path.isfile(path):
            files.append(path)
        else:
            print(f"!! 找不到：{path}", file=sys.stderr)
    # 去重并保序
    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        key = os.path.abspath(f).lower()
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def cmd_info(paths: Sequence[str], verbose: bool) -> int:
    rc = 0
    for path in paths:
        try:
            info = load_gif(path)
            raw = read_gif_raw(path)
        except Exception as exc:
            print(f"[失败] {path}: {exc}", file=sys.stderr)
            rc = 1
            continue
        print(info.summary())
        print(
            f"    版本={raw.signature.decode('latin-1')} "
            f"循环={'无限' if raw.loop == 0 else raw.loop} "
            f"全局色表={raw.global_palette_colors} "
            f"差异帧={raw.delta_frames}/{raw.frame_count} "
            f"透明帧={sum(raw.transparent_flags)}"
        )
        print(f"    延时分布: " + ", ".join(f"{d}ms×{c}" for d, c in raw.delay_histogram()))
        if raw.min_delay_ms < BROWSER_CLAMP_MS:
            print(
                f"    ! 最小延时 {raw.min_delay_ms}ms 已低于浏览器下限 {BROWSER_CLAMP_MS}ms，"
                f"浏览器会按 {BROWSER_CLAMP_TO_MS}ms 播（实际 {raw.browser_total_ms}ms）"
            )
        del info
    return rc


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.gui:
        from .gui import launch

        return launch()

    if not args.inputs:
        parser.print_help()
        return 2

    files = collect_inputs(args.inputs, args.recursive, args.pattern)
    if not files:
        print("没有找到任何 GIF 文件。", file=sys.stderr)
        return 2

    if args.info:
        return cmd_info(files, args.verbose)

    speeds = args.speeds or ([args.speed] if args.speed else [2.0])
    if args.speed and not args.speeds:
        speeds = [args.speed]

    transform = Transform(
        reverse=args.reverse,
        scale=args.scale,
        width=args.width,
        height=args.height,
        crop=args.crop,
        start_s=args.start,
        end_s=args.end,
        colors=args.colors,
    )
    if args.no_loop:
        loop_value: int | None = -1
    elif args.loop is not None:
        loop_value = args.loop
    else:
        loop_value = None
    encode = EncodeOptions(
        loop=loop_value,
        optimize=not args.no_optimize,
        disposal=args.disposal,
    )

    if args.dry_run:
        print(f"将处理 {len(files)} 个文件 × {len(speeds)} 个倍速：")
        for f in files:
            info = load_gif(f)
            print(f"  {info.summary()}")
            for s in speeds:
                plan = plan_speed(info.delays, s, mode=args.mode, min_delay_ms=args.min_delay)
                print(
                    f"    -> {format_speed(s)}x: {plan.out_frames} 帧, "
                    f"{plan.total_ms}ms, 丢 {plan.dropped} 帧, "
                    f"实际 {plan.achieved_speed:.3f}x"
                )
        return 0

    def progress(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    results = convert_many(
        files,
        speeds,
        outdir=args.outdir,
        name_template=args.name_template,
        mode=args.mode,
        min_delay_ms=args.min_delay,
        transform=transform,
        encode=encode,
        verify=not args.no_verify,
        overwrite=args.overwrite,
        progress=progress,
    )

    if args.quiet:
        for r in results:
            if r.ok:
                print(r.out_path)
    else:
        print()
        for r in results:
            print(format_result(r, verbose=args.verbose))
        print(format_summary(results))

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(results_to_json(results), fh, ensure_ascii=False, indent=2)
        if not args.quiet:
            print(f"JSON 报告已写入 {args.json_path}")

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

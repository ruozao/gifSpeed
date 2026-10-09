"""gifspeed 独立验证脚本。

为什么需要"独立"：``gifspeed`` 自己用 Pillow 读写 GIF，如果编码器和解码器犯了
同一个错，自检会一起骗过。所以这里用 **ffmpeg** 当第二个、完全独立的解码器：
把源文件和输出文件都解成 PNG，再逐帧、逐像素比对。

验证内容
--------
1. 每个源 GIF × {0.5, 2, 5, 10} 倍速 × {drop, delay} 两种模式都真的跑一遍。
2. 输出帧应当**正好等于**源文件里被挑选的那些帧（丢帧是挑选，不是重采样）。
3. 输出文件的字节级延时结构（用 gifspeed 自带的手写解析器读）。
4. 浏览器实际播出时长 —— 延时 < 20ms 的帧会被浏览器抬到 100ms。
5. 生成对比图，肉眼看得到差别。

用法::

    python verify_gifspeed.py               # 全部验证
    python verify_gifspeed.py --quick       # 只抽 6 帧比对，跑得快
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

from PIL import Image, ImageChops, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gifspeed import core  # noqa: E402
from gifspeed.report import read_gif_raw  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SPEEDS = (0.5, 2.0, 5.0, 10.0)
SOURCES = (
    "66C1EC40389049F55EE62882A7825F54.gif",
    "4D4E5531856363FC1FDFD085306F80A5.gif",
)
FFMPEG_CANDIDATES = (
    os.environ.get("GIFSPEED_FFMPEG", ""),
    r"E:\workbench\ffmpeg\bin\ffmpeg.exe",
    shutil.which("ffmpeg") or "",
)


def find_ffmpeg() -> str | None:
    for candidate in FFMPEG_CANDIDATES:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def extract_frames(ffmpeg: str, gif: str, outdir: str, quiet: bool = True) -> list[str]:
    """用 ffmpeg 把一个 GIF 解成 PNG 序列，返回按顺序排列的路径。"""
    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir, exist_ok=True)
    cmd = [
        ffmpeg, "-y", "-loglevel", "error" if quiet else "info",
        "-i", gif, "-vsync", "0", os.path.join(outdir, "%04d.png"),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    names = sorted(n for n in os.listdir(outdir) if n.endswith(".png"))
    return [os.path.join(outdir, n) for n in names]


def compare_frames(
    out_pngs: list[str],
    src_pngs: list[str],
    indices: list[int],
    stride: int = 1,
) -> dict:
    """逐像素比 out_pngs[j] 和 src_pngs[indices[j]]。"""
    if len(out_pngs) != len(indices):
        return {"error": f"输出帧数 {len(out_pngs)} != 期望 {len(indices)}"}

    worst = 0
    worst_frame = -1
    bad_frames: list[int] = []
    checked = 0
    for j, src_index in enumerate(indices):
        if stride > 1 and (j % stride):
            continue
        if not 0 <= src_index < len(src_pngs):
            return {"error": f"第 {j} 帧映射到不存在的源帧 {src_index}"}
        a = Image.open(out_pngs[j]).convert("RGB")
        b = Image.open(src_pngs[src_index]).convert("RGB")
        if a.size != b.size:
            return {"error": f"第 {j} 帧尺寸不同 {a.size} vs {b.size}"}
        checked += 1
        diff = ImageChops.difference(a, b)
        local = max(hi for _lo, hi in diff.getextrema())
        if local:
            bad_frames.append(j)
            if local > worst:
                worst, worst_frame = local, j
    return {
        "checked": checked,
        "max_channel_diff": worst,
        "worst_frame": worst_frame,
        "bad_frame_count": len(bad_frames),
        "bad_frames": bad_frames[:10],
        "lossless": not bad_frames,
    }


FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def load_font(size: int = 13):
    """找一个能渲染中文的字体（PIL 默认点阵字体只能画 latin-1，中文会变乱码方块）。"""
    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def sample_evenly(paths: list[str], count: int) -> list[str]:
    """在整段序列上均匀取 count 个样本（不是只取开头几个）。"""
    if count <= 0 or not paths:
        return []
    if len(paths) <= count:
        return list(paths)
    step = (len(paths) - 1) / (count - 1) if count > 1 else 0
    picked = []
    for i in range(count):
        picked.append(paths[min(len(paths) - 1, round(i * step))])
    return picked


def contact_sheet(rows: list[tuple[str, list[str]]], out_path: str, columns: int = 6) -> str:
    """把若干组帧拼成一张对比图。

    每行一组，**在整段动画上均匀采样** columns 帧——这样丢帧的效果一眼可见
    （只取开头几帧的话，所有行看起来会几乎一样）。
    """
    thumbs: list[list[Image.Image]] = []
    cell_w = 0
    cell_h = 0
    for _label, paths in rows:
        images = [Image.open(p).convert("RGB") for p in sample_evenly(paths, columns)]
        if not images:
            continue
        cell_w = max(cell_w, max(im.width for im in images))
        cell_h = max(cell_h, max(im.height for im in images))
        thumbs.append(images)
    if not thumbs:
        raise ValueError("没有可用于拼接的帧")

    scale = min(1.0, 190.0 / max(1, cell_w), 190.0 / max(1, cell_h))
    cell_w = max(1, int(cell_w * scale))
    cell_h = max(1, int(cell_h * scale))
    label_w = 250
    pad = 6
    width = label_w + columns * (cell_w + pad)
    sheet = Image.new("RGB", (width, len(thumbs) * (cell_h + pad)), (24, 24, 28))
    draw = ImageDraw.Draw(sheet)
    font = load_font(13)
    line_h = 15
    for r, ((label, _paths), images) in enumerate(zip(rows, thumbs)):
        y = r * (cell_h + pad)
        lines = label.split("\n")
        ty = y + max(4, cell_h // 2 - (line_h * len(lines)) // 2)
        for line in lines:
            draw.text((8, ty), line, fill=(235, 235, 235), font=font)
            ty += line_h
        for c, im in enumerate(images):
            thumb = im.resize((cell_w, cell_h), Image.LANCZOS) if scale < 1 else im
            sheet.paste(thumb, (label_w + c * (cell_w + pad), y))
    sheet.save(out_path)
    return out_path


def build_sheets(pngdir: str, outdir: str, records: list[dict], sources) -> list[str]:
    """从已经解好的 PNG 目录重建对比图（不需要重跑转换）。"""
    sheets = []
    for name in sources:
        stem = os.path.splitext(name)[0]
        srcdir = os.path.join(pngdir, f"src_{stem}")
        if not os.path.isdir(srcdir):
            continue
        by_key = {(r.get("mode"), r.get("speed")): r for r in records if r.get("ok") and r["file"].startswith(stem)}
        rows: list[tuple[str, list[str]]] = []
        src_paths = sorted(os.path.join(srcdir, n) for n in os.listdir(srcdir))
        if src_paths:
            rows.append((f"源文件\n{len(src_paths)} 帧", src_paths))
        for speed in SPEEDS:
            for mode in core.SPEED_MODES:
                sub = os.path.join(pngdir, f"out_{stem}_{core.format_speed(speed)}x_{mode}")
                if not os.path.isdir(sub):
                    continue
                paths = sorted(os.path.join(sub, n) for n in os.listdir(sub))
                rec = by_key.get((mode, speed))
                if rec:
                    note = (
                        f"{core.format_speed(speed)}x {mode}\n"
                        f"{rec['out_frames_raw']} 帧 · {rec['out_total_ms']}ms\n"
                        f"延时 {rec['min_delay_ms']}~{rec['max_delay_ms']}ms\n"
                        f"体积 {rec['size_ratio'] * 100:.1f}%"
                    )
                else:
                    note = f"{core.format_speed(speed)}x {mode}\n{len(paths)} 帧"
                rows.append((note, paths))
        sheet_path = os.path.join(outdir, f"compare_{stem}.png")
        contact_sheet(rows, sheet_path, columns=6)
        sheets.append(sheet_path)
    return sheets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="gifspeed 独立验证（ffmpeg 交叉比对）")
    parser.add_argument("--outdir", default=os.path.join(HERE, ".verify"), help="验证产物目录")
    parser.add_argument("--quick", action="store_true", help="每 4 帧只比 1 帧")
    parser.add_argument("--sheets-only", action="store_true",
                        help="不重新转换，只用上次的 .verify/png 和 verify.json 重画对比图")
    parser.add_argument("--json", dest="json_path", default=None, help="把结果写成 JSON")
    args = parser.parse_args(argv)

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("找不到 ffmpeg，无法做独立验证。可用 GIFSPEED_FFMPEG 环境变量指定。")
        return 2

    outdir = os.path.abspath(args.outdir)
    resdir = os.path.join(outdir, "results")
    pngdir = os.path.join(outdir, "png")
    json_path = args.json_path or os.path.join(outdir, "verify.json")

    if args.sheets_only:
        if not os.path.isfile(json_path):
            print(f"找不到 {json_path}，请先完整跑一次验证。")
            return 2
        with open(json_path, encoding="utf-8") as fh:
            records = json.load(fh)["records"]
        for sheet_path in build_sheets(pngdir, outdir, records, SOURCES):
            print(f"对比图: {sheet_path}")
        return 0

    os.makedirs(resdir, exist_ok=True)
    os.makedirs(pngdir, exist_ok=True)

    stride = 4 if args.quick else 1
    records: list[dict] = []
    t0 = time.time()

    for name in SOURCES:
        src_path = os.path.join(HERE, name)
        if not os.path.isfile(src_path):
            print(f"跳过不存在的源文件 {src_path}")
            continue
        stem = os.path.splitext(name)[0]
        print(f"\n=== {name} ===")
        src_pngs = extract_frames(ffmpeg, src_path, os.path.join(pngdir, f"src_{stem}"))
        info = core.load_gif(src_path)
        print(f"  ffmpeg 解出 {len(src_pngs)} 帧；gifspeed 读出 {len(info.frames)} 帧")

        for mode in core.SPEED_MODES:
            for speed in SPEEDS:
                out_name = f"{stem}_{core.format_speed(speed)}x_{mode}.gif"
                out_path = os.path.join(resdir, out_name)
                result = core.convert(src_path, out_path, speed, mode=mode, verify=True)
                if not result.ok:
                    print(f"  [失败] {out_name}: {result.error}")
                    records.append({"file": out_name, "ok": False, "error": result.error})
                    continue

                out_pngs = extract_frames(
                    ffmpeg, out_path, os.path.join(pngdir, f"out_{stem}_{core.format_speed(speed)}x_{mode}")
                )
                indices = list(result.plan.src_indices)
                if mode == "delay":
                    indices = list(range(len(indices)))
                cmp = compare_frames(out_pngs, src_pngs, indices, stride=stride)
                raw = read_gif_raw(out_path)

                record = {
                    "file": out_name,
                    "ok": True,
                    "mode": mode,
                    "speed": speed,
                    "src_frames": len(src_pngs),
                    "out_frames_raw": len(out_pngs),
                    "frames": result.plan.out_frames,
                    "dropped": result.plan.dropped,
                    "src_total_ms": result.plan.src_total_ms,
                    "out_total_ms": result.plan.total_ms,
                    "requested_ms": result.plan.requested_total_ms,
                    "speed_error_ms": result.speed_error_ms,
                    "speed_error_pct": result.speed_error_pct,
                    "achieved_speed": result.plan.achieved_speed,
                    "browser_total_ms": result.plan.browser_total_ms,
                    "browser_effective_speed": result.plan.browser_effective_speed,
                    "min_delay_ms": raw.min_delay_ms,
                    "max_delay_ms": raw.max_delay_ms,
                    "raw_total_ms": raw.total_ms,
                    "zero_delay_frames": raw.zero_delay_frames,
                    "src_size": result.src_info.file_size,
                    "out_size": result.out_size,
                    "size_ratio": result.out_size / max(1, result.src_info.file_size),
                    "pillow_lossless": result.lossless,
                    "pillow_max_diff": result.max_pixel_diff,
                    "ffmpeg_compare": cmp,
                    "warnings": result.warnings,
                }
                records.append(record)
                flag = "无损" if cmp.get("lossless") else f"有差 {cmp.get('max_channel_diff')}"
                print(
                    f"  {core.format_speed(speed):>4}x {mode:<5} "
                    f"{result.plan.out_frames:>3} 帧  时长 {result.plan.total_ms:>6}ms "
                    f"(请求 {result.plan.requested_total_ms:>6}ms, 偏差 {result.speed_error_ms:+5}ms)  "
                    f"ffmpeg 比对 {flag}  体积 {record['size_ratio']*100:5.1f}%"
                )

    report = {
        "ffmpeg": ffmpeg,
        "elapsed_s": round(time.time() - t0, 1),
        "stride": stride,
        "records": records,
    }
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    ok_records = [r for r in records if r.get("ok")]
    bad = [r for r in ok_records if not r["ffmpeg_compare"].get("lossless")]
    print("\n" + "=" * 78)
    print(f"共 {len(records)} 项，成功 {len(ok_records)}，失败 {len(records) - len(ok_records)}")
    print(f"ffmpeg 交叉比对逐像素无损: {len(ok_records) - len(bad)} / {len(ok_records)}")
    if bad:
        for r in bad:
            print(f"  ! {r['file']}: {r['ffmpeg_compare']}")
    print(f"结果已写入 {json_path}")

    for sheet_path in build_sheets(pngdir, outdir, records, SOURCES):
        print(f"对比图: {sheet_path}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""gifspeed.report —— 直接解析 GIF 字节流的独立校验器 + 自检报告。

这里刻意**不依赖 Pillow** 来读文件：Pillow 会替我们"猜测"很多语义（比如把缺失的
GCE 延时补成上一帧的值、自动合成差异帧），而自检需要的是文件里**实际写着的字节**。
所以这里手写一个最小 GIF 解析器，逐块扫描 Extension / Image Descriptor。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Sequence

from .core import (
    BROWSER_CLAMP_MS,
    BROWSER_CLAMP_TO_MS,
    CS_MS,
    ConversionResult,
    format_speed,
)


@dataclass
class RawGifInfo:
    path: str
    signature: bytes = b""
    width: int = 0
    height: int = 0
    global_palette_colors: int = 0
    loop: int | None = None
    delays_ms: list[int] = field(default_factory=list)
    disposals: list[int] = field(default_factory=list)
    transparent_flags: list[int] = field(default_factory=list)
    transparent_indices: list[int] = field(default_factory=list)
    frame_bboxes: list[tuple[int, int, int, int]] = field(default_factory=list)
    comment: str | None = None
    file_size: int = 0
    block_counts: dict[str, int] = field(default_factory=dict)

    @property
    def frame_count(self) -> int:
        return len(self.frame_bboxes)

    @property
    def total_ms(self) -> int:
        return sum(self.delays_ms)

    @property
    def min_delay_ms(self) -> int:
        return min(self.delays_ms) if self.delays_ms else 0

    @property
    def max_delay_ms(self) -> int:
        return max(self.delays_ms) if self.delays_ms else 0

    @property
    def zero_delay_frames(self) -> int:
        """延时写成了 0cs 的帧数 —— 这些帧浏览器会按 100ms 播。"""
        return sum(1 for d in self.delays_ms if d == 0)

    @property
    def clamped_frames(self) -> int:
        return sum(1 for d in self.delays_ms if d < BROWSER_CLAMP_MS)

    @property
    def browser_total_ms(self) -> int:
        return sum(
            BROWSER_CLAMP_TO_MS if d < BROWSER_CLAMP_MS else d for d in self.delays_ms
        )

    @property
    def delta_frames(self) -> int:
        w, h = self.width, self.height
        return sum(1 for b in self.frame_bboxes if b != (0, 0, w, h))

    def delay_histogram(self) -> list[tuple[int, int]]:
        counts: dict[int, int] = {}
        for d in self.delays_ms:
            counts[d] = counts.get(d, 0) + 1
        return sorted(counts.items())


def _skip_subblocks(data: bytes, pos: int) -> int:
    """跳过一串 GIF sub-block（以 0x00 结束）。"""
    n = len(data)
    while pos < n:
        size = data[pos]
        if size == 0:
            return pos + 1
        pos += 1 + size
    return n


def read_gif_raw(path: str) -> RawGifInfo:
    """逐块解析 GIF，取出文件里真实记录的延时 / disposal / 循环次数。"""
    data = open(path, "rb").read()
    info = RawGifInfo(path=path, file_size=len(data))
    if len(data) < 13 or data[:3] != b"GIF":
        raise ValueError(f"{path} 不是 GIF 文件")
    info.signature = data[:6]
    info.width = int.from_bytes(data[6:8], "little")
    info.height = int.from_bytes(data[8:10], "little")
    packed = data[10]
    if packed & 0x80:
        info.global_palette_colors = 2 << (packed & 0x07)
    pos = 13 + (info.global_palette_colors * 3 if info.global_palette_colors else 0)

    counts = {"gce": 0, "netscape": 0, "comment": 0, "application": 0, "other_ext": 0, "image": 0}
    n = len(data)
    while pos < n:
        block = data[pos]
        if block == 0x3B:                                   # Trailer
            break
        if block == 0x21:                                   # Extension Introducer
            label = data[pos + 1] if pos + 1 < n else 0
            if label == 0xF9:                               # Graphic Control Extension
                size = data[pos + 2]
                if size >= 4:
                    gp = data[pos + 3]
                    delay_cs = int.from_bytes(data[pos + 4:pos + 6], "little")
                    info.delays_ms.append(delay_cs * CS_MS)
                    info.disposals.append((gp >> 2) & 0x07)
                    info.transparent_flags.append(gp & 0x01)
                    info.transparent_indices.append(data[pos + 6])
                counts["gce"] += 1
                pos = _skip_subblocks(data, pos + 2)
            elif label == 0xFF:                             # Application Extension
                size = data[pos + 2]
                app = data[pos + 3:pos + 3 + size]
                p = pos + 2 + 1 + size
                if app.startswith(b"NETSCAPE") and p < n and data[p] >= 3:
                    info.loop = int.from_bytes(data[p + 2:p + 4], "little")
                    counts["netscape"] += 1
                else:
                    counts["application"] += 1
                pos = _skip_subblocks(data, p)
            elif label == 0xFE:                             # Comment Extension
                p = pos + 2
                chunks = []
                while p < n and data[p] != 0:
                    ln = data[p]
                    chunks.append(data[p + 1:p + 1 + ln])
                    p += 1 + ln
                if chunks:
                    info.comment = b"".join(chunks).decode("latin-1", "replace")
                counts["comment"] += 1
                pos = _skip_subblocks(data, pos + 2)
            else:
                counts["other_ext"] += 1
                pos = _skip_subblocks(data, pos + 2)
        elif block == 0x2C:                                 # Image Descriptor
            if pos + 10 > n:
                break
            x = int.from_bytes(data[pos + 1:pos + 3], "little")
            y = int.from_bytes(data[pos + 3:pos + 5], "little")
            w = int.from_bytes(data[pos + 5:pos + 7], "little")
            h = int.from_bytes(data[pos + 7:pos + 9], "little")
            info.frame_bboxes.append((x, y, w, h))
            ipacked = data[pos + 9]
            lct = 2 << (ipacked & 0x07) if ipacked & 0x80 else 0
            p = pos + 10 + lct * 3
            p += 1                                          # LZW minimum code size
            counts["image"] += 1
            pos = _skip_subblocks(data, p)
        else:
            break

    # 有些文件没有给所有帧写 GCE，缺失的按前一帧补全，便于统计
    while len(info.delays_ms) < len(info.frame_bboxes):
        info.delays_ms.append(info.delays_ms[-1] if info.delays_ms else 0)
        info.disposals.append(info.disposals[-1] if info.disposals else 0)
        info.transparent_flags.append(0)
        info.transparent_indices.append(0)

    info.block_counts = counts
    return info


# --------------------------------------------------------------------------- #
# 报告渲染
# --------------------------------------------------------------------------- #


def describe_loop(loop: int | None) -> str:
    """把 NETSCAPE 循环数说成人话。

    ``0`` 是无限循环；``None`` 或负数表示文件里**没有** NETSCAPE 扩展，
    也就是只播一次（GIF 的默认行为）。
    """
    if loop is None or loop < 0:
        return "只播一次"
    if loop == 0:
        return "无限"
    return f"{loop} 次"


def format_result(result: ConversionResult, verbose: bool = False) -> str:
    """把单个转换结果渲染成自检报告。"""
    lines: list[str] = []
    if not result.ok:
        lines.append(f"[失败] {os.path.basename(result.src_path)}: {result.error}")
        return "\n".join(lines)

    plan = result.plan
    pct = result.speed_error_pct
    lines.append(
        f"[{'OK ' if result.ok else '!! '}] {os.path.basename(result.out_path)}"
        f"   {format_speed(plan.speed)}x / mode={plan.mode}"
    )
    lines.append(
        f"      帧数 {len(result.src_info.frames)} -> {plan.out_frames}"
        f" (丢 {plan.dropped})   总时长 {plan.src_total_ms}ms -> {plan.total_ms}ms"
        f"   请求 {plan.requested_total_ms:.0f}ms   偏差 {result.speed_error_ms:+.0f}ms ({pct:+.2f}%)"
    )
    lines.append(
        f"      延时 {min(plan.delays_ms)}~{max(plan.delays_ms)}ms"
        f" (分布 {plan_delay_histogram(plan.delays_ms)})"
        f"   实际倍速 {plan.achieved_speed:.3f}x"
        f"   浏览器实际 {plan.browser_total_ms}ms (≈{plan.browser_effective_speed:.2f}x)"
    )
    lines.append(
        f"      体积 {result.src_info.file_size/1024:.0f}KB -> {result.out_size/1024:.0f}KB"
        f" ({result.out_size/max(1,result.src_info.file_size)*100:.0f}%)"
        f"   循环={describe_loop(result.out_loop)}"
        f"   耗时 {result.elapsed_s:.2f}s"
    )

    check_bits = []
    if result.lossless is True:
        check_bits.append("逐像素无损")
    elif result.lossless is False:
        check_bits.append(f"像素有差(最大 {result.max_pixel_diff})")
    else:
        check_bits.append("有损变换(未比对)")
    if result.decoded_frames:
        check_bits.append(f"回读帧数={result.decoded_frames}")
    if result.decoded_total_ms:
        check_bits.append(f"回读时长={result.decoded_total_ms}ms")
    lines.append("      自检: " + " | ".join(check_bits))

    for w in result.warnings:
        lines.append(f"      ! {w}")

    if verbose:
        raw = None
        try:
            raw = read_gif_raw(result.out_path)
        except Exception:
            pass
        if raw is not None:
            lines.append(
                f"      字节级: {raw.signature.decode('latin-1')} "
                f"{raw.width}x{raw.height} 全局色表={raw.global_palette_colors} "
                f"NETSCAPE块={raw.block_counts.get('netscape',0)} "
                f"GCE={raw.block_counts.get('gce',0)} "
                f"差异帧={raw.delta_frames}/{raw.frame_count}"
            )
    return "\n".join(lines)


def plan_delay_histogram(delays: Sequence[int]) -> str:
    counts: dict[int, int] = {}
    for d in delays:
        counts[d] = counts.get(d, 0) + 1
    return ", ".join(f"{d}ms×{c}" for d, c in sorted(counts.items()))


def format_summary(results: Sequence[ConversionResult]) -> str:
    """整批结果的汇总表。"""
    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    lines = ["", "=" * 78, "汇总", "=" * 78]
    lines.append(
        f"成功 {len(ok)} / 失败 {len(bad)} / 共 {len(results)}"
    )
    if ok:
        worst = max(ok, key=lambda r: abs(r.speed_error_pct))
        lines.append(
            f"最大速度偏差: {worst.speed_error_pct:+.2f}% ({os.path.basename(worst.out_path)})"
        )
        lossless = [r for r in ok if r.lossless is True]
        if lossless:
            lines.append(f"逐像素无损: {len(lossless)}/{len([r for r in ok if r.lossless is not None])} 个结果通过比对")
        clamp_risk = [r for r in ok if r.plan.browser_total_ms > r.plan.total_ms * 1.05]
        if clamp_risk:
            lines.append(
                f"注意: {len(clamp_risk)} 个结果的延时低于 {BROWSER_CLAMP_MS}ms，"
                f"在浏览器里会被抬到 {BROWSER_CLAMP_TO_MS}ms 而达不到标称倍速："
            )
            for r in clamp_risk:
                lines.append(f"    - {os.path.basename(r.out_path)}")
    for r in bad:
        lines.append(f"    [失败] {os.path.basename(r.src_path)}: {r.error}")
    lines.append("=" * 78)
    return "\n".join(lines)


def results_to_json(results: Sequence[ConversionResult]) -> list[dict]:
    out = []
    for r in results:
        plan = r.plan
        out.append(
            {
                "src": r.src_path,
                "out": r.out_path,
                "ok": r.ok,
                "error": r.error,
                "speed": plan.speed,
                "mode": plan.mode,
                "src_frames": len(r.src_info.frames),
                "out_frames": plan.out_frames,
                "dropped": plan.dropped,
                "src_total_ms": plan.src_total_ms,
                "out_total_ms": plan.total_ms,
                "requested_total_ms": round(plan.requested_total_ms, 1),
                "achieved_speed": round(plan.achieved_speed, 4),
                "browser_total_ms": plan.browser_total_ms,
                "browser_effective_speed": round(plan.browser_effective_speed, 4),
                "delays_ms": plan.delays_ms,
                "min_delay_ms": min(plan.delays_ms) if plan.delays_ms else None,
                "max_delay_ms": max(plan.delays_ms) if plan.delays_ms else None,
                "loop": r.out_loop,
                "src_size": r.src_info.file_size,
                "out_size": r.out_size,
                "lossless": r.lossless,
                "max_pixel_diff": r.max_pixel_diff,
                "decoded_frames": r.decoded_frames,
                "decoded_total_ms": r.decoded_total_ms,
                "elapsed_s": round(r.elapsed_s, 3),
                "warnings": r.warnings,
            }
        )
    return out

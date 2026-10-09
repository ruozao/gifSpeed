"""gifspeed.core —— GIF 变速核心引擎。

设计要点（都是实测出来的，不是猜的）
------------------------------------
1. GIF 帧延时以 1/100 秒（centisecond, cs）存储，所以任何输出延时都只能是 10ms 的整数倍。
2. 主流浏览器（Chrome / Firefox / WebKit，以及 IE10+）会把 **小于 20ms** 的帧延时
   强行当作 100ms 处理。微软官方文档原文：frames may be shown with a delay as little
   as 20ms. If the server specifies a lower delay, the animation is delayed to 100ms
   for legacy compatibility.
   => 只把延时除以 10 得到的 "10 倍速 GIF"，在浏览器里仍然按原速播放，等于没加速。
   默认模式 ``drop`` 用丢帧把延时守在 >= min_delay_ms，才能得到真正的倍速。
3. 源 GIF 常用「帧间差异」(delta) 压缩，每帧只写变化区域并依赖前一帧的状态。丢帧会
   破坏这种累积关系，所以必须**先合成(coalesce)成完整帧**再重采样。
4. Pillow 保存时用 floor(ms/10) 写 cs：7ms 会写成 0cs，而 0cs 会被浏览器当作 100ms。
   所以输出延时必须做下限保护。
5. 实测：保存时 ``disposal=1`` + ``optimize=True`` 会触发 Pillow 的帧间差异编码，
   体积能减半且逐像素无损（对比 disposal=2 时体积翻倍）。
6. Pillow 报出来的 ``transparency`` 不可靠：实测两个源 GIF 的 GCE 里都有透明标志，
   但 transparent index 在像素直方图里一次都没出现过（GIF1 78 帧 0 次、GIF2 61 帧 0 次）。
   所以 ``load_gif`` 会做一层透明归一化——没被真正用到的透明元数据直接剥掉，
   真被用到的透明像素用背景色填实。这样输出始终是「全不透明完整帧」，
   ``disposal=1`` 就永远是正确且体积最小的选择。
"""

from __future__ import annotations

import contextlib
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from PIL import GifImagePlugin, Image, ImageSequence

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 浏览器把小于该值的帧延时当作 100ms 处理（单位 ms）
BROWSER_CLAMP_MS = 20
#: 浏览器对过小延时的替代值
BROWSER_CLAMP_TO_MS = 100
#: GIF 延时的时间粒度（1cs）
CS_MS = 10
#: 绝对不能写进文件的延时（0cs 会被当作 100ms）
ABSOLUTE_MIN_MS = 10

SPEED_MODES = ("drop", "delay")

#: 只在「每帧局部色表真的不一样」时才转 RGB(A) 的加载策略。
#: Pillow 默认是 RGB_AFTER_FIRST —— 第 2 帧起全部转成 RGBA，交回 GIF 编码器时
#: 会被重新量化：实测 78 帧的 GIF1 因此从每帧 251 色塌成 55 色、逐像素全错。
_PALETTE_STRATEGY = getattr(
    getattr(GifImagePlugin, "LoadingStrategy", None),
    "RGB_AFTER_DIFFERENT_PALETTE_ONLY",
    getattr(GifImagePlugin, "RGB_AFTER_DIFFERENT_PALETTE_ONLY", 2),
)


@contextlib.contextmanager
def _palette_loading():
    """在「保持 P 模式调色板」的加载策略下读 GIF。

    读写两侧都必须用它：默认策略把第 2 帧起转成 RGBA，写出去会被重新量化，
    读回来又会丢掉 alpha 而不是做合成，两边都会得到完全错误的像素。
    """
    previous = GifImagePlugin.LOADING_STRATEGY
    GifImagePlugin.LOADING_STRATEGY = _PALETTE_STRATEGY
    try:
        yield
    finally:
        GifImagePlugin.LOADING_STRATEGY = previous


def format_speed(speed: float) -> str:
    """把倍速格式化成稳定的短字符串：2.0 -> '2'，0.5 -> '0.5'。"""
    text = f"{float(speed):g}"
    return text


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class FrameMeta:
    """单帧的原始元数据。"""

    delay_ms: int
    disposal: int | None = None
    transparency: int | None = None
    tile: tuple[int, int, int, int] | None = None

    @property
    def is_full_frame(self) -> bool:
        return self.tile is None or self.tile[:2] == (0, 0)


@dataclass
class GifInfo:
    """一个 GIF 的完整读取结果（帧已合成为完整画布）。"""

    path: str
    size: tuple[int, int]
    frames: list[Image.Image]
    meta: list[FrameMeta]
    loop: int | None
    background: int | None
    version: bytes | None = None          # b'GIF87a' / b'GIF89a'
    file_size: int = 0

    @property
    def delays(self) -> list[int]:
        return [m.delay_ms for m in self.meta]

    @property
    def total_ms(self) -> int:
        return sum(self.delays)

    @property
    def has_transparency(self) -> bool:
        return any(m.transparency is not None for m in self.meta)

    @property
    def has_delta_frames(self) -> bool:
        return any(not m.is_full_frame for m in self.meta)

    @property
    def uniform_delay(self) -> bool:
        return len(set(self.delays)) <= 1

    def summary(self) -> str:
        w, h = self.size
        d = self.delays
        kind = "全帧" if not self.has_delta_frames else f"差异帧 {sum(1 for m in self.meta if not m.is_full_frame)}/{len(self.meta)}"
        return (
            f"{os.path.basename(self.path)}: {w}x{h}, {len(self.frames)} 帧, "
            f"总时长 {self.total_ms}ms, 延时 {min(d)}~{max(d)}ms, "
            f"循环={'无限' if self.loop == 0 else self.loop}, "
            f"{kind}, 透明={'有' if self.has_transparency else '无'}, "
            f"{self.file_size/1024:.0f}KB"
        )


@dataclass
class Transform:
    """像素/时间轴上的附加变换。全部为 None/False 时走无损直通路径。"""

    reverse: bool = False
    scale: float | None = None
    width: int | None = None
    height: int | None = None
    crop: tuple[int, int, int, int] | None = None
    start_s: float | None = None
    end_s: float | None = None
    colors: int | None = None

    @property
    def touches_pixels(self) -> bool:
        return bool(self.scale or self.width or self.height or self.crop or self.colors)


@dataclass
class SpeedPlan:
    """变速的时间轴方案。"""

    mode: str
    speed: float
    src_indices: list[int]        # 输出帧 -> 源帧下标
    delays_ms: list[int]          # 输出帧延时（10ms 整数倍）
    src_total_ms: int
    requested_total_ms: float
    dropped: int                  # 被丢掉的源帧数

    @property
    def out_frames(self) -> int:
        return len(self.delays_ms)

    @property
    def total_ms(self) -> int:
        return sum(self.delays_ms)

    @property
    def achieved_speed(self) -> float:
        return self.src_total_ms / self.total_ms if self.total_ms else float("inf")

    @property
    def browser_total_ms(self) -> int:
        """浏览器实际会播多久（考虑 <20ms 被抬到 100ms）。"""
        return sum(
            BROWSER_CLAMP_TO_MS if d < BROWSER_CLAMP_MS else d for d in self.delays_ms
        )

    @property
    def browser_effective_speed(self) -> float:
        t = self.browser_total_ms
        return self.src_total_ms / t if t else float("inf")


@dataclass
class EncodeOptions:
    loop: int | None = None            # None = 沿用源文件
    optimize: bool = True
    disposal: int | None = None        # None = 自动（1 = 不还原，帧间差异压缩）

    def resolved_loop(self, src: GifInfo) -> int | None:
        return src.loop if self.loop is None else self.loop

    def resolved_disposal(self, src: GifInfo) -> int:
        if self.disposal is not None:
            return self.disposal
        # load_gif 已把帧归一化成「全不透明完整帧」（见模块 docstring 第 6 条），
        # 所以 do-not-dispose 永远是正确选择，而且只有它能让 Pillow 做帧间差异
        # 压缩——实测同样内容 disposal=2 的体积是它的 2.15 倍。
        return 1


@dataclass
class ConversionResult:
    src_path: str
    out_path: str
    plan: SpeedPlan
    src_info: GifInfo
    warnings: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    out_size: int = 0
    out_loop: int | None = None
    decoded_frames: int = 0
    decoded_total_ms: int = 0
    decoded_delays: list[int] = field(default_factory=list)
    lossless: bool | None = None       # None = 未做逐像素比对（有损变换）
    max_pixel_diff: int | None = None
    ok: bool = True
    error: str | None = None

    @property
    def speed_error_ms(self) -> float:
        return self.plan.total_ms - self.plan.requested_total_ms

    @property
    def speed_error_pct(self) -> float:
        if not self.plan.requested_total_ms:
            return 0.0
        return 100.0 * self.speed_error_ms / self.plan.requested_total_ms


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #


def load_gif(path: str, progress: Callable[[str], None] | None = None) -> GifInfo:
    """读取 GIF 并把每一帧合成为完整画布（coalesce）。

    Pillow 的 GifImagePlugin 在 seek 时会按 disposal 把新帧贴到上一帧上，
    因此 ImageSequence 迭代出来的就是合成后的整帧，正好是我们重采样需要的形态。
    读完之后还会做两次归一化：先把所有帧统一到同一张 P 模式色表
    （见 :func:`_unify_palette`，否则编码器会重新量化），再把「僵尸透明标志」
    清掉（见 :func:`_normalize_transparency`）。
    """
    say = progress or (lambda _m: None)
    with _palette_loading():
        with Image.open(path) as im:
            version = im.info.get("version")
            loop = im.info.get("loop")
            background = im.info.get("background")
            size = im.size
            default_delay = im.info.get("duration", 100)
            frames: list[Image.Image] = []
            meta: list[FrameMeta] = []
            for fr in ImageSequence.Iterator(im):
                frames.append(fr.copy())      # copy() 带上当前帧的 P 调色板
                tile = fr.tile[0][1] if fr.tile else None
                meta.append(
                    FrameMeta(
                        delay_ms=int(fr.info.get("duration", default_delay) or default_delay),
                        disposal=fr.info.get("disposal"),
                        transparency=fr.info.get("transparency"),
                        tile=tuple(tile) if tile else None,
                    )
                )
    frames = _unify_palette(frames, say)
    filled = _normalize_transparency(frames, meta, background)
    if filled:
        say(f"透明归一化：{filled} 个透明像素用背景色填实")
    say(f"读取 {os.path.basename(path)}: {len(frames)} 帧, {size[0]}x{size[1]}")
    return GifInfo(
        path=path,
        size=size,
        frames=frames,
        meta=meta,
        loop=loop,
        background=background,
        version=version if isinstance(version, bytes) else None,
        file_size=os.path.getsize(path),
    )


def _unify_palette(
    frames: list[Image.Image],
    say: Callable[[str], None],
) -> list[Image.Image]:
    """让所有帧共用同一张 P 模式色表。

    Pillow 的 GIF 编码器在 ``optimize=True`` 时用「调色板下标」比较相邻帧来生成
    差异帧，所以只要两帧的色表不同，下标相等就**不代表颜色相等**，编出来的帧会
    整片错色。这里保证进入编码器的帧永远是「同一张色表 + P 模式」。

    正常路径（源文件只有一张全局色表）什么都不用做——加载策略已经保证了这一点。
    兜底路径（源文件真的用了每帧局部色表）把非 P 帧按参考色表重新索引；
    因为要取最近色，这一步可能轻微改变画面，所以会打印一条提示。
    """
    if not frames:
        return frames
    reference = next((f.getpalette() for f in frames if f.mode == "P" and f.getpalette()), None)
    palettes = {bytes(f.getpalette() or b"") for f in frames if f.mode == "P"}
    if reference and palettes == {bytes(reference)} and all(f.mode == "P" for f in frames):
        return frames

    if not reference:
        say("警告：源文件没有可用的全局色表，保留原始帧不做调色板统一")
        return frames

    ref_image = Image.new("P", (1, 1))
    ref_image.putpalette(reference)
    say("源文件含多张局部色表或非 P 模式帧，正按参考色表重新索引（可能有细微色差）")
    unified: list[Image.Image] = []
    for frame in frames:
        if frame.mode == "P" and bytes(frame.getpalette() or b"") == bytes(reference):
            unified.append(frame)
        else:
            unified.append(
                frame.convert("RGB").quantize(palette=ref_image, dither=Image.Dither.NONE)
            )
    return unified


def _normalize_transparency(
    frames: list[Image.Image],
    meta: list[FrameMeta],
    background_index: int | None,
) -> int:
    """让合成后的完整帧变成真正的不透明帧，返回被填实的像素数。

    GIF 的 GCE 里可以声明一个 transparent index，但**声明了不等于用到了**。
    实测本工作区的两个源 GIF：GCE 透明标志到处都是，可是 transparent index
    在所有帧的像素直方图里出现次数都是 0。这种「僵尸透明标志」如果原样带进
    输出，会让编码器写出多余的透明 GCE、把 disposal 选错，甚至让解码器把
    上一帧的内容透出来。处理规则：

    * transparent index 没被像素用到 -> 直接丢掉透明元数据（无损）；
    * 真被用到了 -> 用背景色调色板项填实（这一步理论上会改变画面，
      但源文件本来就没有「透到底」的语义，填背景色才是浏览器实际显示的样子）。
    """
    filled = 0
    for frame, info in zip(frames, meta):
        t = info.transparency
        if t is None:
            continue
        info.transparency = None
        frame.info.pop("transparency", None)
        if frame.mode != "P" or not isinstance(t, int) or not 0 <= t <= 255:
            continue
        try:
            used = frame.histogram()[t]
        except (IndexError, ValueError):
            used = 0
        if used == 0:
            continue
        palette = frame.getpalette() or []
        bg = background_index if isinstance(background_index, int) else 0
        if not 0 <= bg <= 255 or len(palette) < (bg + 1) * 3:
            bg = 0
        mask = frame.point(
            lambda v, _t=t: 255 if v == _t else 0, mode="L"
        ).convert("1", dither=Image.Dither.NONE)
        frame.paste(Image.new("P", frame.size, bg), (0, 0), mask)
        filled += used
    return filled


# --------------------------------------------------------------------------- #
# 时间轴规划
# --------------------------------------------------------------------------- #


def _frame_at(bounds: Sequence[float], t: float) -> int:
    """返回在累积时间轴上覆盖时刻 t 的源帧下标。"""
    import bisect

    i = bisect.bisect_right(bounds, t) - 1
    if i < 0:
        i = 0
    elif i > len(bounds) - 2:
        i = len(bounds) - 2
    return i


def plan_speed(
    delays: Sequence[int],
    speed: float,
    mode: str = "drop",
    min_delay_ms: int = BROWSER_CLAMP_MS,
) -> SpeedPlan:
    """把源帧延时表换算成输出帧延时表（+ 需要保留哪些源帧）。

    ``drop`` 模式
        把整条时间轴按 1/speed 压缩，然后以 ``min_delay_ms`` 为最小步长重新采样。
        当 1/speed 压缩后单帧时长不足 ``min_delay_ms`` 时，采样点会自然跳过部分源帧，
        于是总时长精确等于 目标时长，且任何一帧延时都 >= min_delay_ms —— 浏览器里是
        真正的倍速。代价是高倍速下帧数变少。

    ``delay`` 模式
        保留全部帧，只把每帧延时除以 speed。画面最平滑，但延时可能 < 20ms，
        浏览器会把它抬到 100ms —— 也就是说这个文件在主浏览器里可能并没有变快。
        结果里会给出 browser_total_ms / browser_effective_speed 以便判断。
    """
    if speed <= 0:
        raise ValueError("倍速必须为正数")
    mode = mode.lower()
    if mode not in SPEED_MODES:
        raise ValueError(f"未知模式 {mode!r}，可选 {SPEED_MODES}")

    n = len(delays)
    if n == 0:
        raise ValueError("源 GIF 没有帧")

    src_total = int(sum(delays))

    if mode == "delay":
        # delay 模式的本意是保住每一帧，所以下限只能是 1cs；再低只会写成 0cs，
        # 而 0cs 会被浏览器当作 100ms —— 那才是真正的"翻车"。
        floor_ms = ABSOLUTE_MIN_MS
        out: list[int] = []
        acc = 0.0
        for d in delays:
            acc += d / speed
            ms = int(acc // CS_MS) * CS_MS
            if ms < floor_ms:
                ms = floor_ms
            acc -= ms
            out.append(ms)
        return SpeedPlan(
            mode=mode,
            speed=speed,
            src_indices=list(range(n)),
            delays_ms=out,
            src_total_ms=src_total,
            requested_total_ms=src_total / speed,
            dropped=0,
        )

    # ---- drop 模式 -------------------------------------------------------- #
    floor_ms = int(min_delay_ms)
    if floor_ms < ABSOLUTE_MIN_MS:
        floor_ms = ABSOLUTE_MIN_MS
    floor_ms = max(CS_MS, (floor_ms // CS_MS) * CS_MS)   # 必须是 10ms 的整数倍

    target_ms = src_total / speed
    n_out = int(target_ms // floor_ms)
    if n_out < 1:
        n_out = 1
    if n_out > n:
        n_out = n                # 不复制帧（减速靠拉长延时，不靠补帧）
    step = target_ms / n_out

    bounds = [0.0]
    for d in delays:
        bounds.append(bounds[-1] + float(d))

    src_indices: list[int] = []
    for j in range(n_out):
        # 取每个输出帧时间片的中点，稳定地映射回源时间轴
        src_t = (j * step + step * 0.5) * speed
        src_indices.append(_frame_at(bounds, src_t))

    # 用误差扩散把浮点步长量化成 10ms 整数倍，保证累计误差不漂移
    delays_out: list[int] = []
    acc = 0.0
    for _ in range(n_out):
        acc += step
        ms = int(acc // CS_MS) * CS_MS
        if ms < floor_ms:
            ms = floor_ms
        acc -= ms
        delays_out.append(ms)

    return SpeedPlan(
        mode=mode,
        speed=speed,
        src_indices=src_indices,
        delays_ms=delays_out,
        src_total_ms=src_total,
        requested_total_ms=target_ms,
        dropped=n - len(set(src_indices)),
    )


def trim_range(delays: Sequence[int], start_s: float | None, end_s: float | None) -> tuple[int, int]:
    """按秒把 [start, end) 映射成源帧下标区间。"""
    n = len(delays)
    if n == 0:
        return 0, 0
    bounds = [0]
    for d in delays:
        bounds.append(bounds[-1] + d)
    total = bounds[-1]
    t0 = 0.0 if start_s is None else max(0.0, float(start_s) * 1000.0)
    t1 = float(total) if end_s is None else min(float(total), float(end_s) * 1000.0)
    if t1 <= t0:
        raise ValueError("截取区间为空：--end 必须大于 --start")
    first = _frame_at(bounds, t0)
    last = _frame_at(bounds, max(t0, t1 - 0.001))
    return first, last + 1


# --------------------------------------------------------------------------- #
# 像素变换
# --------------------------------------------------------------------------- #


def _apply_crop(frames: list[Image.Image], box: tuple[int, int, int, int]) -> list[Image.Image]:
    out = []
    for f in frames:
        w, h = f.size
        x0, y0, x1, y1 = box
        x0 = max(0, min(x0, w - 1))
        y0 = max(0, min(y0, h - 1))
        x1 = max(x0 + 1, min(x1, w))
        y1 = max(y0 + 1, min(y1, h))
        out.append(f.crop((x0, y0, x1, y1)))
    return out


def _apply_scale(
    frames: list[Image.Image],
    scale: float | None,
    width: int | None,
    height: int | None,
) -> list[Image.Image]:
    w0, h0 = frames[0].size
    if width and height:
        w1, h1 = int(width), int(height)
    elif width:
        w1 = int(width)
        h1 = max(1, round(h0 * w1 / w0))
    elif height:
        h1 = int(height)
        w1 = max(1, round(w0 * h1 / h0))
    else:
        s = float(scale or 1.0)
        w1 = max(1, round(w0 * s))
        h1 = max(1, round(h0 * s))
    if (w1, h1) == (w0, h0):
        return frames
    return [f.convert("RGB").resize((w1, h1), Image.LANCZOS) for f in frames]


def _apply_colors(frames: list[Image.Image], colors: int) -> list[Image.Image]:
    """按统一调色板减色（有损，仅在用户显式指定时调用）。"""
    rgb = [f.convert("RGB") for f in frames]
    # 用一帧抽样的合成图生成全局调色板，避免每帧一张局部色表导致体积膨胀
    sample = rgb[0] if len(rgb) == 1 else Image.blend(
        rgb[0].resize((max(1, rgb[0].width // 2), max(1, rgb[0].height // 2))),
        rgb[len(rgb) // 2].resize((max(1, rgb[0].width // 2), max(1, rgb[0].height // 2))),
        0.5,
    )
    palette_img = sample.quantize(colors=colors, method=Image.MEDIANCUT)
    return [f.quantize(palette=palette_img, dither=Image.FLOYDSTEINBERG) for f in rgb]


# --------------------------------------------------------------------------- #
# 保存
# --------------------------------------------------------------------------- #


def save_gif(
    frames: list[Image.Image],
    delays_ms: Sequence[int],
    out_path: str,
    loop: int | None,
    *,
    optimize: bool = True,
    disposal: int = 1,
    background: int | None = None,
    comment: str | None = None,
) -> None:
    if not frames:
        raise ValueError("没有可保存的帧")
    if len(frames) != len(delays_ms):
        raise ValueError("帧数与延时数不一致")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    kwargs: dict = dict(
        save_all=True,
        append_images=list(frames[1:]),
        duration=[int(d) for d in delays_ms],
        optimize=optimize,
        disposal=disposal,
    )
    # Pillow 用 struct 'H' 写 NETSCAPE 循环数，负数会直接抛
    # "'H' format requires 0 <= number <= 65535"。而 GIF 里「只播一次」的正确表达
    # 就是**根本不写** NETSCAPE 扩展，所以 loop 为 None 或负数时直接不传。
    if loop is not None and 0 <= loop <= 65535:
        kwargs["loop"] = int(loop)
    if background is not None:
        kwargs["background"] = int(background)
    if comment:
        kwargs["comment"] = comment.encode("ascii", "replace")
    frames[0].save(out_path, **kwargs)


# --------------------------------------------------------------------------- #
# 逐像素自检
# --------------------------------------------------------------------------- #


def pixel_diff(
    out_path: str,
    expected: Sequence[Image.Image],
    sample_stride: int = 1,
) -> tuple[bool, int]:
    """把写出的文件解码回来，和内存里期望的帧逐像素比对。

    返回 (是否逐像素一致, 最大通道差)。两个坑都要避开：

    * ``ImageSequence.Iterator`` 对多帧文件每次都 yield **同一个对象**，
      不 ``copy()`` 的话整个列表都会指向最后一帧；
    * 必须用 :func:`_palette_loading`，否则第 2 帧起被读成 RGBA，
      ``convert("RGB")`` 只是丢掉 alpha 而不是做合成，会误报整片像素错误。
    """
    from PIL import ImageChops

    max_diff = 0
    with _palette_loading(), Image.open(out_path) as im:
        decoded = [fr.copy() for fr in ImageSequence.Iterator(im)]
    if len(decoded) != len(expected):
        return False, 255
    for i, fr in enumerate(decoded):
        if sample_stride > 1 and (i % sample_stride):
            continue
        a = fr.convert("RGB")
        b = expected[i].convert("RGB")
        if a.size != b.size:
            return False, 255
        diff = ImageChops.difference(a, b)
        local = max(hi for _lo, hi in diff.getextrema())
        if local > max_diff:
            max_diff = local
        if local:
            return False, max_diff
    return True, max_diff


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def output_name(src_path: str, speed: float, template: str | None = None) -> str:
    stem = os.path.splitext(os.path.basename(src_path))[0]
    ext = os.path.splitext(src_path)[1] or ".gif"
    if template:
        name = template.format(stem=stem, speed=format_speed(speed), ext=ext.lstrip("."))
        if not name.lower().endswith((".gif", ".gifv")):
            name += ext
        return name
    return f"{stem}_{format_speed(speed)}x{ext}"


def convert(
    src_path: str,
    out_path: str,
    speed: float,
    *,
    mode: str = "drop",
    min_delay_ms: int = BROWSER_CLAMP_MS,
    transform: Transform | None = None,
    encode: EncodeOptions | None = None,
    verify: bool = True,
    progress: Callable[[str], None] | None = None,
) -> ConversionResult:
    """把一个 GIF 按指定倍速转换并写到 out_path。"""
    say = progress or (lambda _m: None)
    transform = transform or Transform()
    encode = encode or EncodeOptions()
    t_start = time.perf_counter()

    info = load_gif(src_path, progress=say)

    # 1) 时间轴截取
    lo, hi = trim_range(info.delays, transform.start_s, transform.end_s)
    if (lo, hi) != (0, len(info.delays)):
        say(f"截取第 {lo}~{hi-1} 帧（{lo and sum(info.delays[:lo])}ms ~ {sum(info.delays[:hi])}ms）")
    src_frames = info.frames[lo:hi]
    src_delays = info.delays[lo:hi]
    if not src_frames:
        raise ValueError("截取后没有剩余帧")

    # 2) 变速规划
    plan = plan_speed(src_delays, speed, mode=mode, min_delay_ms=min_delay_ms)
    say(
        f"{format_speed(speed)}x [{mode}] -> {plan.out_frames} 帧, "
        f"总时长 {plan.total_ms}ms (目标 {plan.requested_total_ms:.0f}ms)"
    )

    frames = [src_frames[i] for i in plan.src_indices]
    delays = list(plan.delays_ms)

    # 3) 反向
    if transform.reverse:
        frames = frames[::-1]
        delays = delays[::-1]
        say("已反向播放")

    # 4) 像素变换
    if transform.crop:
        frames = _apply_crop(frames, transform.crop)
        say(f"裁剪到 {frames[0].size[0]}x{frames[0].size[1]}")
    if transform.scale or transform.width or transform.height:
        frames = _apply_scale(frames, transform.scale, transform.width, transform.height)
        say(f"缩放到 {frames[0].size[0]}x{frames[0].size[1]}")
    if transform.colors:
        frames = _apply_colors(frames, int(transform.colors))
        say(f"减色到 {int(transform.colors)} 色（有损）")

    # 5) 保存
    loop_value = encode.resolved_loop(info)
    disposal = encode.resolved_disposal(info)
    comment = (
        f"gifspeed {format_speed(speed)}x mode={mode} "
        f"src_frames={len(src_frames)} out_frames={plan.out_frames}"
    )
    save_gif(
        frames,
        delays,
        out_path,
        loop_value,
        optimize=encode.optimize,
        disposal=disposal,
        background=info.background,
        comment=comment,
    )

    result = ConversionResult(
        src_path=src_path,
        out_path=out_path,
        plan=plan,
        src_info=info,
        out_size=os.path.getsize(out_path),
        out_loop=loop_value,
    )

    # 6) 警告
    if mode == "delay" and min(delays) < BROWSER_CLAMP_MS:
        result.warnings.append(
            f"最小延时 {min(delays)}ms < {BROWSER_CLAMP_MS}ms：主浏览器会把它当作 "
            f"{BROWSER_CLAMP_TO_MS}ms，实际播出时长约 {plan.browser_total_ms}ms "
            f"(≈{plan.browser_effective_speed:.2f}x)，并不是请求的 {format_speed(speed)}x。"
            f"需要真倍速请改用 --mode drop。"
        )
    if abs(plan.achieved_speed - speed) / speed > 0.02:
        result.warnings.append(
            f"实际倍速 {plan.achieved_speed:.3f}x 与请求 {format_speed(speed)}x 有偏差："
            f"GIF 延时最小刻度是 10ms、且浏览器下限 {BROWSER_CLAMP_MS}ms，"
            f"极端倍速无法精确达到。"
        )
    if plan.dropped and info.has_delta_frames:
        result.warnings.append(
            f"源文件含差异帧，已先合成为完整帧再丢帧（否则画面会撕裂）。"
        )

    # 7) 写回读校验
    if verify:
        from .report import read_gif_raw

        raw = read_gif_raw(out_path)
        result.decoded_frames = raw.frame_count
        result.decoded_delays = raw.delays_ms
        result.decoded_total_ms = sum(raw.delays_ms)
        if raw.frame_count != plan.out_frames:
            result.warnings.append(
                f"编码器把帧数从 {plan.out_frames} 改成了 {raw.frame_count}（相同帧被合并）"
            )
        if raw.delays_ms != delays:
            result.warnings.append("写回的延时表与预期不一致，请检查。")
        if not transform.touches_pixels and not transform.reverse:
            ok, diff = pixel_diff(out_path, frames)
            result.lossless = ok
            result.max_pixel_diff = diff
            if not ok:
                result.warnings.append(f"逐像素比对不一致，最大通道差 {diff}")

    result.elapsed_s = time.perf_counter() - t_start
    return result


def convert_many(
    src_paths: Iterable[str],
    speeds: Sequence[float],
    outdir: str | None = None,
    *,
    name_template: str | None = None,
    mode: str = "drop",
    min_delay_ms: int = BROWSER_CLAMP_MS,
    transform: Transform | None = None,
    encode: EncodeOptions | None = None,
    verify: bool = True,
    overwrite: bool = True,
    progress: Callable[[str], None] | None = None,
) -> list[ConversionResult]:
    """批量转换：每个输入 × 每个倍速 产出一个文件。"""
    results: list[ConversionResult] = []
    src_paths = list(src_paths)
    total = len(src_paths) * len(speeds)
    done = 0
    for src in src_paths:
        for speed in speeds:
            done += 1
            base_dir = outdir if outdir else os.path.dirname(os.path.abspath(src))
            out_path = os.path.join(base_dir, output_name(src, speed, name_template))
            if progress:
                progress(f"[{done}/{total}] {os.path.basename(src)} @ {format_speed(speed)}x")
            if not overwrite and os.path.exists(out_path):
                if progress:
                    progress(f"  跳过（已存在）: {os.path.basename(out_path)}")
                continue
            try:
                results.append(
                    convert(
                        src,
                        out_path,
                        speed,
                        mode=mode,
                        min_delay_ms=min_delay_ms,
                        transform=transform,
                        encode=encode,
                        verify=verify,
                        progress=progress,
                    )
                )
            except Exception as exc:  # 单个失败不影响整批
                failed = ConversionResult(
                    src_path=src,
                    out_path=out_path,
                    plan=SpeedPlan(mode, speed, [], [], 0, 0.0, 0),
                    src_info=GifInfo(src, (0, 0), [], [], None, None),
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
                failed.warnings.append(f"转换失败：{exc}")
                results.append(failed)
                if progress:
                    progress(f"  !! 失败: {exc}")
    return results

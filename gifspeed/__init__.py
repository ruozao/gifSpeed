"""gifspeed —— GIF 变速工具（CLI + tkinter 桌面窗口）。"""

import sys as _sys

try:
    from .core import (
        ABSOLUTE_MIN_MS,
        BROWSER_CLAMP_MS,
        BROWSER_CLAMP_TO_MS,
        CS_MS,
        ConversionResult,
        EncodeOptions,
        FrameMeta,
        GifInfo,
        SpeedPlan,
        Transform,
        convert,
        convert_many,
        format_speed,
        load_gif,
        output_name,
        pixel_diff,
        plan_speed,
        save_gif,
        trim_range,
    )
    from .report import (
        RawGifInfo,
        format_result,
        format_summary,
        read_gif_raw,
        results_to_json,
    )
except ImportError as _exc:  # pragma: no cover - 只在缺依赖时走到
    if "PIL" not in str(_exc):
        raise
    raise SystemExit(
        "gifspeed 需要 Pillow，但当前这个 Python 里没有装。\n"
        f"  当前解释器：{_sys.executable}\n"
        f"  具体错误：{_exc}\n"
        "\n"
        "两种解决办法，任选其一：\n"
        f"  1) 给它装上：  \"{_sys.executable}\" -m pip install Pillow\n"
        "  2) 换个已经装了 Pillow 的解释器来跑，例如：\n"
        '       "C:\\Users\\<你>\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe" -m gifspeed --help\n'
        "\n"
        "本目录下的 gifspeed.cmd 会自动帮你挑一个能用的解释器。\n"
    ) from _exc

__version__ = "1.0.0"

__all__ = [
    "ABSOLUTE_MIN_MS",
    "BROWSER_CLAMP_MS",
    "BROWSER_CLAMP_TO_MS",
    "CS_MS",
    "ConversionResult",
    "EncodeOptions",
    "FrameMeta",
    "GifInfo",
    "RawGifInfo",
    "SpeedPlan",
    "Transform",
    "convert",
    "convert_many",
    "format_result",
    "format_speed",
    "format_summary",
    "load_gif",
    "output_name",
    "pixel_diff",
    "plan_speed",
    "read_gif_raw",
    "results_to_json",
    "save_gif",
    "trim_range",
    "__version__",
]

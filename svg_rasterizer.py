"""ComfyUI node for rasterizing native SVG values to IMAGE and MASK tensors."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from io import BytesIO
from typing import Any

import numpy as np
import torch
from PIL import Image


MIN_DIMENSION = 64
MAX_DIMENSION = 16384
BACKGROUND_OPTIONS = ("transparent", "white", "black")


def _read_file_like(value: Any) -> bytes | str:
    """Read a stream without permanently changing its current position."""
    position = None
    try:
        position = value.tell()
    except (AttributeError, OSError, ValueError):
        pass

    try:
        try:
            value.seek(0)
        except (AttributeError, OSError, ValueError):
            pass
        return value.read()
    finally:
        if position is not None:
            try:
                value.seek(position)
            except (AttributeError, OSError, ValueError):
                pass


def _as_svg_bytes(value: Any) -> bytes:
    """Convert one supported SVG payload to bytes."""
    if hasattr(value, "read") and callable(value.read):
        value = _read_file_like(value)

    if isinstance(value, str):
        data = value.encode("utf-8")
    elif isinstance(value, bytes):
        data = value
    elif isinstance(value, (bytearray, memoryview)):
        data = bytes(value)
    else:
        raise TypeError(
            "unsupported SVG payload representation "
            f"{type(value).__module__}.{type(value).__qualname__}"
        )

    if not data.strip():
        raise ValueError("SVG payload is empty")
    return data


def _extract_svg_documents(svg: Any) -> list[bytes]:
    """Extract SVG documents, prioritizing ComfyUI's native SVG.data wrapper."""
    if svg is None:
        raise ValueError("SVG input is empty")

    if isinstance(svg, (str, bytes, bytearray, memoryview)) or (
        hasattr(svg, "read") and callable(svg.read)
    ):
        payloads = [svg]
    elif isinstance(svg, Mapping):
        for key in ("data", "svg", "content"):
            if key in svg:
                payloads = svg[key]
                break
        else:
            raise TypeError(
                "unsupported SVG mapping; expected a 'data', 'svg', or 'content' key"
            )
    elif hasattr(svg, "data"):
        # Current ComfyUI: comfy_api.latest._util.image_types.SVG, whose data
        # member is a list of BytesIO objects (one stream per batch item).
        payloads = svg.data
    else:
        raise TypeError(
            "unsupported SVG object representation "
            f"{type(svg).__module__}.{type(svg).__qualname__}"
        )

    if isinstance(payloads, Sequence) and not isinstance(
        payloads, (str, bytes, bytearray, memoryview)
    ):
        items = list(payloads)
    else:
        items = [payloads]

    if not items:
        raise ValueError("SVG input contains no documents")

    documents = []
    for index, item in enumerate(items):
        try:
            documents.append(_as_svg_bytes(item))
        except (TypeError, ValueError) as exc:
            raise type(exc)(f"SVG item {index}: {exc}") from exc
    return documents


def _validate_dimension(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"Invalid output dimensions: {name} must be an integer")
    value = int(value)
    if not MIN_DIMENSION <= value <= MAX_DIMENSION:
        raise ValueError(
            f"Invalid output dimensions: {name} must be between "
            f"{MIN_DIMENSION} and {MAX_DIMENSION}, got {value}"
        )
    return value


def _render_one(svg_bytes: bytes, width: int, height: int, background: str):
    try:
        # Import lazily so ComfyUI can still load the node and report a focused
        # installation error if the optional renderer is unavailable.
        import resvg_py

        try:
            svg_text = svg_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"SVG document is not valid UTF-8: {exc}") from exc

        png_bytes = resvg_py.svg_to_bytes(
            svg_string=svg_text,
            width=width,
            height=height,
        )
        with Image.open(BytesIO(png_bytes)) as png:
            rgba = png.convert("RGBA")
            rgba.load()
    except ImportError as exc:
        raise RuntimeError(
            "Failed to rasterize SVG: resvg_py is unavailable. Install the "
            "package requirements in ComfyUI's Python environment. "
            f"Original error: {exc}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to rasterize SVG: {exc}") from exc

    if rgba.size != (width, height):
        if rgba.width > width or rgba.height > height:
            raise RuntimeError(
                "Failed to rasterize SVG: resvg_py returned an oversized image "
                f"{rgba.width}x{rgba.height} for a {width}x{height} canvas"
            )

        # resvg preserves the SVG aspect ratio and may return a fitted image
        # smaller than the requested canvas on one axis. Center that directly
        # rendered image on a transparent canvas; no bitmap resize is used.
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        offset = ((width - rgba.width) // 2, (height - rgba.height) // 2)
        canvas.alpha_composite(rgba, dest=offset)
        rgba = canvas

    alpha = np.asarray(rgba.getchannel("A"), dtype=np.float32) / 255.0

    if background == "transparent":
        rgb = rgba.convert("RGB")
    else:
        fill = (255, 255, 255, 255) if background == "white" else (0, 0, 0, 255)
        canvas = Image.new("RGBA", rgba.size, fill)
        rgb = Image.alpha_composite(canvas, rgba).convert("RGB")

    image = np.asarray(rgb, dtype=np.float32) / 255.0
    # Copies make the tensors own writable, contiguous memory independent of PIL.
    return torch.from_numpy(image.copy()), torch.from_numpy(alpha.copy())


class SVGRasterizer:
    """Rasterize native ComfyUI SVG batches directly at the target resolution."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "svg": ("SVG",),
                "width": (
                    "INT",
                    {"default": 6144, "min": MIN_DIMENSION, "max": MAX_DIMENSION},
                ),
                "height": (
                    "INT",
                    {"default": 6144, "min": MIN_DIMENSION, "max": MAX_DIMENSION},
                ),
                "background": (list(BACKGROUND_OPTIONS), {"default": "white"}),
            }
        }

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("image", "mask")
    FUNCTION = "rasterize"
    CATEGORY = "SVG"

    def rasterize(self, svg, width=6144, height=6144, background="white"):
        width = _validate_dimension("width", width)
        height = _validate_dimension("height", height)
        if background not in BACKGROUND_OPTIONS:
            raise ValueError(
                f"Invalid background {background!r}; expected one of "
                f"{', '.join(BACKGROUND_OPTIONS)}"
            )

        try:
            documents = _extract_svg_documents(svg)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid SVG input: {exc}") from exc

        images = []
        masks = []
        for index, document in enumerate(documents):
            try:
                image, mask = _render_one(document, width, height, background)
            except RuntimeError as exc:
                if len(documents) == 1:
                    raise
                raise RuntimeError(f"{exc} (batch item {index})") from exc
            images.append(image)
            masks.append(mask)

        return torch.stack(images, dim=0), torch.stack(masks, dim=0)

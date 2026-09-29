"""ComfyUI SVG Rasterizer package registration and dependency bootstrap."""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path


_REQUIRED_MODULES = {
    "resvg_py": "resvg_py",
    "PIL": "pillow",
    "numpy": "numpy",
}


def _ensure_dependencies() -> None:
    missing = [
        package
        for module, package in _REQUIRED_MODULES.items()
        if importlib.util.find_spec(module) is None
    ]
    if not missing:
        return

    requirements = Path(__file__).with_name("requirements.txt")
    print(
        "[ComfyUI SVG Rasterizer] Missing dependencies: "
        f"{', '.join(missing)}. Installing from {requirements}...",
        flush=True,
    )

    try:
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-warn-script-location",
                "-r",
                str(requirements),
            ]
        )
        importlib.invalidate_caches()
        still_missing = [
            package
            for module, package in _REQUIRED_MODULES.items()
            if importlib.util.find_spec(module) is None
        ]
        if still_missing:
            raise RuntimeError(
                "pip completed, but these packages are still unavailable: "
                f"{', '.join(still_missing)}"
            )
        print(
            "[ComfyUI SVG Rasterizer] Dependencies installed successfully.",
            flush=True,
        )
    except Exception as exc:
        # Keep package loading whenever ComfyUI's existing NumPy/Pillow are
        # available. The node will report a focused error if resvg_py is used.
        print(
            "[ComfyUI SVG Rasterizer] Automatic dependency installation "
            f"failed: {exc}\n"
            "Install manually with ComfyUI's Python: "
            f'"{sys.executable}" -m pip install -r "{requirements}"',
            flush=True,
        )


_ensure_dependencies()


from .svg_rasterizer import SVGRasterizer  # noqa: E402


NODE_CLASS_MAPPINGS = {
    "ComfyUISVGRasterizer": SVGRasterizer,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ComfyUISVGRasterizer": "SVG Rasterizer",
}


__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

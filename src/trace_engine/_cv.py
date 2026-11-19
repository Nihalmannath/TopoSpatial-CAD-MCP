"""OpenCV when installed, otherwise a small NumPy/SciPy/Pillow stand-in.

The tracer only needs a handful of OpenCV calls. Shipping this fallback means
``manage_trace`` works in the stock TopoSpatial environment (which has
numpy, scipy and Pillow) without installing opencv. Real OpenCV is used
automatically when present and is a few times faster.
"""

from __future__ import annotations

from typing import Any, Sequence, Tuple

import numpy as np

try:  # pragma: no cover - exercised implicitly
    import cv2 as _real_cv2
except ImportError:  # pragma: no cover
    _real_cv2 = None


class _Fallback:
    IMREAD_COLOR = 1
    IMREAD_GRAYSCALE = 0
    COLOR_BGR2GRAY = 6
    THRESH_BINARY = 0
    THRESH_OTSU = 8
    MORPH_OPEN = 2
    INTER_CUBIC = 2
    FONT_HERSHEY_SIMPLEX = 0
    LINE_AA = 16

    # ---------------------------------------------------------------- I/O
    @staticmethod
    def imread(path: str, flags: int = 1) -> Any:
        from PIL import Image

        try:
            with Image.open(path) as im:
                if flags == 0:  # IMREAD_GRAYSCALE
                    return np.asarray(im.convert("L"))
                rgb = np.asarray(im.convert("RGB"))
        except Exception:
            return None
        return np.ascontiguousarray(rgb[:, :, ::-1])

    @staticmethod
    def imwrite(path: str, image: np.ndarray) -> bool:
        from PIL import Image

        if image.ndim == 2:
            Image.fromarray(image).save(path)
        else:
            Image.fromarray(np.ascontiguousarray(image[:, :, ::-1])).save(path)
        return True

    @staticmethod
    def cvtColor(image: np.ndarray, code: int) -> np.ndarray:
        b, g, r = image[:, :, 0], image[:, :, 1], image[:, :, 2]
        return (0.114 * b + 0.587 * g + 0.299 * r).round().astype(np.uint8)

    @staticmethod
    def resize(image: np.ndarray, dsize: Any, fx: float = 1.0, fy: float = 1.0, interpolation: int = 2) -> np.ndarray:
        from PIL import Image

        h, w = image.shape[:2]
        size = (int(round(w * fx)), int(round(h * fy)))
        return np.asarray(Image.fromarray(image).resize(size, Image.BICUBIC))

    # ------------------------------------------------------- binary image ops
    @staticmethod
    def threshold(gray: np.ndarray, thresh: float, maxval: float, kind: int) -> Tuple[float, np.ndarray]:
        hist = np.bincount(gray.ravel(), minlength=256).astype(float)
        total = hist.sum()
        omega = np.cumsum(hist) / total
        mu = np.cumsum(hist * np.arange(256)) / total
        mu_t = mu[-1]
        with np.errstate(divide="ignore", invalid="ignore"):
            sigma_b = (mu_t * omega - mu) ** 2 / (omega * (1 - omega))
        try:
            t = float(np.nanargmax(sigma_b))
        except ValueError:
            t = float(thresh)
        return t, ((gray > t) * maxval).astype(np.uint8)

    @staticmethod
    def morphologyEx(image: np.ndarray, op: int, kernel: np.ndarray) -> np.ndarray:
        from scipy import ndimage

        # Separable min/max filters: much faster than generic binary morphology.
        binary = (image > 0).astype(np.uint8)
        eroded = ndimage.minimum_filter(binary, size=kernel.shape, mode="nearest")
        return ndimage.maximum_filter(eroded, size=kernel.shape, mode="constant", cval=0)

    @staticmethod
    def dilate(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
        from scipy import ndimage

        return ndimage.maximum_filter((image > 0).astype(np.uint8), size=kernel.shape, mode="constant", cval=0)

    @staticmethod
    def connectedComponentsWithStats(image: np.ndarray) -> Tuple[int, np.ndarray, np.ndarray, np.ndarray]:
        from scipy import ndimage

        labels, n = ndimage.label(image > 0, structure=np.ones((3, 3), bool))
        stats = np.zeros((n + 1, 5), np.int64)
        centroids = np.zeros((n + 1, 2), float)
        areas = np.bincount(labels.ravel(), minlength=n + 1)
        for i, sl in enumerate(ndimage.find_objects(labels), start=1):
            if sl is None:
                continue
            ys, xs = sl
            stats[i] = (xs.start, ys.start, xs.stop - xs.start, ys.stop - ys.start, areas[i])
        h, w = image.shape
        stats[0] = (0, 0, w, h, areas[0])
        return n + 1, labels.astype(np.int32), stats, centroids

    @staticmethod
    def floodFill(image: np.ndarray, mask: Any, seed: Sequence[int], value: int) -> Tuple[int, np.ndarray, Any, Any]:
        from scipy import ndimage

        x, y = seed
        region, _ = ndimage.label(image == image[y, x])  # 4-connected
        fill = region == region[y, x]
        image[fill] = value
        return int(fill.sum()), image, mask, None

    # --------------------------------------------------------------- drawing
    @staticmethod
    def _draw(image: np.ndarray, fn: Any) -> None:
        from PIL import Image, ImageDraw

        pil = Image.fromarray(image)
        fn(ImageDraw.Draw(pil))
        image[...] = np.asarray(pil)

    @staticmethod
    def _color(image: np.ndarray, color: Any) -> Any:
        if image.ndim == 2:
            return int(color if np.isscalar(color) else color[0])
        return tuple(int(c) for c in color)

    @classmethod
    def line(cls, image: np.ndarray, p0: Sequence[int], p1: Sequence[int], color: Any, thickness: int = 1, *a: Any) -> np.ndarray:
        c = cls._color(image, color)
        cls._draw(image, lambda d: d.line([tuple(p0), tuple(p1)], fill=c, width=max(1, int(thickness))))
        return image

    @classmethod
    def circle(cls, image: np.ndarray, center: Sequence[int], r: int, color: Any, thickness: int = 1, *a: Any) -> np.ndarray:
        c = cls._color(image, color)
        x, y = center
        box = [x - r, y - r, x + r, y + r]
        cls._draw(image, lambda d: d.ellipse(box, fill=c if thickness < 0 else None, outline=c))
        return image

    @classmethod
    def fillPoly(cls, image: np.ndarray, polys: Sequence[np.ndarray], color: Any) -> np.ndarray:
        c = cls._color(image, color)
        for poly in polys:
            pts = [tuple(int(v) for v in p) for p in np.asarray(poly).reshape(-1, 2)]
            cls._draw(image, lambda d: d.polygon(pts, fill=c))
        return image

    @classmethod
    def polylines(cls, image: np.ndarray, polys: Sequence[np.ndarray], closed: bool, color: Any, thickness: int = 1) -> np.ndarray:
        c = cls._color(image, color)
        for poly in polys:
            pts = [tuple(int(v) for v in p) for p in np.asarray(poly).reshape(-1, 2)]
            if closed:
                pts.append(pts[0])
            cls._draw(image, lambda d: d.line(pts, fill=c, width=max(1, int(thickness))))
        return image

    @classmethod
    def putText(cls, image: np.ndarray, text: str, org: Sequence[int], font: int, scale: float, color: Any, thickness: int = 1, *a: Any) -> np.ndarray:
        c = cls._color(image, color)
        x, y = org
        cls._draw(image, lambda d: d.text((x, y - 10), text, fill=c))
        return image


cv2 = _real_cv2 if _real_cv2 is not None else _Fallback()
HAVE_OPENCV = _real_cv2 is not None

"""Find the face in a live-view frame and read its exposure and colour.

Exposure is the median brightness of the middle of the face (cheeks, nose,
forehead), so hair, eyes and background do not pull it around; on a face lit
from one side it is the lit half that counts. Colour is read
the way a colourist reads a vectorscope: every skin tone, light or dark, sits
near one line on the scope (about 123 degrees), so a face pushed off that line
shows a colour cast. Here, positive means toward green.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import io
import math

import numpy as np
from PIL import Image

SKIN_LINE_DEGREES = 123.0
MIN_CHROMA = 0.02  # below this the face is too grey (log footage, very dim) to read a cast from
CLIP_LEVEL = 0.97

Box = tuple[float, float, float, float]  # x, y, width, height as fractions of the frame, top left origin


def _rec709_to_linear(v: np.ndarray) -> np.ndarray:
    return np.where(v < 0.081, v / 4.5, ((v + 0.099) / 1.099) ** (1 / 0.45))


def _linear_to_rec709(x: np.ndarray) -> np.ndarray:
    return np.where(x < 0.018, x * 4.5, 1.099 * np.power(np.maximum(x, 0.018), 0.45) - 0.099)


# Sony's S-Log3 curve, on 10-bit code values scaled to 0..1.
def _slog3_to_linear(v: np.ndarray) -> np.ndarray:
    code = v * 1023
    return np.where(
        code >= 171.2102946929,
        10 ** ((code - 420) / 261.5) * 0.19 - 0.01,
        (code - 95) * 0.01125 / (171.2102946929 - 95),
    )


def _linear_to_slog3(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    code = np.where(
        x >= 0.01125,
        420 + np.log10(np.maximum(x + 0.01, 1e-9) / 0.19) * 261.5,
        x * (171.2102946929 - 95) / 0.01125 + 95,
    )
    return code / 1023


PROFILES = {
    "rec709": (_rec709_to_linear, _linear_to_rec709),
    "slog3": (_slog3_to_linear, _linear_to_slog3),
}


def to_linear(level: float, profile: str) -> float:
    return float(PROFILES[profile][0](np.asarray(level, dtype=float)))


def from_linear(light: float, profile: str) -> float:
    return float(PROFILES[profile][1](np.asarray(light, dtype=float)))


def stops_off(level: float, target: float, profile: str) -> float:
    """Stops of light to add (positive) or take away to bring level to target."""

    return math.log2(max(to_linear(target, profile), 1e-6) / max(to_linear(level, profile), 1e-6))


@dataclass
class Reading:
    face: bool  # False: no face found, the middle of the frame was read instead
    box: Box
    level: float  # median luma of the middle of the face, 0..1 as live view encodes it
    clipped: float  # share of the face at or near white
    hue: float | None  # skin hue on the vectorscope in degrees, None when too grey to read
    chroma: float
    sides: tuple[float, float] = (0.0, 0.0)  # median luma of the left and right halves

    @property
    def lit(self) -> float:
        """What exposure is judged on: the lit side of a face, as with any side-lit
        portrait, so the shadow side is free to fall where the fill puts it."""

        return max(self.sides) if self.face else self.level

    def contrast(self, profile: str) -> float:
        """Stops between the lit and the shadow side of the face: the lighting ratio."""

        bright, dark = max(self.sides), min(self.sides)
        return stops_off(dark, bright, profile)

    def hue_off(self, reference: float = SKIN_LINE_DEGREES) -> float | None:
        """Degrees off the reference skin hue; positive is toward green."""

        if self.hue is None:
            return None
        return (self.hue - reference + 180) % 360 - 180

    def summary(self) -> dict:
        out = asdict(self)
        out["box"] = [round(v, 3) for v in self.box]
        out["sides"] = [round(v, 3) for v in self.sides]
        for key in ("level", "clipped", "chroma"):
            out[key] = round(out[key], 3)
        if self.hue is not None:
            out["hue"] = round(self.hue, 1)
        return out


def find_face(jpeg: bytes) -> Box | None:
    """The largest face Apple Vision finds, or None."""

    import Vision
    from Foundation import NSData, NSDictionary

    data = NSData.dataWithBytes_length_(jpeg, len(jpeg))
    # A plain {} does not bridge here; Vision wants a real NSDictionary.
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, NSDictionary.dictionary())
    request = Vision.VNDetectFaceRectanglesRequest.alloc().init()
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise ValueError(f"live view frame is not a readable image: {error}")
    faces = list(request.results() or [])
    if not faces:
        return None
    box = max(faces, key=lambda f: f.boundingBox().size.width * f.boundingBox().size.height).boundingBox()
    # Vision measures from the bottom left.
    x, y, w, h = box.origin.x, box.origin.y, box.size.width, box.size.height
    return (x, 1 - y - h, w, h)


def _inner(box: Box) -> Box:
    """The middle of a face box: drops hair, ears, chin and background."""

    x, y, w, h = box
    return (x + w * 0.2, y + h * 0.15, w * 0.6, h * 0.7)


def read(jpeg: bytes, box: Box | None = None) -> Reading:
    """Meter a frame. Without a box, finds the face; with no face, reads the middle third."""

    face = box is not None
    if box is None:
        box = find_face(jpeg)
        face = box is not None
    if box is None:
        box = (1 / 3, 1 / 3, 1 / 3, 1 / 3)
    rgb = np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB"), dtype=float) / 255
    height, width, _ = rgb.shape
    x, y, w, h = _inner(box) if face else box
    left, top = int(x * width), int(y * height)
    right, bottom = max(left + 1, int((x + w) * width)), max(top + 1, int((y + h) * height))
    region = rgb[top:bottom, left:right]
    middle = max(1, region.shape[1] // 2)
    halves = [region[:, :middle], region[:, middle:] if region.shape[1] > 1 else region]
    sides = tuple(float(np.median(_luma(half.reshape(-1, 3)))) for half in halves)
    patch = region.reshape(-1, 3)

    r, g, b = patch[:, 0], patch[:, 1], patch[:, 2]
    luma = _luma(patch)
    clipped = float(np.mean(patch.max(axis=1) >= CLIP_LEVEL))
    # Colour from the skin itself: not the shadows (eyes, brows) and not clipped shine.
    skin = (luma > np.percentile(luma, 20)) & (patch.max(axis=1) < CLIP_LEVEL)
    if not skin.any():
        skin = np.ones_like(luma, dtype=bool)
    cb = float(np.mean((b[skin] - luma[skin]) * 0.564))
    cr = float(np.mean((r[skin] - luma[skin]) * 0.713))
    chroma = math.hypot(cb, cr)
    hue = math.degrees(math.atan2(cr, cb)) % 360 if chroma >= MIN_CHROMA else None
    return Reading(face, box, float(np.median(luma)), clipped, hue, chroma, sides)


def _luma(pixels: np.ndarray) -> np.ndarray:
    return 0.299 * pixels[:, 0] + 0.587 * pixels[:, 1] + 0.114 * pixels[:, 2]

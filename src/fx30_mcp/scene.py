"""One reading per frame: the face, its light and colour, and the window, from one decode."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import meter
from .meter import Box


@dataclass(frozen=True)
class Scene:
    face: bool  # False: no face found, the middle of the frame was read instead
    box: Box | None
    yaw: float | None
    level: float  # Reading.lit
    sides: tuple[float, float]
    cb: float
    cr: float
    hue: float | None
    chroma: float
    clipped: float
    window: meter.Window | None
    camera: dict[str, Any]  # the settings() dict the scene was read with, including meter and recording
    frame: bytes


def read_scene(frame: bytes, camera: dict[str, Any], profile: str, box: Box | None = None) -> Scene:
    """Decode once, ask Vision once (not at all when box is given), then meter the pixels."""

    pixels = meter.decode(frame)
    yaw = None
    if box is None:
        found = meter.detect(frame)
        if found is not None:
            box, yaw = found
    reading = meter.read(pixels, box)
    window = meter.find_window(pixels, box, profile)
    return Scene(
        face=reading.face,
        box=box,
        yaw=yaw,
        level=reading.lit,
        sides=reading.sides,
        cb=reading.cb,
        cr=reading.cr,
        hue=reading.hue,
        chroma=reading.chroma,
        clipped=reading.clipped,
        window=window,
        camera=camera,
        frame=frame,
    )

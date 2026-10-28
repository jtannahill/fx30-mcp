"""Look at the room with the lights off, and check which side the key lights.

With the key and fill off, a frame shows what the room does to the face on its
own: the window, the ambient light on each cheek, the way the face is turned.
Both lights go back to how they were, even when the camera fails.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from . import meter
from .camera import CameraBase

Sleep = Callable[[float], Awaitable[None]]
LIT_SHARE = 0.1  # the key must add at least this share of the off level to count


@dataclass
class Survey:
    reading: meter.Reading
    yaw: float | None
    ambient: tuple[float, float]
    window: meter.Window | None
    camera: dict[str, Any]
    info: dict[str, Any]
    frame: bytes


@dataclass
class KeyCheck:
    sides: tuple[float, float]
    lit: str | None  # "left" or "right" of the frame, None when the key adds next to nothing
    on: bytes = b""  # the frames the check compared, kept for the catchlight check
    off: bytes = b""


async def _was_on(light: Any) -> bool:
    if light.state.power is None:
        await light.read_status()
    return light.state.power is not False


async def survey(
    camera: CameraBase,
    key: Any,
    fill: Any | None,
    profile: str,
    settle: float = 0.7,
    sleep: Sleep = asyncio.sleep,
) -> Survey:
    """Turn the key and fill off, read one frame, and put them back."""

    lights = [light for light in (key, fill) if light is not None]
    previous = [(light, await _was_on(light)) for light in lights]
    try:
        for light in lights:
            await light.set_power(False)
        await sleep(settle)
        frame = await camera.frame()
        settings = await camera.settings()
        info = await camera.describe()
    finally:
        for light, was_on in previous:
            await light.set_power(was_on)
    reading = await asyncio.to_thread(meter.read, frame)
    yaw = await asyncio.to_thread(meter.face_yaw, frame)
    window = await asyncio.to_thread(meter.find_window, frame, reading.box, profile)
    return Survey(reading, yaw, reading.sides, window, settings, info, frame)


async def key_check(
    camera: CameraBase,
    key: Any,
    box: meter.Box,
    profile: str,
    settle: float = 0.7,
    sleep: Sleep = asyncio.sleep,
) -> KeyCheck:
    """Compare a frame with the key off to one with it on; leaves the key on."""

    try:
        await key.set_power(False)
        await sleep(settle)
        off = await camera.frame()
    finally:
        await key.set_power(True)
    await sleep(settle)
    on = await camera.frame()
    sides = await asyncio.to_thread(meter.key_only, on, off, box, profile)
    dark = await asyncio.to_thread(meter.read, off, box)
    floor = LIT_SHARE * max(meter.to_linear(level, profile) for level in dark.sides)
    if max(sides) <= floor or max(sides) <= 0:
        return KeyCheck(sides, None, on, off)
    return KeyCheck(sides, "left" if sides[0] > sides[1] else "right", on, off)

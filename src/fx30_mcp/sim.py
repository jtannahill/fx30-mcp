"""A pretend camera and light for tests and for trying the tools without the gear.

The camera renders a flat skin-coloured face lit by the pretend light: brighter
with brightness (along a curve, as real dimmers are), greener or more magenta
with tint and with a green cast from the "room". With key_spill below 1 the key
lights the left half of the face more than the right, which a fill light lifts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import io
from typing import Any

import numpy as np
from PIL import Image

from .camera import CameraBase
from .meter import PROFILES

SKIN = np.array([0.75, 0.41, 0.26])  # linear RGB of a light skin tone under even white light


@dataclass
class SimState:
    power: bool = True
    brightness: float | None = 30.0
    kelvin: int | None = 5600
    tint: int | None = 0


@dataclass
class SimCaps:
    tint: bool = True
    kelvin_min: int = 2700
    kelvin_max: int = 7500


@dataclass
class SimLight:
    name: str = "pretend light"
    backend: str = "sim"
    state: SimState = field(default_factory=SimState)
    caps: SimCaps = field(default_factory=SimCaps)
    writes: list[tuple[int, float, int]] = field(default_factory=list)

    async def read_status(self) -> dict[str, Any]:
        return {"light": self.name, **self.state.__dict__}

    async def set_white(self, kelvin: int, brightness: float | None = None, tint: int = 0) -> None:
        self.state.kelvin = kelvin
        if brightness is not None:
            self.state.brightness = brightness
        self.state.tint = tint
        self.writes.append((kelvin, self.state.brightness, tint))

    async def set_brightness(self, percent: float) -> None:
        self.state.brightness = percent

    async def close(self) -> None:
        pass


class SimCamera(CameraBase):
    backend = "sim"

    def __init__(
        self,
        light: SimLight,
        profile: str = "rec709",
        gain: float = 1.0,
        curve: float = 1.6,  # light output goes as brightness ** curve
        green_cast: float = 0.0,
        tint_sign: int = 1,  # +1: a higher tint is greener
        fill: SimLight | None = None,
        fill_gain: float = 1.0,
        key_spill: float = 1.0,  # share of the key reaching the right half of the face
    ) -> None:
        self.name = "pretend camera"
        self.light = light
        self.profile = profile
        self.gain = gain
        self.curve = curve
        self.green_cast = green_cast
        self.tint_sign = tint_sign
        self.fill = fill
        self.fill_gain = fill_gain
        self.key_spill = key_spill
        self.kelvin: int | None = None
        self.recording = False
        self.values: dict[str, Any] = {"iso": "800", "shutter": "1/50", "aperture": "f/2.8"}

    def _output(self, light: SimLight | None, gain: float) -> float:
        if light is None or not light.state.power:
            return 0.0
        return gain * ((light.state.brightness or 0) / 100) ** self.curve

    def render(self) -> bytes:
        key = self._output(self.light, self.gain)
        fill = self._output(self.fill, self.fill_gain)
        green = 1 + self.green_cast + 0.02 * (self.light.state.tint or 0) * self.tint_sign
        tone = SKIN * np.array([1.0, green, 1.0])
        encode = PROFILES[self.profile][1]
        frame = np.full((180, 320, 3), float(encode(np.array(0.02 * key))))
        frame[40:140, 100:160] = encode(np.clip(tone * (key + 0.1 * fill), 0, 1.2))
        frame[40:140, 160:220] = encode(np.clip(tone * (key * self.key_spill + fill), 0, 1.2))
        pixels = (np.clip(frame, 0, 1) * 255).round().astype(np.uint8)
        out = io.BytesIO()
        Image.fromarray(pixels).save(out, "JPEG", quality=95)
        return out.getvalue()

    async def settings(self) -> dict[str, Any]:
        return {**self.values, "kelvin": self.kelvin, "recording": self.recording}

    async def set(self, setting: str, value: str) -> str:
        self.values[setting] = value
        return value

    async def set_kelvin(self, kelvin: int) -> None:
        self.kelvin = kelvin

    async def frame(self) -> bytes:
        return self.render()

    async def record(self, on: bool) -> None:
        self.recording = on

    async def close(self) -> None:
        pass

"""A pretend camera and light for tests and for trying the tools without the gear.

The camera renders a flat skin-coloured face lit by the pretend light: brighter
with brightness (along a curve, as real dimmers are), greener or more magenta
with tint and with a green cast from the "room". With key_spill below 1 the key
lights the left half of the face more than the right, which a fill light lifts.
A window, room ambient light, the key's side and distance, and the camera's own
exposure (ISO, shutter, aperture) shape the rest of the scene.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import io
import math
from typing import Any

import numpy as np
from PIL import Image

from .camera import CameraBase, CameraError, read_only
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

    async def set_power(self, on: bool) -> None:
        self.state.power = on

    async def set_brightness(self, percent: float) -> None:
        self.state.brightness = percent

    async def close(self) -> None:
        pass


APERTURES = ["f/2.8", "f/3.2", "f/3.5", "f/4", "f/4.5", "f/5", "f/5.6", "f/6.3", "f/7.1", "f/8", "f/9",
             "f/10", "f/11", "f/13", "f/14", "f/16", "f/18", "f/20", "f/22"]
EXPOSURE_MODES = ["Intelligent Auto", "P", "A", "S", "M", "Movie F Mode", "S&Q F Mode"]
SHUTTERS = ["1/60", "1/80", "1/100", "1/125", "1/160", "1/200", "1/250", "1/320", "1/400", "1/500"]


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
        key_spill: float = 1.0,  # share of the key reaching the half of the face away from the key
        window: float = 0.0,  # linear level of a window at the right of the frame, 0 for none
        ambient: float = 0.0,  # room light on the face, mostly on the rim_side half
        rim_side: str = "right",
        key_side: str = "left",
        key_distance_ft: float = 3.0,
        yaw: float = 0.0,  # which way the face is turned, degrees, read by tests
        iso: str = "800",
        shutter: str = "1/60",
        aperture: str = "f/2.8",
        exposure_mode: str = "Movie F Mode",  # as the FX30 came: shutter and ISO stay on its own controls
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
        self.window = window
        self.ambient = ambient
        self.rim_side = rim_side
        self.key_side = key_side
        self.key_distance_ft = key_distance_ft
        self.yaw = yaw
        self.kelvin: int | None = None
        self.recording = False
        self.values: dict[str, Any] = {"iso": iso, "shutter": shutter, "aperture": aperture}
        self.exposure_mode = exposure_mode

    def _output(self, light: SimLight | None, gain: float) -> float:
        if light is None or not light.state.power:
            return 0.0
        return gain * ((light.state.brightness or 0) / 100) ** self.curve

    def exposure_stops(self) -> float:
        """Stops of exposure relative to ISO 800, 1/60, f/2.8."""

        iso = float(self.values["iso"])
        shutter = self.values["shutter"]
        seconds = float(shutter.split("/")[1]) ** -1 if "/" in shutter else float(shutter.strip('"'))
        f_number = float(self.values["aperture"].lstrip("fF/"))
        return math.log2(iso / 800) + math.log2(seconds * 60) - 2 * math.log2(f_number / 2.8)

    def render(self) -> bytes:
        scale = 2 ** self.exposure_stops()
        key = self._output(self.light, self.gain) * (3.0 / self.key_distance_ft) ** 2
        # A key warmer than the camera's white balance reads warm; cooler reads cool.
        shift = 0.0
        if self.kelvin and self.light.state.kelvin:
            shift = (self.kelvin - self.light.state.kelvin) / 1000
        key_tint = np.array([1 + 0.08 * shift, 1.0, 1 - 0.08 * shift])
        fill = self._output(self.fill, self.fill_gain)
        green = 1 + self.green_cast + 0.02 * (self.light.state.tint or 0) * self.tint_sign
        tone = SKIN * np.array([1.0, green, 1.0])
        encode = PROFILES[self.profile][1]
        rim = {"left": (1.0, 0.3), "right": (0.3, 1.0)}[self.rim_side]
        lit_left = self.key_side == "left"
        key_halves = (key, key * self.key_spill) if lit_left else (key * self.key_spill, key)
        fill_halves = (0.1 * fill, fill) if lit_left else (fill, 0.1 * fill)
        frame = np.full((180, 320, 3), float(encode(np.array(0.02 * key * scale))))
        if self.window:
            frame[0:140, 250:320] = encode(np.array(self.window * scale))
        for x, half in ((100, 0), (160, 1)):
            other = (fill_halves[half] + self.ambient * rim[half]) * scale
            level = tone * other + tone * key_tint * key_halves[half] * scale
            frame[40:140, x : x + 60] = encode(np.clip(level, 0, 1.2))
        pixels = (np.clip(frame, 0, 1) * 255).round().astype(np.uint8)
        out = io.BytesIO()
        Image.fromarray(pixels).save(out, "JPEG", quality=95)
        return out.getvalue()

    async def settings(self) -> dict[str, Any]:
        level = max(self._output(self.light, self.gain) + self._output(self.fill, self.fill_gain) + self.ambient, 1e-6)
        return {
            **self.values, "kelvin": self.kelvin, "exposure_mode": self.exposure_mode, "recording": self.recording,
            "meter": self.exposure_stops() + math.log2(level), "battery": 100, "overheat": False,
            "cards_ok": True, "live_view_effect": True,
        }

    def hold(self, on: bool) -> None:
        pass

    async def describe(self) -> dict[str, Any]:
        return {
            "frame_rate": 29.97,
            "file_format": "XAVC S 4K",
            "recording_setting": "52",
            "picture_profile": "PP8",
            "gamma": "S-Log3",
            "display_assist": False,
            "apertures": list(APERTURES),
            "shutters": list(SHUTTERS),
        }

    async def set(self, setting: str, value: str) -> str:
        if setting in ("shutter", "iso") and self.exposure_mode != "M":
            raise read_only(setting, self.values[setting])
        self.values[setting] = value
        return value

    async def set_exposure_mode(self, mode: str) -> None:
        if mode not in EXPOSURE_MODES:
            raise CameraError(f"exposure_mode {mode!r} is not offered; choices: {', '.join(EXPOSURE_MODES)}")
        self.exposure_mode = mode

    async def set_kelvin(self, kelvin: int) -> None:
        self.kelvin = kelvin

    async def frame(self) -> bytes:
        return self.render()

    async def record(self, on: bool) -> None:
        self.recording = on

    async def close(self) -> None:
        pass

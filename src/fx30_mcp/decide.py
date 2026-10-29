"""Decide what to do next: all judgment, no hardware.

decide() looks at one Scene, the rig as last written, the goal and the history, and returns
Moves: at most one light move, one camera move and one thing to ask of the person. It is pure
apart from updating history.gain and history.last_stops. Setup mode brings the shot to Ready;
the Rig applies the moves and the Director owns the history.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from . import meter
from .plan import (
    MIN_KEY_FT,
    UNEVEN_STOPS,
    Geometry,
    camera_card,
    closer,
    exposure_change,
    fmt_ft,
)
from .scene import Scene
from .survey import KeyCheck

STOP_TOLERANCE = 1 / 6
CLIP_LIMIT = 0.03  # more of the face than this at white is shine to take down
MIN_BRIGHTNESS = 1.0
HUE_DEADBAND = 5.0  # degrees; skin tones spread a few degrees either side of the line
DEGREES_PER_TINT = 3.0  # a first guess at how far one tint step moves skin on the scope
MAX_TINT_STEP = 3
CARD_SETTINGS = ("aperture", "shutter", "iso")  # the order the card is applied in

CANT_HOLD = "A sheer curtain or ND film on the glass would. Or open up and let the window sit brighter."
DARKEST = ("The camera can't darken this window any further", "ND film or a sheer curtain on the glass would.")
NO_FACE = ("Sit in the shot", "The coach needs your face in the frame to plan.")
CONNECTING = ("Connecting to the light", "This can take up to two minutes.")
UNREACHABLE = (
    "The light isn't answering",
    "If the amaran or Sidus Link app is open on your phone, close it or turn off the phone's Bluetooth, "
    "then start the coach again.",
)


@dataclass(frozen=True)
class RigState:
    power: bool | None
    brightness: float | None
    kelvin: int | None
    tint: int
    camera: dict[str, Any]  # iso, shutter, aperture, kelvin, exposure_mode as last read


@dataclass(frozen=True)
class Goal:
    face_level: float  # Settings.target
    skin_hue: float  # Settings.skin_hue
    window_range: list[float]
    profile: str  # "rec709" or "slog3"
    tint_sign: int
    kelvin: int  # Plan.kelvin, the camera's white balance and the key's base
    frame_rate: float
    widest: float
    geometry: Geometry  # where the key should be, for instructions


@dataclass(frozen=True)
class LightMove:
    brightness: float
    kelvin: int
    tint: int


@dataclass(frozen=True)
class CameraMove:
    setting: str  # "exposure_mode", "aperture", "iso", "shutter", "kelvin", "live_view_effect"
    value: str


@dataclass(frozen=True)
class Ask:
    instruction: str
    detail: str | None


@dataclass(frozen=True)
class Moves:
    light: LightMove | None = None
    camera: CameraMove | None = None
    ask: Ask | None = None
    held: str | None = None  # why something that wanted to change did not (for the report)


@dataclass
class History:  # owned by the Director, read by decide
    key_check: KeyCheck | None = None
    catchlight: str | None = None  # "raise", "lower", "ok" or None
    gain: float = 1.0  # halves on overshoot, as balance did
    last_stops: float | None = None
    snapshot: tuple[float, float, float] | None = None  # take: (level, cb, cr) at record start
    snapshot_meter: float | None = None  # take: the camera's meter at record start
    commanded: dict[str, str] = field(default_factory=dict)  # camera values the director last wrote
    hands_off: bool = False  # owner changed the camera by hand this take
    last_camera_step: float = -math.inf
    last_tint_step: float = -math.inf
    last_tick: float | None = None


def _number(setting: str, text: Any) -> float | None:
    """A camera value as a number, whatever form it is written in: "f/2.8" or "2.8", "1/60" or '1/60"'."""

    text = str(text).strip().strip('"')
    try:
        if setting == "aperture":
            return float(text.lstrip("fF/"))
        if setting == "shutter":
            if "/" in text:
                top, bottom = text.split("/")
                return float(top) / float(bottom)
            return float(text)
        return float(text.split()[-1])
    except (ValueError, IndexError):
        return None


def canonical(setting: str, value: Any) -> str | None:
    """One form for a camera value: "800", "1/60", "f/6.3". None when the camera did not report it;
    text that is not a number comes back as it is, so it never matches a card."""

    if value is None:
        return None
    number = _number(setting, value)
    if number is None:
        return str(value)
    if setting == "aperture":
        return f"f/{number:g}"
    if setting == "shutter" and 0 < number < 1 and math.isclose(1 / number, round(1 / number), rel_tol=0.01):
        return f"1/{round(1 / number)}"
    return f"{number:g}"


def decide(scene: Scene, rig: RigState, goal: Goal, mode: str, history: History, now: float) -> Moves:
    if mode == "setup":
        return _setup(scene, rig, goal, history)
    raise NotImplementedError(f"mode {mode!r}")


def _camera_move(scene: Scene, rig: RigState, goal: Goal) -> CameraMove | None:
    camera = rig.camera
    if camera.get("live_view_effect") is False:
        return CameraMove("live_view_effect", "On")
    if camera.get("exposure_mode") not in (None, "M"):
        return CameraMove("exposure_mode", "M")
    if camera.get("kelvin") is not None and canonical("kelvin", camera["kelvin"]) != str(goal.kelvin):
        return CameraMove("kelvin", str(goal.kelvin))
    return None


def _card_move(scene: Scene, rig: RigState, goal: Goal) -> CameraMove | None:
    stops = exposure_change(scene.window, goal.face_level, goal.profile, goal.window_range)
    card = camera_card(rig.camera, stops, goal.frame_rate, goal.widest, scene.window is not None)
    for setting in CARD_SETTINGS:
        current = canonical(setting, rig.camera.get(setting))
        wanted = canonical(setting, getattr(card, setting))
        if current is not None and current != wanted:
            return CameraMove(setting, getattr(card, setting))
    return None


def _key_ask(goal: Goal, history: History) -> Ask | None:
    geometry = goal.geometry
    check = history.key_check
    if check is None:
        return Ask(
            f"Key on camera-{geometry.side}",
            f"{fmt_ft(geometry.distance_ft)} from your face, {fmt_ft(geometry.height_ft)} above your eyes, "
            "aimed down.",
        )
    near, far = check.sides if geometry.side == "left" else reversed(check.sides)
    if check.lit is None:
        return Ask("Turn the key on", "The key isn't reaching your face.")
    # A soft key lights both cheeks nearly alike; only a clearly brighter far side is wrong.
    if far > near * 2**UNEVEN_STOPS:
        return Ask("Key is on the wrong side", f"Move it to camera-{geometry.side}.")
    if history.catchlight == "raise":
        return Ask("Raise the key", "The catchlight is low in your eyes.")
    if history.catchlight == "lower":
        return Ask("Lower the key a little", "There's no catchlight in your eyes.")
    return None


def _tint_step(scene: Scene, rig: RigState, goal: Goal) -> int:
    """The tint to move to: skin hue against the skin line, as balance did."""

    if not scene.face or scene.hue is None:
        return rig.tint
    hue_off = (scene.hue - goal.skin_hue + 180) % 360 - 180  # positive is toward green
    if abs(hue_off) <= HUE_DEADBAND:
        return rig.tint
    size = min(MAX_TINT_STEP, max(1, round(abs(hue_off) / DEGREES_PER_TINT)))
    # Too green means less green from the light.
    return max(-10, min(10, rig.tint - size * goal.tint_sign * (1 if hue_off > 0 else -1)))


def _setup(scene: Scene, rig: RigState, goal: Goal, history: History) -> Moves:
    move = _camera_move(scene, rig, goal)
    if move is not None:
        return Moves(camera=move)
    if not scene.face:
        return Moves(ask=Ask(*NO_FACE))
    move = _card_move(scene, rig, goal)
    if move is not None:
        return Moves(camera=move)
    ask = _key_ask(goal, history)
    if ask is not None:
        return Moves(ask=ask)

    stops = meter.stops_off(scene.level, goal.face_level, goal.profile)
    if scene.clipped > CLIP_LIMIT:
        stops = min(stops, -1 / 3)
    if history.last_stops is not None and abs(stops) > STOP_TOLERANCE and history.last_stops * stops < 0:
        history.gain *= 0.5  # overshot: the light's dimming curve is not linear here
    history.last_stops = stops

    brightness = float(rig.brightness if rig.brightness is not None else 50.0)
    new_brightness = brightness
    ask = None
    if abs(stops) > STOP_TOLERANCE:
        new_brightness = min(100.0, max(MIN_BRIGHTNESS, brightness * 2 ** (stops * history.gain)))
        if round(new_brightness, 1) == round(brightness, 1):
            distance = goal.geometry.distance_ft
            if stops > 0:
                if closer(distance, stops) < MIN_KEY_FT:
                    ask = Ask("The key can't hold this window right now", CANT_HOLD)
                else:
                    ask = Ask("Key closer", f"{fmt_ft(distance)} to {fmt_ft(closer(distance, stops))}")
            else:
                ask = Ask("Key farther", f"{fmt_ft(distance)} to {fmt_ft(closer(distance, stops))}")
    tint = _tint_step(scene, rig, goal)
    new_brightness = round(new_brightness, 1)
    light = None
    if (new_brightness, tint) != (round(brightness, 1), rig.tint):
        light = LightMove(new_brightness, goal.kelvin, tint)
    return Moves(light=light, ask=ask)

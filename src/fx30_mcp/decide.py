"""Decide what to do next: all judgment, no hardware.

decide() looks at one Scene, the rig as last written, the goal and the history, and returns
Moves: at most one light move, one camera move and one thing to ask of the person. It is pure
apart from updating history.gain and history.last_stops in setup, and history.hands_off and
history.last_tint_step in a take. Setup mode brings the shot to Ready. Balance mode is setup for the
light alone: no camera move and no key placement, for balance_shot. Take mode holds what recording
started with (start_take), slowly enough that a viewer never sees a change, and never asks.
The Rig applies the moves and the Director owns the history.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from . import meter
from .plan import (
    APERTURES,
    MIN_KEY_FT,
    SHUTTERS,
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

# Take mode. Pacing first guesses, kept as names so they can be tuned on the owner's footage.
CAMERA_STEP_SECONDS = 8.0
BRIGHTNESS_STOPS_PER_SECOND = 1 / 6
KELVIN_PER_SECOND = 100
TINT_STEP_SECONDS = 5.0
TAKE_TICK_SECONDS = 1.0  # the take interval, used when there was no previous tick
# A late tick moves no further than one tick's worth, so no single light change is big enough to see.
MAX_TICK_SECONDS = 1.0
CONFIRM_SECONDS = 3.0  # a camera value off what the director wrote this soon after it wrote is lag, not a hand
STEP = 1 / 3  # stops in one camera step
# Stops past STEP a step may span: nominal values are rounded (f/11 to f/13 reads 0.48 stop, is 1/3).
# Anything wider (f/1.8 to f/2.8, ISO 6400 to 3200, 1/30 to 1/50) is off the ladder.
STEP_SLACK = 0.2
METER_STANDS_IN = 1 / 3  # stops the camera's meter must move before it speaks for a window it cannot see
ISO_STEPS = [100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600, 2000, 2500, 3200]
TAKE_DARKEST_APERTURE = 22.0
KEY_KELVIN_RANGE = (2700, 6500)
# Colour axes in (cb, cr): the way skin moves when the light on it warms, and when it greens.
WARM = (-0.6, 0.8)
GREEN = (-0.707, -0.707)
KELVIN_PER_WARM = 4000.0  # key kelvin that undoes one unit along WARM (first guess)
TINT_PER_GREEN = 60.0  # tint steps that undo one unit along GREEN (first guess)
KELVIN_DEADBAND = 50  # K; smaller corrections are noise in the face's colour
EXPOSURE_SETTINGS = ("aperture", "iso", "shutter")  # the order a take steps them in, least visible first

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
    fix_tint: bool = True  # False: setup and balance leave the key's tint as it is (balance_shot(fix_tint=False))


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
    if mode in ("setup", "balance"):
        return _setup(scene, rig, goal, history, light_only=mode == "balance")
    if mode == "take":
        return _take(scene, rig, goal, history, now)
    raise ValueError(f"unknown mode {mode!r}")


def start_take(scene: Scene, history: History) -> None:
    """Recording has started: hold this face and these camera settings through the take."""

    history.snapshot = (scene.level, scene.cb, scene.cr)
    history.snapshot_meter = scene.camera.get("meter")
    history.hands_off = False
    # A take reads the gain but never learns it: capped at 1/6 stop a second it can hardly overshoot,
    # so a sign flip there is the subject moving. Each take, and the setup after it, starts fresh.
    history.gain = 1.0
    history.last_stops = None
    for setting in EXPOSURE_SETTINGS:
        value = canonical(setting, scene.camera.get(setting))
        if value is None:
            history.commanded.pop(setting, None)
        else:
            history.commanded[setting] = value


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
    # The camera reads back in several forms ("ISO 800", '1/60"', "6.3"); the card wants one.
    readback = {s: canonical(s, rig.camera[s]) for s in CARD_SETTINGS if rig.camera.get(s) is not None}
    card = camera_card({**rig.camera, **readback}, stops, goal.frame_rate, goal.widest, scene.window is not None)
    for setting in CARD_SETTINGS:
        current = canonical(setting, rig.camera.get(setting))
        wanted = canonical(setting, getattr(card, setting))
        if current is not None and current != wanted:
            return CameraMove(setting, getattr(card, setting))
    return None


def key_placed(check: KeyCheck, side: str) -> bool:
    """The key reaches the face and lights it from the planned side."""

    if check.lit is None:
        return False
    near, far = check.sides if side == "left" else reversed(check.sides)
    # A soft key lights both cheeks nearly alike; only a clearly brighter far side is wrong.
    return far <= near * 2**UNEVEN_STOPS


def _key_ask(goal: Goal, history: History) -> Ask | None:
    geometry = goal.geometry
    check = history.key_check
    if check is None:
        return Ask(
            f"Key on camera-{geometry.side}",
            f"{fmt_ft(geometry.distance_ft)} from your face, {fmt_ft(geometry.height_ft)} above your eyes, "
            "aimed down.",
        )
    if check.lit is None:
        return Ask("Turn the key on", "The key isn't reaching your face.")
    if not key_placed(check, geometry.side):
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


def _learn_gain(stops: float, history: History) -> None:
    if history.last_stops is not None and abs(stops) > STOP_TOLERANCE and history.last_stops * stops < 0:
        history.gain *= 0.5  # overshot: the light's dimming curve is not linear here
    history.last_stops = stops


def _setup(scene: Scene, rig: RigState, goal: Goal, history: History, light_only: bool = False) -> Moves:
    """Light only (balance mode) leaves the camera and the key's placement as they are."""

    move = None if light_only else _camera_move(scene, rig, goal)
    if move is not None:
        return Moves(camera=move)
    if not scene.face:
        return Moves(ask=Ask(*NO_FACE))
    move = None if light_only else _card_move(scene, rig, goal)
    if move is not None:
        return Moves(camera=move)
    ask = None if light_only else _key_ask(goal, history)
    if ask is not None:
        return Moves(ask=ask)

    stops = meter.stops_off(scene.level, goal.face_level, goal.profile)
    if scene.clipped > CLIP_LIMIT:
        stops = min(stops, -1 / 3)
    _learn_gain(stops, history)

    brightness = float(rig.brightness if rig.brightness is not None else 50.0)
    new_brightness = brightness
    ask = None
    if abs(stops) > STOP_TOLERANCE:
        new_brightness = min(100.0, max(MIN_BRIGHTNESS, brightness * 2 ** (stops * history.gain)))
        if round(new_brightness, 1) == round(brightness, 1):
            # A small gain can round the step away; the key still moves its smallest step toward the face.
            new_brightness = min(100.0, max(MIN_BRIGHTNESS, brightness + (0.1 if stops > 0 else -0.1)))
        if (stops > 0 and brightness >= 100.0) or (stops < 0 and brightness <= MIN_BRIGHTNESS):
            distance = goal.geometry.distance_ft
            if stops > 0:
                if closer(distance, stops) < MIN_KEY_FT:
                    ask = Ask("The key can't hold this window right now", CANT_HOLD)
                else:
                    ask = Ask("Key closer", f"{fmt_ft(distance)} to {fmt_ft(closer(distance, stops))}")
            else:
                ask = Ask("Key farther", f"{fmt_ft(distance)} to {fmt_ft(closer(distance, stops))}")
    tint = _tint_step(scene, rig, goal) if goal.fix_tint else rig.tint
    new_brightness = round(new_brightness, 1)
    light = None
    if (new_brightness, tint) != (round(brightness, 1), rig.tint):
        light = LightMove(new_brightness, goal.kelvin, tint)
    return Moves(light=light, ask=ask)


def _hand_changed(scene: Scene, history: History, now: float) -> bool:
    """The camera's exposure is not what the director last wrote, and the write has had time to land."""

    if now - history.last_camera_step <= CONFIRM_SECONDS:
        return False
    for setting in EXPOSURE_SETTINGS:
        wanted = history.commanded.get(setting)
        seen = canonical(setting, scene.camera.get(setting))
        if wanted is not None and seen is not None and canonical(setting, wanted) != seen:
            return True
    return False


def _ladder(setting: str, goal: Goal) -> list[str]:
    """The values a take may step through for a setting, brightest first."""

    if setting == "aperture":
        return [f"f/{f:g}" for f in APERTURES if max(goal.widest, APERTURES[0]) - 1e-6 <= f <= TAKE_DARKEST_APERTURE]
    if setting == "iso":
        return [str(i) for i in reversed(ISO_STEPS)]
    base = min(SHUTTERS, key=lambda s: abs(float(s.split("/")[1]) - 2 * goal.frame_rate))
    return SHUTTERS[SHUTTERS.index(base) :]  # never slower than the base shutter for the frame rate


def _darkness(setting: str, value: Any) -> float | None:
    """Stops darker than f/1, ISO 1 or 1 s: one scale, so every ladder steps the same way."""

    number = _number(setting, value)
    if number is None or number <= 0:
        return None
    # Each f-number doubling is two stops; a higher ISO or a longer shutter is brighter.
    return 2 * math.log2(number) if setting == "aperture" else -math.log2(number)


def _next_step(scene: Scene, goal: Goal, darker: bool) -> tuple[CameraMove | None, bool]:
    """One 1/3 step toward darker or brighter, in the order aperture, ISO, shutter, and whether a
    setting was passed over because its next rung is more than a step away (off the ladder)."""

    off_ladder = False
    for setting in EXPOSURE_SETTINGS:
        current = _darkness(setting, scene.camera.get(setting))
        if current is None:
            continue
        ladder = _ladder(setting, goal)
        # 0.05 stop of slack: nominal values ("f/14", "1/125") are rounded, the smallest real gap is 0.2.
        if darker:
            steps = [v for v in ladder if _darkness(setting, v) > current + 0.05]
        else:
            steps = [v for v in reversed(ladder) if _darkness(setting, v) < current - 0.05]
        if not steps:
            continue
        if abs(_darkness(setting, steps[0]) - current) > STEP + STEP_SLACK:
            off_ladder = True  # e.g. f/1.8 on the way to f/2.8: a jump a viewer would see
            continue
        return CameraMove(setting, steps[0]), off_ladder
    return None, off_ladder


def _window_stops(scene: Scene, goal: Goal, history: History, face_level: float) -> float:
    """Stops to add to the camera for the window; the camera's meter speaks for a window it cannot see."""

    if scene.window is not None:
        return exposure_change(scene.window, face_level, goal.profile, goal.window_range)
    now_meter, start_meter = scene.camera.get("meter"), history.snapshot_meter
    if now_meter is None or start_meter is None:
        return 0.0
    change = now_meter - start_meter
    return -change if abs(change) > METER_STANDS_IN else 0.0


def _take(scene: Scene, rig: RigState, goal: Goal, history: History, now: float) -> Moves:
    if not scene.face:
        return Moves(held="no face")
    if history.snapshot is None:
        return Moves(held="no snapshot")
    level0, cb0, cr0 = history.snapshot
    dt = TAKE_TICK_SECONDS if history.last_tick is None else now - history.last_tick
    dt = min(MAX_TICK_SECONDS, max(0.0, dt))
    if not history.hands_off and _hand_changed(scene, history, now):
        history.hands_off = True

    # The face: back toward its level at record start, at most BRIGHTNESS_STOPS_PER_SECOND.
    brightness = float(rig.brightness if rig.brightness is not None else 50.0)
    stops = meter.stops_off(scene.level, level0, goal.profile)
    face_stops = 0.0
    held = None
    if abs(stops) > STOP_TOLERANCE:
        cap = BRIGHTNESS_STOPS_PER_SECOND * dt
        face_stops = max(-cap, min(cap, stops * history.gain))
        if stops > 0 and brightness >= 100.0:
            face_stops, held = 0.0, "face dark; the light is at full"
        elif stops < 0 and brightness <= MIN_BRIGHTNESS:
            face_stops, held = 0.0, "face bright; the light is at its lowest"
    new_brightness = min(100.0, max(MIN_BRIGHTNESS, brightness * 2**face_stops))

    # Colour against the snapshot, split into warmth and green: kelvin for one, tint for the other.
    dcb, dcr = scene.cb - cb0, scene.cr - cr0
    det = WARM[0] * GREEN[1] - GREEN[0] * WARM[1]
    warm = (dcb * GREEN[1] - GREEN[0] * dcr) / det
    green = (WARM[0] * dcr - dcb * WARM[1]) / det
    kelvin = rig.kelvin if rig.kelvin is not None else goal.kelvin
    new_kelvin = kelvin
    correction = warm * KELVIN_PER_WARM  # a warmer face wants a cooler (higher kelvin) key
    if abs(correction) >= KELVIN_DEADBAND:
        cap = KELVIN_PER_SECOND * dt
        new_kelvin = round(kelvin + max(-cap, min(cap, correction)))
        new_kelvin = min(KEY_KELVIN_RANGE[1], max(KEY_KELVIN_RANGE[0], new_kelvin))
    tint = rig.tint
    wanted_tint = -green * TINT_PER_GREEN * goal.tint_sign  # too green means less green from the light
    if abs(wanted_tint) >= 1 and now - history.last_tint_step >= TINT_STEP_SECONDS:
        stepped = max(-10, min(10, tint + (1 if wanted_tint > 0 else -1)))
        if stepped != tint:
            tint = stepped
            history.last_tint_step = now

    # The window: one 1/3 camera step, paired with the opposite change on the key. The face wins.
    camera = None
    window = _window_stops(scene, goal, history, level0)
    if abs(window) > STOP_TOLERANCE:
        darker = window < 0
        what = "window clipping" if darker else "window too dark"
        if history.hands_off:
            held = "camera changed by hand"
        elif now - history.last_camera_step >= CAMERA_STEP_SECONDS:
            step, off_ladder = _next_step(scene, goal, darker)
            paired = new_brightness * 2 ** (STEP if darker else -STEP)
            if step is None:
                held = f"{what}; off the ladder" if off_ladder else f"{what}; nothing left to step"
            elif paired > 100.0:
                held = f"{what}; the light is at full"
            elif paired < MIN_BRIGHTNESS:
                held = f"{what}; the light is at its lowest"
            else:
                camera, new_brightness = step, paired
    elif history.hands_off:
        held = "camera changed by hand"

    new_brightness = round(new_brightness, 1)
    light = None
    if (new_brightness, new_kelvin, tint) != (round(brightness, 1), kelvin, rig.tint):
        light = LightMove(new_brightness, new_kelvin, tint)
    return Moves(light=light, camera=camera, held=held)

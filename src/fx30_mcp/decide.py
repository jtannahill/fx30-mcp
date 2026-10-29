"""Decide what to do next: all judgment, no hardware.

decide() looks at one Scene, the rig as last written, the goal and the history, and returns
Moves: at most one light move, one camera move and one thing to ask of the person. It is pure
apart from updating history.gain, history.last_stops, history.refused and the card it aims at in setup,
history.hands_off, history.last_tint_step and the meter's average in a take, and the window's average,
clip memory and last move in both. Setup mode brings the shot to Ready; a value
the camera refused to take over USB is asked for by hand until the camera reads it. Balance mode is setup for the
light alone: no camera move and no key placement, for balance_shot. Take mode holds what recording
started with (start_take), slowly enough that a viewer never sees a change, and never asks.
The Rig applies the moves and the Director owns the history.

The window is judged by its brightness with the camera taken out (plan.window_scene), averaged over
WINDOW_SMOOTH_SECONDS, so one noisy frame moves nothing and the director's own steps never read as the light
changing. Setup aims it once at the middle of its band and then, like a take, moves only when it leaves
the band. A clipped frame is only a floor, so it can only darken, and the exposure it clipped at is
remembered: the camera never opens up to within CLIP_MARGIN of it until the light falls well below. A
window that keeps clipping in setup (its clip share, averaged, past WINDOW_CLIPPED) darkens the card a
third at a time until it stops, but never past a floor fixed when that starts, CLIP_FLOOR_MARGIN above
the mid grey under which the window is no longer found; there it asks for ND. A window too dark to be
found is judged in setup from its average.
And the owner's rule, that it does not jump: the director does not turn its own last move back (darker
then brighter, or brighter then darker) unless the average has moved more than the band is wide since.
Glints obey it. Exempt are a window that keeps clipping in setup, which only ever darkens, a camera
changed by hand since that move, which the director no longer counts as its own, and a window lost
darker than that floor, which may be opened up to find again.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from . import meter
from .plan import (
    APERTURES,
    CLIP,
    MIN_KEY_FT,
    SHUTTERS,
    UNEVEN_STOPS,
    WINDOW_CLIPPED,
    CameraCard,
    Geometry,
    camera_card,
    closer,
    exposure_of,
    fmt_ft,
    window_band,
    window_scene,
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

# The window, in setup and in a take.
WINDOW_SMOOTH_SECONDS = 4.0  # the average's time constant; an unclipped average younger than this aims nothing
WINDOW_MAX_DT = 2.0  # seconds: a frame after a long gap (a key check) counts no more than this toward the average
CLIP_MARGIN = 1 / 3  # stops: opening up stops this far short of an exposure the window clipped at
CLIP_FORGET = 1.0  # stops the average must fall below where the window clipped before that is forgotten
# Stops above mid grey (where meter.find_window stops counting a window) that darkening for a window that keeps
# clipping stops short of: room for a frame a third of a stop dark, from an average a third high.
CLIP_FLOOR_MARGIN = 2 / 3
ROUNDING = 0.05  # stops: nominal values ("f/14", "1/125") are rounded
MAX_CARD_CHANGES = 6  # setup: new cards aimed at before the card is held (a clipped window still darkens it)

CANT_HOLD = "A sheer curtain or ND film on the glass would. Or open up and let the window sit brighter."
DARKEST = ("The camera can't darken this window any further", "ND film or a sheer curtain on the glass would.")
NO_FACE = ("Sit in the shot", "The coach needs your face in the frame to plan.")
CONNECTING = ("Connecting to the light", "This can take up to two minutes.")
# The setting each camera move writes, as the page names it when the owner must set it by hand.
LABELS = {
    "aperture": "Aperture",
    "shutter": "Shutter",
    "iso": "ISO",
    "kelvin": "White balance",
    "exposure_mode": "Exposure mode",
    "live_view_effect": "Live view effect",
}
BY_HAND = " on the camera"
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
    refused: dict[str, str] = field(default_factory=dict)  # setup: setting -> the value the camera refused
    camera_refused: str | None = None  # take: why camera steps are off for the rest of it ("camera refused f/22")
    last_camera_step: float = -math.inf
    last_tint_step: float = -math.inf
    last_tick: float | None = None
    # The window's brightness with the camera taken out (plan.window_scene), averaged over WINDOW_SMOOTH_SECONDS.
    window_scene: float | None = None
    window_seconds: float = 0.0  # how long the average has run since it started, or since a clipped floor
    window_bound: bool = False  # the average is a clipped frame's floor, not a reading
    clip_ceiling: float | None = None  # the lowest camera exposure the window was seen clipping at
    clip_scene: float | None = None  # the average when it last clipped
    clip_share: float = 0.0  # the share of the window at white, averaged: past WINDOW_CLIPPED it keeps clipping
    clip_floor: float | None = None  # setup: the darkest exposure a window that keeps clipping may be taken to
    aimed: bool = False  # setup has aimed the window at the middle of its band from a settled average
    card: CameraCard | None = None  # setup: the card last aimed at
    card_from: tuple[str | None, ...] | None = None  # setup: what the camera read when it was aimed
    card_changes: int = 0  # setup: new cards aimed at since setup began or the last take
    last_move: int = 0  # the director's last change of exposure for the window: -1 darker, 1 brighter
    last_move_scene: float | None = None  # the window's average when it made it
    last_move_exposure: float | None = None  # the camera's exposure it left: a different one was set by hand
    meter_scene: float | None = None  # take: the camera's meter with its exposure taken out, averaged


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


def camera_exposure(camera: dict[str, Any]) -> float | None:
    """Stops of exposure (plan.exposure_of) from the camera's readback in any of its forms; None when it
    cannot say."""

    values = [canonical(s, camera.get(s)) for s in ("iso", "shutter", "aperture")]
    if None in values:
        return None
    try:
        return exposure_of(*values)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def decide(scene: Scene, rig: RigState, goal: Goal, mode: str, history: History, now: float) -> Moves:
    if mode in ("setup", "balance"):
        return _setup(scene, rig, goal, history, now, light_only=mode == "balance")
    if mode == "take":
        return _take(scene, rig, goal, history, now)
    raise ValueError(f"unknown mode {mode!r}")


def start_take(scene: Scene, history: History) -> None:
    """Recording has started: hold this face and these camera settings through the take."""

    history.snapshot = (scene.level, scene.cb, scene.cr)
    history.snapshot_meter = scene.camera.get("meter")
    exposure = camera_exposure(scene.camera)
    history.meter_scene = None
    if history.snapshot_meter is not None and exposure is not None:
        history.meter_scene = history.snapshot_meter - exposure  # the meter rises with exposure: take it out
    history.card, history.card_from, history.card_changes, history.clip_floor = None, None, 0, None
    history.hands_off = False
    history.camera_refused = None
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


def end_take(history: History) -> None:
    """Recording has stopped: setup may aim new cards again."""

    history.card, history.card_from, history.card_changes, history.clip_floor = None, None, 0, None


def _dt(history: History, now: float) -> float:
    """Seconds since the last tick, as the window's and the meter's averages count them."""

    dt = TAKE_TICK_SECONDS if history.last_tick is None else now - history.last_tick
    return min(WINDOW_MAX_DT, max(0.0, dt))


def _smooth(average: float | None, reading: float, dt: float) -> float:
    if average is None:
        return reading  # the first reading starts the average
    return average + (1 - math.exp(-dt / WINDOW_SMOOTH_SECONDS)) * (reading - average)


def _watch_window(window: meter.Window, profile: str, exposure: float, history: History, now: float) -> bool:
    """Fold this frame's window into the average and the clip memory; True when it is clipping.

    A clipped frame's level is only a floor: the average rises to it at once and never falls for it. The
    first real reading after a floor replaces it, since a floor says nothing about how far over it is."""

    reading = window_scene(window.level, profile, exposure)
    clipped = window.clipped > WINDOW_CLIPPED
    dt = _dt(history, now)
    average = history.window_scene
    history.clip_share = _smooth(history.clip_share, window.clipped, dt)
    # Once settled, a floor is folded in without starting over, so a glint never brings back "window settling".
    young = average is None or history.window_seconds < WINDOW_SMOOTH_SECONDS
    if clipped:
        raised = average is None or reading > average
        if raised:
            history.window_scene, history.window_bound = reading, True
        history.window_seconds = 0.0 if young and raised else history.window_seconds + dt
    elif average is None or (young and history.window_bound and reading > average):
        history.window_scene, history.window_seconds, history.window_bound = reading, 0.0, False
    else:
        history.window_scene = _smooth(average, reading, dt)
        history.window_seconds += dt
        history.window_bound = False
    if clipped:
        history.clip_ceiling = exposure if history.clip_ceiling is None else min(history.clip_ceiling, exposure)
        history.clip_scene = history.window_scene
    elif history.clip_ceiling is not None and history.window_scene < history.clip_scene - CLIP_FORGET:
        _forget_clip(history)  # the light changed: a cloud, the evening
    return clipped


def _forget_clip(history: History) -> None:
    history.clip_ceiling = history.clip_scene = history.clip_floor = None


def _too_dark_to_see(history: History, profile: str, exposure: float) -> bool:
    """No window was found, but one was seen, and at this exposure it would sit under mid grey, where
    meter.find_window no longer counts it: it is too dark to see, not gone."""

    return history.window_scene is not None and 2 ** (history.window_scene + exposure) < meter.to_linear(0.5, profile)


def _window_wanted(history: History, goal: Goal, target: float) -> tuple[float, float]:
    """The camera exposure that puts the average window at the middle of its band over target, and how far
    either side of it the band reaches."""

    lo, hi = window_band(target, goal.profile, goal.window_range)
    wanted = math.log2(meter.to_linear(target, goal.profile)) + (lo + hi) / 2 - history.window_scene
    return wanted, (hi - lo) / 2


def _capped(wanted: float, exposure: float, history: History, clipped: bool) -> float:
    """wanted, but never brighter than now for a clipped frame, nor within CLIP_MARGIN of where it clipped."""

    if wanted <= exposure:
        return wanted
    if clipped:
        return exposure
    if history.clip_ceiling is not None:
        return max(exposure, min(wanted, history.clip_ceiling - CLIP_MARGIN))
    return wanted


def _turns_back(stops: float, history: History, band: float) -> bool:
    """Moving by stops would reverse the last change while the average has moved no more than band since."""

    if stops == 0 or history.last_move == 0 or (stops > 0) == (history.last_move > 0):
        return False
    return abs(history.window_scene - history.last_move_scene) <= band


def _moved(stops: float, history: History, exposure: float | None) -> None:
    """Remember which way the exposure just moved for the window, the average it moved at, and where it left
    the camera."""

    history.last_move, history.last_move_scene = (1 if stops > 0 else -1), history.window_scene
    history.last_move_exposure = exposure


def _near_clip(scene: Scene, step: CameraMove, history: History) -> bool:
    """Opening up by step would take the camera within CLIP_MARGIN of an exposure the window clipped at."""

    if history.clip_ceiling is None:
        return False
    after = camera_exposure({**scene.camera, step.setting: step.value})
    return after is not None and after > history.clip_ceiling - CLIP_MARGIN + ROUNDING


def _camera_move(scene: Scene, rig: RigState, goal: Goal) -> CameraMove | None:
    camera = rig.camera
    if camera.get("live_view_effect") is False:
        return CameraMove("live_view_effect", "On")
    if camera.get("exposure_mode") not in (None, "M"):
        return CameraMove("exposure_mode", "M")
    if camera.get("kelvin") is not None and canonical("kelvin", camera["kelvin"]) != str(goal.kelvin):
        return CameraMove("kelvin", str(goal.kelvin))
    return None


def _card_values(card: CameraCard | dict[str, Any]) -> tuple[str | None, ...]:
    get = card.get if isinstance(card, dict) else lambda s: getattr(card, s)
    return tuple(canonical(s, get(s)) for s in CARD_SETTINGS)


def _card_exposure(card: CameraCard) -> float:
    return exposure_of(card.iso, card.shutter, card.aperture)


def _toward(card: CameraCard | None, rig: RigState) -> CameraMove | None:
    """The first setting of card the camera does not read yet: one setting per tick."""

    if card is None:
        return None
    for setting in CARD_SETTINGS:
        current = canonical(setting, rig.camera.get(setting))
        wanted = canonical(setting, getattr(card, setting))
        if current is not None and current != wanted:
            return CameraMove(setting, getattr(card, setting))
    return None


def _card_move(
    scene: Scene, rig: RigState, goal: Goal, history: History, now: float
) -> tuple[CameraMove | None, Ask | None, str | None]:
    """The next card setting to write, an ask when the camera can darken the window no further, and why the
    card waits when it is waiting on the window."""

    # The camera reads back in several forms ("ISO 800", '1/60"', "6.3"); the card wants one.
    readback = {s: canonical(s, rig.camera[s]) for s in CARD_SETTINGS if rig.camera.get(s) is not None}
    current = {**rig.camera, **readback}
    exposure = camera_exposure(rig.camera)
    found = scene.window is not None
    floor = history.clip_floor
    if not found and exposure is not None and floor is not None and exposure < floor - ROUNDING:
        # Lost, darker than a clipping window is ever taken (stopped down by hand, or the light fell): forget the
        # clip and the last move, so the card may open up and find it again.
        _forget_clip(history)
        history.last_move = 0
    if not found and (exposure is None or not _too_dark_to_see(history, goal.profile, exposure)):
        return _toward(camera_card(current, 0.0, goal.frame_rate, goal.widest, False), rig), None, None
    if exposure is None:
        return None, None, None
    clipped = found and _watch_window(scene.window, goal.profile, exposure, history, now)
    # A card still being written is walked to the end, one setting per tick, and judged as if written: a
    # shutter brought back to base can make the first write darker than the whole card. A setting that is
    # neither the card's nor what the camera read before it was set by hand: the card is dropped.
    walking = _toward(history.card, rig)
    if walking is not None and history.card_from is not None:
        allowed = zip(_card_values(history.card), history.card_from)
        if any(seen not in pair for seen, pair in zip(_card_values(current), allowed)):
            history.card = walking = None
    basis = _card_exposure(history.card) if walking is not None else exposure
    moved_to = history.last_move_exposure
    if walking is None and history.last_move and moved_to is not None and abs(exposure - moved_to) > ROUNDING:
        history.last_move = 0  # changed by hand since the director's last move: not its own to turn back
    wanted, half = _window_wanted(history, goal, goal.face_level)
    floored = False
    if clipped and history.clip_share > WINDOW_CLIPPED:
        # It keeps clipping: a third below where it clipped, whichever way the card last moved (it only ever
        # darkens, so it cannot loop), but never so dark that the window sinks out of sight. That floor is fixed
        # when the darkening starts, from the average then; a window white to its middle needs none (it cannot
        # be lost, and its average is only a floor), so it is fixed once the middle reads.
        if scene.window.level >= CLIP[goal.profile]:
            history.clip_floor = None
        elif history.clip_floor is None:
            mid_grey = math.log2(meter.to_linear(0.5, goal.profile))
            history.clip_floor = mid_grey + CLIP_FLOOR_MARGIN - history.window_scene
        target = min(wanted, basis, exposure - CLIP_MARGIN)
        if history.clip_floor is not None and target < history.clip_floor:
            target, floored = history.clip_floor, True
        if basis <= target + ROUNDING:
            return walking, Ask(*DARKEST) if floored else None, None  # the card already goes as dark as it may
    elif clipped:
        if wanted > basis - STOP_TOLERANCE:
            return walking, None, None  # a glint, no darker than the average says: nothing new to aim at
        target = wanted  # darker than the card, so never brighter
        if _turns_back(target - basis, history, 2 * half):
            return walking, None, None
    else:
        if found and history.window_seconds < WINDOW_SMOOTH_SECONDS:
            return walking, None, "window settling"  # a window not found cannot settle: it is judged from memory
        if history.aimed and abs(basis - wanted) <= half:
            return walking, None, None  # inside its band: leave the card alone
        history.aimed = True  # aim once at the middle; from here on only leaving the band moves it
        target = _capped(wanted, basis, history, False)
        if _turns_back(target - basis, history, 2 * half):
            return walking, None, None
    stops = target - exposure
    if floored:
        stops = math.ceil(3 * (stops - ROUNDING)) / 3  # whole thirds, so the card's rounding cannot pass the floor
    elif stops > 0 and history.clip_ceiling is not None:
        stops = math.floor(3 * (stops + ROUNDING)) / 3  # whole thirds, so the card's rounding cannot pass the cap
    card = camera_card(current, stops, goal.frame_rate, goal.widest, True)
    if clipped and _card_exposure(card) > min(basis, exposure) - ROUNDING:
        return walking, Ask(*DARKEST), None  # no darker card: only ND or a curtain can help
    if _card_values(card) != _card_values(history.card or current):
        # Past MAX_CARD_CHANGES the card holds; a clipped window only ever darkens it, so it cannot loop.
        if history.card_changes < MAX_CARD_CHANGES or clipped:
            history.card, history.card_from = card, _card_values(current)
            history.card_changes += 1
            change = _card_exposure(card) - basis
            if abs(change) > ROUNDING:
                _moved(change, history, _card_exposure(card))
    return _toward(history.card, rig), None, None


def _reads_as(setting: str, readback: Any, value: str) -> bool:
    if setting == "live_view_effect":
        return readback is True
    return readback is not None and canonical(setting, readback) == canonical(setting, value)


def _shown(move: CameraMove) -> str:
    if move.setting == "kelvin":
        return f"{move.value}K"
    if move.setting in CARD_SETTINGS:
        return canonical(move.setting, move.value)
    return move.value


def _write_or_ask(move: CameraMove, history: History) -> Moves:
    """The camera move, or, when the camera refused this very value, an ask to set it by hand."""

    refused = history.refused.get(move.setting)
    if refused is not None and canonical(move.setting, refused) == canonical(move.setting, move.value):
        return Moves(ask=Ask(f"Set {_shown(move)}{BY_HAND}", LABELS[move.setting]))
    return Moves(camera=move)


def by_hand(ask: Ask | None) -> bool:
    """The ask is for a camera setting the camera would not take over USB."""

    return ask is not None and ask.instruction.endswith(BY_HAND) and ask.detail in LABELS.values()


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


def _setup(scene: Scene, rig: RigState, goal: Goal, history: History, now: float, light_only: bool = False) -> Moves:
    """Light only (balance mode) leaves the camera and the key's placement as they are."""

    for setting, value in list(history.refused.items()):
        if _reads_as(setting, rig.camera.get(setting), value):
            del history.refused[setting]  # set by hand: the ask is done
    move = None if light_only else _camera_move(scene, rig, goal)
    if move is not None:
        return _write_or_ask(move, history)
    if not scene.face:
        return Moves(ask=Ask(*NO_FACE))
    move, window_ask, held = (None, None, None) if light_only else _card_move(scene, rig, goal, history, now)
    if move is not None:
        return _write_or_ask(move, history)
    ask = None if light_only else _key_ask(goal, history)
    if ask is not None:
        return Moves(ask=ask, held=held)

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
    return Moves(light=light, ask=ask or window_ask, held=held)  # held: not Ready while the window settles


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


def _window_stops(
    scene: Scene, goal: Goal, history: History, face_level: float, now: float
) -> tuple[float, str | None]:
    """Stops toward where the window should be, 0 to hold: once the average has left its band, or on a
    clipped frame; and why it holds when the rule against turning back holds it. The camera's meter speaks
    for a window it cannot see."""

    exposure = camera_exposure(scene.camera)
    if exposure is None:
        return 0.0, None
    meter_now = scene.camera.get("meter")
    if meter_now is not None:
        # The meter rises with exposure: taken out, the director's own steps are not the light changing.
        history.meter_scene = _smooth(history.meter_scene, meter_now - exposure, _dt(history, now))
    if scene.window is not None:
        clipped = _watch_window(scene.window, goal.profile, exposure, history, now)
        wanted, half = _window_wanted(history, goal, face_level)
        if not clipped and (history.window_seconds < WINDOW_SMOOTH_SECONDS or abs(exposure - wanted) <= half):
            return 0.0, None
        stops = _capped(wanted, exposure, history, clipped) - exposure
        if not clipped and wanted - exposure > STOP_TOLERANCE >= stops:
            return 0.0, "window dark; a step brighter clipped"
        if abs(stops) > STOP_TOLERANCE and _turns_back(stops, history, 2 * half):
            return 0.0, f"window {'dark' if stops > 0 else 'bright'}; too soon to turn back"
        return stops, None
    if history.snapshot_meter is None or history.meter_scene is None:
        return 0.0, None
    change = history.meter_scene + exposure - history.snapshot_meter  # the meter now, smoothed, against record start
    return (-change if abs(change) > METER_STANDS_IN else 0.0), None


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
    window, why = _window_stops(scene, goal, history, level0, now)
    if abs(window) > STOP_TOLERANCE:
        darker = window < 0
        what = "window clipping" if darker else "window too dark"
        if history.hands_off:
            held = "camera changed by hand"
        elif history.camera_refused is not None:
            held = history.camera_refused
        elif now - history.last_camera_step >= CAMERA_STEP_SECONDS:
            step, off_ladder = _next_step(scene, goal, darker)
            paired = new_brightness * 2 ** (STEP if darker else -STEP)
            if step is not None and not darker and _near_clip(scene, step, history):
                held = "window dark; a step brighter clipped"  # it sits low rather than clip again
            elif step is None:
                held = f"{what}; off the ladder" if off_ladder else f"{what}; nothing left to step"
            elif paired > 100.0:
                held = f"{what}; the light is at full"
            elif paired < MIN_BRIGHTNESS:
                held = f"{what}; the light is at its lowest"
            else:
                camera, new_brightness = step, paired
                if scene.window is not None:
                    _moved(window, history, camera_exposure({**scene.camera, step.setting: step.value}))
    elif history.hands_off:
        held = "camera changed by hand"
    elif why is not None:
        held = held or why

    new_brightness = round(new_brightness, 1)
    light = None
    if (new_brightness, new_kelvin, tint) != (round(brightness, 1), kelvin, rig.tint):
        light = LightMove(new_brightness, new_kelvin, tint)
    return Moves(light=light, camera=camera, held=held)

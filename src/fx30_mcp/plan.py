"""Plan the shot: where the key goes, what the camera is set to, and how it looks.

Everything here is pure: a survey and the settings go in, a plan comes out.
Stops are log2 of light; positive means brighter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any

from . import meter
from .config import Settings
from .survey import Survey

CLIP = {"rec709": 0.97, "slog3": 0.94}  # where a level counts as white
AMBER_BELOW = 1.5  # a window under this many stops over the face reads as a dull grey backdrop
MIN_KEY_FT = 1.5  # closer than this the key is impractical
APERTURES = [2.8, 3.2, 3.5, 4.0, 4.5, 5.0, 5.6, 6.3, 7.1, 8.0, 9.0, 10.0, 11.0, 13.0, 14.0, 16.0, 18.0, 20.0, 22.0]
SHUTTERS = ["1/50", "1/60", "1/80", "1/100", "1/125", "1/160", "1/200", "1/250", "1/320", "1/400", "1/500"]
STOP_APERTURE = APERTURES.index(16.0)  # stop down this far before raising the shutter
TURNED_DEGREES = 15
UNEVEN_STOPS = 1 / 3
WINDOW_CLIPPED = 0.02  # more of the window than this at white: its level is only a floor
# Stops: the window's band is never narrower, however little headroom the profile leaves (Rec.709 at 0.60
# has 1.36 stops over the face; without this the band was 1.03 to 1.03 and the camera hunted forever).
MIN_WINDOW_BAND = 2 / 3
ND_NOTE = "Shutter above {base} makes motion a little crisper. A variable ND would bring it back to {base}."


def exposure_of(iso: str, shutter: str, aperture: str) -> float:
    """Stops of exposure relative to ISO 800, 1/60 and f/2.8."""

    shutter = shutter.strip('"')
    if "/" in shutter:
        top, bottom = shutter.split("/")
        seconds = float(top) / float(bottom)
    else:
        seconds = float(shutter)
    f_number = float(aperture.lstrip("fF/"))
    return math.log2(float(iso) / 800) + math.log2(seconds * 60) - 2 * math.log2(f_number / 2.8)


def choose_key_side(
    yaw: float | None,
    ambient: tuple[float, float] | None,
    profile: str,
    override: str | None = None,
) -> tuple[str, str]:
    """The side of the camera the key goes on, and the one sentence saying why."""

    if override:
        return override, "You chose this side."
    if yaw is not None and abs(yaw) > TURNED_DEGREES:
        return ("left" if yaw > 0 else "right"), "You're turned to one side, so the key lights the far side of your face."
    if ambient is not None:
        left, right = ambient
        if abs(meter.stops_off(min(ambient), max(ambient), profile)) > UNEVEN_STOPS:
            lit = "left" if left > right else "right"
            other = "right" if lit == "left" else "left"
            why = f"The window already lights the camera-{lit} side of your face, so the key goes on the other side to give your face shape."
            return other, why
    return "left", "Nothing in the room favours a side, so the key goes camera-left."


@dataclass
class Geometry:
    side: str
    angle_deg: int = 40
    height_ft: float = 1.5
    aim_down_deg: int = 25
    distance_ft: float = 4.0


def key_geometry(side: str, modifier: str | None, size_in: float | None) -> Geometry:
    """Where the key goes: a softbox-like modifier starts at 1.25 times its width, anything else at 4 ft."""

    if modifier in ("softbox", "lantern", "umbrella") and size_in:
        return Geometry(side, distance_ft=max(MIN_KEY_FT, 1.25 * size_in / 12))
    return Geometry(side)


def closer(distance_ft: float, stops_short: float) -> float:
    """The distance that makes up stops_short: half the distance is two stops."""

    return distance_ft * 2 ** (-stops_short / 2)


def fmt_ft(feet: float) -> str:
    """Feet to the nearest half; under 2 ft in inches."""

    halves = math.floor(feet * 2 + 0.5) / 2
    if halves < 2:
        return f"{round(halves * 12)} in"
    return f"{halves:g} ft"


def headroom(target: float, profile: str) -> float:
    """Stops from the face target up to white."""

    return meter.stops_off(target, CLIP[profile], profile)


def window_band(target: float, profile: str, window_range: list[float]) -> tuple[float, float]:
    """Stops over the face the window may sit between: its top a third below white, at least MIN_WINDOW_BAND wide."""

    hi = min(window_range[1], headroom(target, profile) - 1 / 3)
    return min(window_range[0], hi - MIN_WINDOW_BAND), hi


def window_scene(level: float, profile: str, exposure: float) -> float:
    """The window's brightness with the camera taken out: stops as ISO 800, 1/60 and f/2.8 would show it.
    Changing the camera does not change it; changing the light does."""

    return math.log2(max(meter.to_linear(level, profile), 1e-6)) - exposure


def exposure_change(window: meter.Window | None, target: float, profile: str, window_range: list[float]) -> float:
    """Stops to add to the camera (negative darkens) so the window sits at the middle of its band over the face."""

    if window is None:
        return 0.0
    if window.clipped > WINDOW_CLIPPED:
        return -1.0  # a step, then measure again: a clipped window says nothing about how far over it is
    lo, hi = window_band(target, profile, window_range)
    over = meter.stops_off(target, window.level, profile)
    if lo <= over <= hi:
        return 0.0  # already in its band: leave the camera alone
    return (lo + hi) / 2 - over


@dataclass
class CameraCard:
    iso: str
    shutter: str
    aperture: str
    note: str | None = None


def _nearest(values: list[float], wanted: float) -> int:
    return min(range(len(values)), key=lambda i: abs(values[i] - wanted))


def _shutter_value(text: str) -> float:
    return float(text.split("/")[1]) if "/" in text else 1 / float(text.strip('"'))


def camera_card(current: dict[str, Any], stops: float, frame_rate: float, widest: float, has_window: bool) -> CameraCard:
    """The camera settings that move the exposure by stops: aperture first, shutter only when stopped down."""

    denominators = [_shutter_value(s) for s in SHUTTERS]
    base = _nearest(denominators, 2 * frame_rate)
    shutter = _nearest(denominators, _shutter_value(str(current["shutter"])))
    aperture = _nearest(APERTURES, float(str(current["aperture"]).lstrip("fF/")))
    open_limit = min(i for i, f in enumerate(APERTURES) if f >= max(widest, 2.8) - 1e-6)
    # A shutter above base goes back to base first, the aperture closing to match, as far as f/16.
    traded = max(0, min(shutter - base, STOP_APERTURE - aperture))
    shutter -= traded
    aperture += traded
    # ISO goes back to base, so whatever the current ISO was adding comes off the move.
    thirds = round(3 * (stops + math.log2(float(current["iso"]) / 800)))
    iso = "800"
    if thirds < 0:
        stopping = max(0, min(-thirds, STOP_APERTURE - aperture))
        aperture += stopping
        shutter = min(len(SHUTTERS) - 1, shutter + (-thirds - stopping))
    elif thirds > 0:
        returning = max(0, min(thirds, shutter - base))
        shutter -= returning
        opening = thirds - returning
        wanted = aperture - opening
        aperture = max(open_limit, wanted)
        if not has_window and wanted < open_limit - 3 and aperture == open_limit:
            iso = "2500"
    note = ND_NOTE.format(base=SHUTTERS[base]) if shutter > base else None
    return CameraCard(iso, SHUTTERS[shutter], f"f/{APERTURES[aperture]:g}", note)


def kelvin_for(window: meter.Window | None) -> int:
    """5600, or bluer light when the window reads clearly blue, so the backdrop does not go cyan."""

    if window is None or window.blue <= 0.03:
        return 5600
    return 6500 if window.blue > 0.06 else 6000


def face_light(stops: float) -> str:
    off = abs(stops)
    return "green" if off <= 1 / 6 else "amber" if off <= 2 / 3 else "red"


def ratio_light(contrast: float | None, ratio: float) -> str | None:
    if contrast is None:
        return None
    off = abs(contrast - ratio)
    return "green" if off <= 0.25 else "amber" if off <= 0.75 else "red"


def window_light(
    window: meter.Window | None,
    face_level: float,
    profile: str,
    truthful: bool,
) -> str | None:
    """None with no window; amber when it cannot be judged or is dull or near white; red when clipping.

    "Dull" is under AMBER_BELOW stops over the face, or less where the profile has little headroom
    (Rec.709). A bright window that is not clipping is fine."""

    if window is None:
        return None
    if not truthful:
        return "amber"
    if window.clipped > WINDOW_CLIPPED:
        return "red"
    over = meter.stops_off(face_level, window.level, profile)
    to_clip = meter.stops_off(window.level, CLIP[profile], profile)
    # The bottom of the band the director aims in, so a window at its aim reads green, not amber and green by turns.
    dull = min(AMBER_BELOW, headroom(face_level, profile) - 1 / 3 - MIN_WINDOW_BAND)
    return "amber" if over < dull or to_clip < 1 / 3 else "green"


@dataclass
class Plan:
    geometry: Geometry
    why: str
    camera: CameraCard
    kelvin: int
    key_percent: float | None
    fill_side: str
    fill_ratio: float
    note: str | None = None

    def to_json(self) -> dict:
        return asdict(self)


def _predict_key_percent(settings: Settings, distance_ft: float, exposure: float) -> float | None:
    """How bright the key should be, from its recorded output; None for a different modifier."""

    output = settings.key_output
    if not output or output.get("modifier") != settings.key_modifier:
        return None
    stops = output["stops_at_full"] + 2 * math.log2(output["distance_ft"] / distance_ft) + (exposure - output["exposure"])
    return float(round(min(100.0, max(1.0, 100 * 2 ** (-stops)))))


def _widest_offered(apertures: list[Any]) -> float | None:
    """The smallest f-number the camera offers; choices that are not numbers are skipped."""

    numbers = []
    for choice in apertures:
        try:
            numbers.append(float(str(choice).lstrip("fF/")))
        except ValueError:
            pass
    return min(numbers, default=None)


def make_plan(
    survey: Survey,
    settings: Settings,
    override: str | None = None,
    geometry: Geometry | None = None,
) -> Plan:
    """The plan for a survey; given a geometry (where the key already is), the key stays there."""

    side, why = choose_key_side(survey.yaw, survey.ambient, settings.profile, override)
    if geometry is None:
        geometry = key_geometry(side, settings.key_modifier, settings.key_size_in)
    side = geometry.side
    frame_rate = survey.info.get("frame_rate") or settings.frame_rate or 29.97
    lens = _widest_offered(survey.info.get("apertures") or [])
    widest = settings.widest_aperture or lens or 2.8
    stops = exposure_change(survey.window, settings.target, settings.profile, settings.window_range)
    has_window = survey.window is not None
    current = survey.camera
    if not has_window:
        # Nothing bright to protect: open up as wide as the lens goes, at base ISO and base shutter.
        base = SHUTTERS[_nearest([_shutter_value(s) for s in SHUTTERS], 2 * frame_rate)]
        stops = exposure_of("800", base, f"f/{max(widest, 2.8):g}") - exposure_of(
            current["iso"], current["shutter"], current["aperture"]
        )
    card = camera_card(current, stops, frame_rate, widest, has_window)
    exposure = exposure_of(card.iso, card.shutter, card.aperture)
    note = None
    if has_window and survey.info.get("picture_profile") == "Off":
        h = headroom(settings.target, "rec709")
        note = f"Picture Profile off holds about {h:.1f} stops over your face; S-Log3 would keep more of the window."
    return Plan(
        geometry=geometry,
        why=why,
        camera=card,
        kelvin=kelvin_for(survey.window),
        key_percent=_predict_key_percent(settings, geometry.distance_ft, exposure),
        fill_side="right" if side == "left" else "left",
        fill_ratio=settings.fill_ratio,
        note=note,
    )

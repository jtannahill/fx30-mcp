"""Light the face to a target with a key light and a fill, leaving the camera alone.

For a talking head the camera settings are best set once: shutter at twice the
frame rate, ISO at a native base, aperture for the depth of field you want.
That leaves the light to set exposure. The loop reads a live-view frame, works
out how many stops the face is off, moves the light's brightness by that much,
and repeats. With the camera's white balance set to the light's colour
temperature, what is left of a colour cast is mostly green or magenta, which
the light's tint corrects.

With a fill light, the same loop also sets the lighting ratio: how many stops
the shadow side of the face sits below the lit side. One stop (2:1) is the
classic interview look; two stops (4:1) is moodier.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable

from . import meter
from .camera import CameraBase, CameraError
from .config import Settings

STOP_TOLERANCE = 1 / 6
HUE_DEADBAND = 5.0  # degrees; skin tones spread a few degrees either side of the line
DEGREES_PER_TINT = 3.0  # a first guess at how far one tint step moves skin on the scope
MAX_TINT_STEP = 3
RATIO_TOLERANCE = 0.25
CLIP_LIMIT = 0.03  # more of the face than this at white is shine to take down
MIN_BRIGHTNESS = 1.0
DEFAULT_KELVIN = 5600


@dataclass
class Step:
    brightness: float
    tint: int
    face: bool
    level: float
    stops: float
    hue_off: float | None
    clipped: float
    fill: float | None = None
    contrast: float | None = None


def _round(step: Step) -> dict[str, Any]:
    out = asdict(step)
    for key in ("brightness", "level", "stops", "clipped", "fill", "contrast"):
        if out[key] is not None:
            out[key] = round(out[key], 3)
    if step.hue_off is not None:
        out["hue_off"] = round(step.hue_off, 1)
    return out


def advice(stops: float, profile: str) -> str:
    if stops > 0:
        iso = (
            "In S-Log3 the FX30's second native ISO, 2500, is about 1.6 stops above 800. "
            if profile == "slog3" else "Raise ISO. "
        )
        return (
            f"The key light is at full and the face is still {stops:.1f} stops under. {iso}"
            "Open the aperture, or bring the light closer: half the distance is two stops more."
        )
    return (
        f"The key light is at its lowest and the face is still {-stops:.1f} stops over. "
        "Move the light back (double the distance is two stops less), add diffusion, "
        "stop down or lower ISO. The FX30 has no built-in ND."
    )


async def balance(
    camera: CameraBase,
    light: Any,
    settings: Settings,
    kelvin: int | None = None,
    fix_tint: bool = True,
    max_steps: int = 8,
    settle: float = 0.7,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    fill: Any = None,
) -> dict[str, Any]:
    """Bring the face to settings.target with the key light and, given a fill light,
    the shadow side to settings.fill_ratio stops below the lit side. Returns what it
    did and where it ended."""

    if light.state.brightness is None or light.state.kelvin is None:
        try:
            await light.read_status()
        except Exception:
            pass
    kelvin = kelvin or light.state.kelvin or DEFAULT_KELVIN
    brightness = float(light.state.brightness or 50)
    tint = int(light.state.tint or 0) if fix_tint else 0
    notes: list[str] = []
    try:
        await camera.set_kelvin(kelvin)
    except CameraError as err:
        notes.append(f"camera white balance not set ({err}); set it to {kelvin}K by hand")
    await light.set_white(kelvin, brightness, tint)
    fill_level = None
    if fill is not None:
        if fill.state.brightness is None:
            try:
                await fill.read_status()
            except Exception:
                pass
        fill_level = float(fill.state.brightness or 20) if fill.state.power is not False else 20.0
        await fill.set_white(kelvin, fill_level, 0)
    await sleep(settle)

    steps: list[Step] = []
    gain = 1.0
    fill_gain = 1.0
    fill_advice = None
    done = False
    result_advice = None
    tint_flipped = False
    for _ in range(max_steps):
        reading = await asyncio.to_thread(meter.read, await camera.frame())
        stops = meter.stops_off(reading.lit, settings.target, settings.profile)
        if reading.clipped > CLIP_LIMIT:
            stops = min(stops, -1 / 3)
        # Skin colour means something only on skin.
        hue_off = reading.hue_off(settings.skin_hue) if fix_tint and reading.face else None
        contrast = reading.contrast(settings.profile) if fill is not None and reading.face else None
        step = Step(brightness, tint, reading.face, reading.lit, stops, hue_off, reading.clipped,
                    fill_level, contrast)

        if steps and fix_tint and steps[-1].tint != tint and hue_off is not None and steps[-1].hue_off is not None:
            before = steps[-1].hue_off
            if before * hue_off > 0 and abs(hue_off) > abs(before) + 2:
                # The tint step made the cast worse: this light counts tint the other way.
                settings.tint_sign = -settings.tint_sign
                tint_flipped = True
        if steps and abs(stops) > STOP_TOLERANCE and steps[-1].stops * stops < 0:
            gain *= 0.5  # overshot: the light's dimming curve is not linear here
        ratio_off = None if contrast is None else contrast - settings.fill_ratio
        if steps and ratio_off is not None and steps[-1].contrast is not None \
                and abs(ratio_off) > RATIO_TOLERANCE and (steps[-1].contrast - settings.fill_ratio) * ratio_off < 0:
            fill_gain *= 0.5
        steps.append(step)

        exposure_ok = abs(stops) <= STOP_TOLERANCE
        tint_ok = hue_off is None or abs(hue_off) <= HUE_DEADBAND or not light.caps.tint
        ratio_ok = ratio_off is None or abs(ratio_off) <= RATIO_TOLERANCE
        if exposure_ok and tint_ok and ratio_ok:
            done = True
            break

        new_brightness = brightness
        if not exposure_ok:
            new_brightness = min(100.0, max(MIN_BRIGHTNESS, brightness * 2 ** (stops * gain)))
            if round(new_brightness, 1) == round(brightness, 1):
                result_advice = advice(stops, settings.profile)
        new_tint = tint
        if not tint_ok:
            size = min(MAX_TINT_STEP, max(1, round(abs(hue_off) / DEGREES_PER_TINT)))
            # Too green (positive) means less green from the light.
            new_tint = max(-10, min(10, tint - size * settings.tint_sign * (1 if hue_off > 0 else -1)))
        new_fill = fill_level
        if not ratio_ok:
            # Shadows deeper than wanted (positive) need more fill.
            new_fill = min(100.0, max(MIN_BRIGHTNESS, fill_level * 2 ** (ratio_off * fill_gain)))
            if round(new_fill, 1) == round(fill_level, 1):
                fill_advice = fill_limit_advice(ratio_off)
        moves = (round(new_brightness, 1), new_tint) != (round(brightness, 1), tint)
        fill_moves = new_fill is not None and round(new_fill, 1) != round(fill_level, 1)
        if not moves and not fill_moves:
            break
        if moves:
            brightness, tint = round(new_brightness, 1), new_tint
            await light.set_white(kelvin, brightness, tint)
        if fill_moves:
            fill_level = round(new_fill, 1)
            await fill.set_white(kelvin, fill_level, 0)
        await sleep(settle)

    last = steps[-1]
    if not done and result_advice is None and abs(last.stops) > STOP_TOLERANCE:
        result_advice = f"Stopped after {len(steps)} readings, {last.stops:+.1f} stops off target."
    if not last.face:
        notes.append("no face found: metered the middle of the frame")
        if fill is not None:
            notes.append("fill left where it was: the lighting ratio needs a face")
    if tint_flipped:
        notes.append("tint ran the other way on this light; remembered for next time")
    result = {
        "done": done,
        "kelvin": kelvin,
        "light": {"brightness": brightness, "tint": tint},
        "target": settings.target,
        "profile": settings.profile,
        "steps": [_round(s) for s in steps],
        "advice": result_advice,
        "notes": notes,
        "tint_flipped": tint_flipped,
    }
    if fill is not None:
        result["fill"] = {
            "brightness": fill_level,
            "ratio_target": settings.fill_ratio,
            "ratio": None if last.contrast is None else round(last.contrast, 2),
            "advice": fill_advice,
        }
    return result


def fill_limit_advice(ratio_off: float) -> str:
    if ratio_off > 0:
        return (
            f"The fill is at full and the shadow side is still {ratio_off:.1f} stops deeper than wanted. "
            "Bring the fill closer, or bounce the key off a white card on the shadow side."
        )
    return (
        f"The fill is at its lowest and the face is still {-ratio_off:.1f} stops flatter than wanted. "
        "Turn the fill off or move it back; room light may be filling the shadows, so dim it."
    )

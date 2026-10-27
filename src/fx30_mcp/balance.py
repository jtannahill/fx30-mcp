"""Light the face to a target with the one light, leaving the camera alone.

For a talking head the camera settings are best set once: shutter at twice the
frame rate, ISO at a native base, aperture for the depth of field you want.
That leaves the light to set exposure. The loop reads a live-view frame, works
out how many stops the face is off, moves the light's brightness by that much,
and repeats. With the camera's white balance set to the light's colour
temperature, what is left of a colour cast is mostly green or magenta, which
the light's tint corrects.
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


def _round(step: Step) -> dict[str, Any]:
    out = asdict(step)
    for key in ("brightness", "level", "stops", "clipped"):
        out[key] = round(out[key], 3)
    if step.hue_off is not None:
        out["hue_off"] = round(step.hue_off, 1)
    return out


def advice(stops: float, profile: str) -> str:
    if stops > 0:
        iso = (
            "In S-Log3 the FX30's second native ISO, 2500, is about 1.6 stops above 800. "
            if profile == "slog3" else "Raise ISO, "
        )
        return (
            f"The light is at full and the face is still {stops:.1f} stops under. {iso}"
            "Open the aperture, or bring the light closer: half the distance is two stops more."
        )
    return (
        f"The light is at its lowest and the face is still {-stops:.1f} stops over. "
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
) -> dict[str, Any]:
    """Bring the face to settings.target. Returns what it did and where it ended."""

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
    await sleep(settle)

    steps: list[Step] = []
    gain = 1.0
    done = False
    result_advice = None
    tint_flipped = False
    for _ in range(max_steps):
        reading = await asyncio.to_thread(meter.read, await camera.frame())
        stops = meter.stops_off(reading.level, settings.target, settings.profile)
        if reading.clipped > CLIP_LIMIT:
            stops = min(stops, -1 / 3)
        # Skin colour means something only on skin.
        hue_off = reading.hue_off(settings.skin_hue) if fix_tint and reading.face else None
        step = Step(brightness, tint, reading.face, reading.level, stops, hue_off, reading.clipped)

        if steps and fix_tint and steps[-1].tint != tint and hue_off is not None and steps[-1].hue_off is not None:
            before = steps[-1].hue_off
            if before * hue_off > 0 and abs(hue_off) > abs(before) + 2:
                # The tint step made the cast worse: this light counts tint the other way.
                settings.tint_sign = -settings.tint_sign
                tint_flipped = True
        if steps and abs(stops) > STOP_TOLERANCE and steps[-1].stops * stops < 0:
            gain *= 0.5  # overshot: the light's dimming curve is not linear here
        steps.append(step)

        exposure_ok = abs(stops) <= STOP_TOLERANCE
        tint_ok = hue_off is None or abs(hue_off) <= HUE_DEADBAND or not light.caps.tint
        if exposure_ok and tint_ok:
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
        if (round(new_brightness, 1), new_tint) == (round(brightness, 1), tint):
            break
        brightness, tint = round(new_brightness, 1), new_tint
        await light.set_white(kelvin, brightness, tint)
        await sleep(settle)

    last = steps[-1]
    if not done and result_advice is None and abs(last.stops) > STOP_TOLERANCE:
        result_advice = f"Stopped after {len(steps)} readings, {last.stops:+.1f} stops off target."
    if not last.face:
        notes.append("no face found: metered the middle of the frame")
    if tint_flipped:
        notes.append("tint ran the other way on this light; remembered for next time")
    return {
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

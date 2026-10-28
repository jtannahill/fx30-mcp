"""Coach a talking-head shot, one instruction at a time.

start() looks at the room with the lights off and plans the shot. run() then
walks the steps: the camera settings on the card (ISO, shutter and aperture are
the owner's to set, so the coach asks and checks the readback), where the key
goes and which side it lights, the fill when there is one, then the existing
balance loop. It ends at Ready. Every change of instruction or light goes out
through publish as one state dict, for the page.

The coach never closes the camera: the server holds it, and a process that
drops the FX30 uncleanly leaves it needing a power cycle.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import math
from typing import Any, Awaitable, Callable

from . import config, meter
from .balance import MIN_BRIGHTNESS, STOP_TOLERANCE, balance
from .camera import WEDGED_HINT, CameraBase, CameraError
from .config import Settings
from .plan import (
    MIN_KEY_FT,
    UNEVEN_STOPS,
    Plan,
    closer,
    exposure_change,
    exposure_of,
    face_light,
    fmt_ft,
    make_plan,
    ratio_light,
    window_light,
)
from .survey import Survey, key_check, survey

CARD_TOLERANCE = 1 / 3  # stops the window may sit off its range before the card is redone
MAX_RECOMPUTES = 4
LABELS = {"aperture": "Aperture", "shutter": "Shutter", "iso": "ISO"}  # asked in this order
ASSUMED = {"iso": "800", "shutter": "1/60", "aperture": "f/2.8"}  # planned from when the camera cannot say
CANT_HOLD = "A sheer curtain or ND film on the glass would. Or open up and let the window sit brighter."
HOLD_STILL = ("Hold still", "Setting the lights. About 5 seconds.")


class LightError(Exception):
    """A light stopped answering: amaran Desktop closed, Bluetooth dropped, a bad reply."""


class _Light:
    """A light whose failures come out as LightError, so they are told apart from the coach's own."""

    def __init__(self, light: Any) -> None:
        self._light = light

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._light, name)
        if not inspect.iscoroutinefunction(value):
            return value

        async def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return await value(*args, **kwargs)
            except CameraError:
                raise
            except Exception as err:
                raise LightError(str(err) or type(err).__name__) from err

        return call


class _Stopped(Exception):
    pass


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


def _canonical(setting: str, value: Any) -> str | None:
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


def _numeric(readback: dict[str, str | None]) -> bool:
    """Every card setting was read and is a number, so it can go into a plan or an exposure."""

    return all(readback[s] is not None and _number(s, readback[s]) is not None for s in LABELS)


class Coach:
    def __init__(
        self,
        camera: CameraBase,
        key: Any,
        fill: Any | None,
        settings: Settings,
        publish: Callable[[dict], None],
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        settle: float = 0.7,
        override: str | None = None,
    ) -> None:
        self.camera = camera
        self.key = _Light(key)
        self.fill = None if fill is None else _Light(fill)
        self.settings = settings
        self.publish = publish
        self.sleep = sleep
        self.settle = settle
        self.override = override
        self.state: dict = {}
        self.plan: Plan | None = None
        self.latest_frame: bytes | None = None
        self.running = False
        self._survey: Survey | None = None
        self._truthful = False
        self._box: meter.Box | None = None
        self._window: meter.Window | None = None
        self._lights: dict[str, str | None] = {"face": "red", "window": None, "ratio": None}
        self._begun = asyncio.Event()
        self._in_place = asyncio.Event()
        self._stopping = False

    @property
    def curve(self) -> str:
        """How live view encodes brightness."""

        return "slog3" if self.settings.profile == "slog3" else "rec709"

    def begin(self) -> None:
        self._begun.set()

    def light_in_place(self) -> None:
        self._in_place.set()

    def stop(self) -> None:
        self._stopping = True
        self._begun.set()
        self._in_place.set()

    async def start(self) -> Plan | None:
        """Survey with the lights off and plan; None, asking them to sit in the shot, without a face."""

        found = await survey(self.camera, self.key, self.fill, self.curve, self.settle, self.sleep)
        readback = {s: _canonical(s, found.camera.get(s)) for s in LABELS}
        # A setting the camera cannot report is planned from the base; the camera step asks for it anyway.
        plannable = {s: v if _number(s, v) is not None else ASSUMED[s] for s, v in readback.items()}
        found = dataclasses.replace(found, camera={**found.camera, **plannable})
        self._survey = found
        self.latest_frame = found.frame
        self._truthful = self.curve == "slog3" or (found.info.get("gamma") == "Rec709" and self.curve == "rec709")
        self._judge(found.reading, found.window)
        if not found.reading.face:
            self.plan = None
            self._show("camera", "Sit in the shot", "The coach needs your face in the frame to plan.", screen="plan")
            return None
        self._box = found.reading.box
        self.plan = make_plan(found, self.settings, self.override)
        self._show("camera", "Tap Start when you're in place", None, screen="plan")
        return self.plan

    async def run(self) -> None:
        """Wait for begin(), then camera, key, fill, balance, Ready. Stops on stop() or a camera or light error."""

        if self.plan is None:
            return
        self.running = True
        try:
            await self._begun.wait()
            self._check()
            await self._camera_step()
            await self._place_key()
            if self.fill is not None:
                await self._wait("place_key", f"Fill on camera-{self.plan.fill_side}", "Next to the lens. Tap when it's there.")
            while not await self._balance_step():
                # The window clips: darken the card. When it is as dark as it goes, the glass needs taming.
                card = self.plan.camera
                await self._camera_step()
                if self.plan.camera == card:
                    await self._wait("balance", "The key can't hold this window right now", CANT_HOLD, self._window_holds)
            self._check()
            self._show("ready", "Ready to record", None)
        except _Stopped:
            self._show("stopped", "Coach stopped", None)
        except CameraError:
            self._show("stopped", "Camera stopped answering", WEDGED_HINT)
        except LightError as err:
            self._show("stopped", "A light stopped answering", str(err))
        except Exception as err:
            self._show("stopped", "Coach stopped", f"Something went wrong: {type(err).__name__}.")
            raise
        finally:
            self.running = False

    def _check(self) -> None:
        if self._stopping:
            raise _Stopped

    def _show(self, step: str, instruction: str, detail: str | None, screen: str = "coach") -> None:
        plan = self.plan.to_json() if self.plan else None
        if plan is not None:
            plan["has_fill"] = self.fill is not None
        state = {
            "screen": screen,
            "step": step,
            "instruction": instruction,
            "detail": detail,
            "lights": dict(self._lights),
            "plan": plan,
        }
        if state != self.state:
            self.state = state
            self.publish(state)

    def _judge(self, reading: meter.Reading, window: meter.Window | None) -> None:
        self._window = window
        contrast = reading.contrast(self.curve) if self.fill is not None and reading.face else None
        self._lights = {
            "face": face_light(self._under(reading)),
            "window": window_light(window, reading.lit, self.curve, self._truthful),
            "ratio": ratio_light(contrast, self.settings.fill_ratio),
        }

    async def _look(self) -> meter.Reading:
        """Read a frame and update the lights from it."""

        frame = await self.camera.frame()
        self.latest_frame = frame
        reading = await asyncio.to_thread(meter.read, frame)
        if reading.face:
            self._box = reading.box
        window = await asyncio.to_thread(meter.find_window, frame, reading.box, self.curve)
        self._judge(reading, window)
        return reading

    def _under(self, reading: meter.Reading) -> float:
        """Stops the face sits under target; negative when over."""

        return meter.stops_off(reading.lit, self.settings.target, self.curve)

    def _window_holds(self, _reading: meter.Reading) -> bool:
        return self._lights["window"] != "red"

    async def _readback(self) -> dict[str, str | None]:
        raw = await self.camera.settings()
        return {s: _canonical(s, raw.get(s)) for s in LABELS}

    async def _wait(
        self,
        step: str,
        instruction: str,
        detail: str | None,
        done: Callable[[meter.Reading], bool] | None = None,
    ) -> meter.Reading | None:
        """Show an instruction and keep the lights live until they tap, or done() says the frame is there."""

        self._in_place.clear()
        self._show(step, instruction, detail)
        reading = None
        while not self._in_place.is_set():
            self._check()
            reading = await self._look()
            self._show(step, instruction, detail)
            if done is not None and done(reading):
                break
            await self.sleep(self.settle)
        self._check()
        return reading

    async def _camera_step(self) -> None:
        """Ask for each card setting until the readback matches; redo the card while the window is off.

        A setting the camera does not report is asked for once and taken as set when they tap."""

        recomputes = 0
        tapped: dict[str, str] = {}
        while True:
            self._check()
            current = await self._readback()
            await self._look()
            card = self.plan.camera
            wanted = {s: _canonical(s, getattr(card, s)) for s in LABELS}
            wrong = next(
                (s for s in LABELS if current[s] != wanted[s] and not (current[s] is None and tapped.get(s) == wanted[s])),
                None,
            )
            if wrong is not None and current[wrong] is None:
                await self._wait("camera", f"Set {wanted[wrong]} on the camera", "Tap when it's set.")
                tapped[wrong] = wanted[wrong]
                continue
            if wrong is not None:
                detail = LABELS[wrong] if card.note is None else f"{LABELS[wrong]}. {card.note}"
                self._show("camera", f"Set {wanted[wrong]} on the camera", detail)
                await self.sleep(self.settle)
                continue
            change = exposure_change(self._window, self.settings.target, self.curve, self.settings.window_range)
            if abs(change) <= CARD_TOLERANCE or recomputes >= MAX_RECOMPUTES:
                return
            recomputes += 1
            seen = {s: current[s] if current[s] is not None else wanted[s] for s in LABELS}
            self.plan = make_plan(
                dataclasses.replace(self._survey, camera={**self._survey.camera, **seen}, window=self._window),
                self.settings,
                self.override,
            )

    async def _place_key(self) -> None:
        geometry = self.plan.geometry
        side = geometry.side
        instruction = f"Key on camera-{side}"
        detail = (
            f"{fmt_ft(geometry.distance_ft)} from your face, {fmt_ft(geometry.height_ft)} above your eyes, "
            "aimed down. Tap when it's there."
        )
        while True:
            await self._wait("place_key", instruction, detail)
            check = await key_check(self.camera, self.key, self._box, self.curve, self.settle, self.sleep)
            near, far = check.sides if side == "left" else reversed(check.sides)
            if check.lit is None:
                instruction, detail = "Turn the key on", "The key isn't reaching your face."
            elif far > near * 2**UNEVEN_STOPS:
                instruction, detail = "Key is on the wrong side", f"Move it to camera-{side}."
            else:
                return  # a soft key lights both cheeks nearly alike; only a clearly brighter far side is wrong

    async def _move_key(self, instruction: str, under: float, done: Callable[[meter.Reading], bool]) -> None:
        """Ask for the key nearer or farther, wait until the face shows it, and note where it went."""

        d0 = self.plan.geometry.distance_ft
        reading = await self._wait("balance", instruction, f"{fmt_ft(d0)} to {fmt_ft(closer(d0, under))}", done)
        if reading is not None and reading.face:
            # Half the distance is two stops: read how far it moved from how much the face changed.
            self.plan.geometry.distance_ft = closer(d0, under - self._under(reading))

    async def _balance_step(self) -> bool:
        """Balance once and judge it: True at Ready, False when the window clips and the card must darken.

        A limit balance cannot get past becomes one instruction; once the frame shows it was followed,
        or they tap, it balances again."""

        while True:
            self._show("balance", *HOLD_STILL)
            result = await balance(
                self.camera,
                self.key,
                self.settings,
                kelvin=self.plan.kelvin,
                settle=self.settle,
                sleep=self.sleep,
                fill=self.fill,
                target=self.settings.target,
            )
            self._check()
            await self._save_key_output(result)
            await self._look()
            last = result["steps"][-1]
            under, key = last["stops"], result["light"]["brightness"]
            fill = result.get("fill")

            def no_longer_under(reading: meter.Reading) -> bool:
                return self._under(reading) <= STOP_TOLERANCE

            def no_longer_over(reading: meter.Reading) -> bool:
                return self._under(reading) >= -STOP_TOLERANCE

            def ratio_green(_reading: meter.Reading | None) -> bool:
                return self._lights["ratio"] == "green"

            if last["face"] and key >= 100 and under > STOP_TOLERANCE:
                if closer(self.plan.geometry.distance_ft, under) < MIN_KEY_FT:
                    await self._wait("balance", "The key can't hold this window right now", CANT_HOLD, no_longer_under)
                else:
                    await self._move_key("Key closer", under, no_longer_under)
            elif last["face"] and key <= MIN_BRIGHTNESS and under < -STOP_TOLERANCE:
                await self._move_key("Key farther", under, no_longer_over)
            elif fill is not None and fill["advice"] is not None and fill["ratio"] is not None and not ratio_green(None):
                if fill["ratio"] > fill["ratio_target"]:
                    await self._wait("balance", "Fill closer", "Or bounce the key off a white card on the shadow side.", ratio_green)
                else:
                    await self._wait("balance", "Fill farther", "Or turn the fill off.", ratio_green)
            elif self._lights["window"] == "red":
                return False
            elif self._lights["face"] == "green":
                return True
            # Otherwise balance ran out of readings short of target: go again.

    async def _save_key_output(self, result: dict[str, Any]) -> None:
        """Remember what the key does at full from where it is, for the next plan's key percent."""

        last = result["steps"][-1]
        if not last["face"]:
            return
        current = await self._readback()
        if not _numeric(current):
            return
        self._check()
        brightness = result["light"]["brightness"]
        self.settings.key_output = {
            "modifier": self.settings.key_modifier,
            "distance_ft": self.plan.geometry.distance_ft,
            "stops_at_full": round(-last["stops"] + math.log2(100 / brightness), 3),
            "exposure": round(exposure_of(current["iso"], current["shutter"], current["aperture"]), 3),
        }
        saved = config.load()  # only this field, so settings changed during the session survive
        saved.key_output = self.settings.key_output
        config.save(saved)

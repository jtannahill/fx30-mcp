"""The director: one loop that sets up the shot and watches it through every take.

Each tick reads the rig and one frame, turns them into a Scene, asks decide what to do and has the Rig
do it. Setup brings the shot to Ready with no taps; the director runs the key check and the catchlight
check itself, the experiments decide cannot make. When the camera starts recording the loop switches to
take mode and holds what recording started with; when it stops, the take's report goes to the page and
by text, and setup watches for the next take. The run ends on stop(), after IDLE_END_SECONDS without
recording, or when the camera stops answering a read; a run that ends mid-take still reports the take.
A camera write the camera refuses does not end it: setup asks for the value by hand, a take stops
stepping the camera. Every change of what the page should show goes out through publish; texts go out
in the background, so a slow send never holds up the loop.
With camera_moves off (balance_shot) setup runs decide's balance mode: the light alone, no key check.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import time
from typing import Any, Awaitable, Callable

from . import config, meter
from .camera import CameraError
from .config import Settings
from .decide import (
    CONNECTING,
    LABELS,
    NO_FACE,
    Ask,
    Goal,
    History,
    Moves,
    RigState,
    _number,
    by_hand,
    canonical,
    decide,
    key_placed,
    start_take,
)
from .notify import Texts, send_later
from .plan import Plan, _widest_offered, exposure_of, face_light, key_geometry, make_plan, window_light
from .rig import LightError, LightUnreachable, Rig
from .report import Take, capital, report_text
from .scene import Scene, read_scene
from .survey import key_check, survey

__all__ = ["Director", "IDLE_END_SECONDS", "TAKE_INTERVAL", "report_text"]

TAKE_INTERVAL = 1.0
IDLE_END_SECONDS = 1800.0
WORKING_PERCENT = 50  # the key's level for the key check when no percent was predicted
LOW_BATTERY = 15  # percent
MOVED_STOPS = 1 / 3  # the face or its cheeks changing this much under a key ask: the key was moved
CARD = ("iso", "shutter", "aperture")
ASSUMED = {"iso": "800", "shutter": "1/60", "aperture": "f/2.8"}  # planned from when the camera cannot say
LINK_HOLD_SECONDS = IDLE_END_SECONDS + 60  # the light's Bluetooth link stays up through a whole run
SETTING_LIGHT = ("Hold still", "Setting the light.")
RETRYING = (
    "The light isn't answering",
    "Retrying on its own. If the amaran or Sidus Link app is open on your phone, close it.",
)
RECORDING_NOTE = "The camera is recording; balance_shot leaves the light alone during a take."
# balance_shot returns once, so it cannot say it is retrying: it says what to do and to call it again.
BALANCE_UNREACHABLE = (
    "The light isn't answering. If the amaran or Sidus Link app is open on your phone, close it "
    "or turn off the phone's Bluetooth, then call balance_shot again."
)


class _Stopped(Exception):
    pass


def _exposure(camera: dict[str, Any]) -> float | None:
    """Stops of exposure from the camera's readback, which comes in several forms ("ISO 800", '1/60"', "6.3")."""

    try:
        return exposure_of(*(canonical(s, camera[s]) for s in ("iso", "shutter", "aperture")))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


class Director:
    def __init__(
        self,
        rig: Rig,
        settings: Settings,
        publish: Callable[[dict], None],
        texts: Texts,
        override: str | None = None,
        camera_moves: bool = True,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        settle: float = 0.7,
        take_interval: float = TAKE_INTERVAL,
    ) -> None:
        self.rig = rig
        self.settings = settings
        self.publish = publish
        self.texts = texts
        self.override = override
        self.camera_moves = camera_moves
        self.sleep = sleep
        self.clock = clock
        self.take_interval = take_interval
        self._settle = settle
        self.profile = "slog3" if settings.profile == "slog3" else "rec709"
        self.plan: Plan | None = None
        self.goal: Goal | None = None
        self.latest_frame: bytes | None = None
        self.history = History()
        self.mode = "setup"  # "setup", "take", or "stopped" once the run has ended
        self.state: dict = {}
        self.last_report: dict[str, Any] | None = None
        self.running = False
        self._stopping = False
        self._held = 0
        self._truthful = False
        self._lights: dict[str, str | None] = {"face": "red", "window": None, "ratio": None}
        self._quiet = 0  # setup ticks in a row that wanted nothing
        self._card_right = False
        self._mark: tuple[float, float] | None = None  # face level and cheek balance when a key ask began
        self._take: Take | None = None
        self._takes = 0
        self._showing_report = False
        self._warned: set[str] = set()
        self._light_problem: tuple[str, str | None] | None = None
        self._last_ask: Ask | None = None
        self._idle_since = 0.0
        self._before_connecting: dict | None = None
        self._sending: set[asyncio.Task] = set()

    def stop(self) -> None:
        self._stopping = True

    async def start(self) -> Plan | None:
        """Survey with the lights off and plan; None, asking them to sit in the shot, without a face."""

        self._hold(True)
        try:
            found = await survey(self.rig.camera, self.rig.key, None, self.profile, self._settle, self.sleep)
        finally:
            self._hold(False)
        readback = {s: canonical(s, found.camera.get(s)) for s in CARD}
        plannable = {s: v if v is not None and _number(s, v) is not None else ASSUMED[s] for s, v in readback.items()}
        found = dataclasses.replace(found, camera={**found.camera, **plannable})
        self.latest_frame = found.frame
        self._truthful = self.profile == "slog3" or (found.info.get("gamma") == "Rec709" and self.profile == "rec709")
        self._judge(found.reading.face, found.reading.lit, found.window, self.settings.target)
        if not found.reading.face:
            self.plan = None
            self._show("camera", *NO_FACE, screen="plan")
            return None
        self.plan = make_plan(found, self.settings, self.override)
        self.goal = Goal(
            face_level=self.settings.target,
            skin_hue=self.settings.skin_hue,
            window_range=self.settings.window_range,
            profile=self.profile,
            tint_sign=self.settings.tint_sign,
            kelvin=self.plan.kelvin,
            frame_rate=found.info.get("frame_rate") or self.settings.frame_rate or 29.97,
            widest=self.settings.widest_aperture or _widest_offered(found.info.get("apertures") or []) or 2.8,
            geometry=self.plan.geometry,
        )
        self._show("camera", "Here's the plan", "The coach sets the camera and the light next.", screen="plan")
        return self.plan

    async def run(self) -> None:
        """Setup and takes until stop(), IDLE_END_SECONDS without recording, or a camera read failing.
        However it ends, a take still open is reported first, before anything cancellation could cut."""

        if self.plan is None:
            return
        self.running = True
        self._hold(True)
        link = getattr(self.rig.key, "link", None)
        hold_was = getattr(link, "idle_timeout", None)
        if hold_was is not None:
            link.idle_timeout = LINK_HOLD_SECONDS  # a reconnect mid-take would stall the loop for a minute
        self._idle_since = self.clock()
        try:
            while True:
                self._check()
                await self._tick()
                if self.mode != "take" and self.clock() - self._idle_since >= IDLE_END_SECONDS:
                    self._show("stopped", "Coach stopped", "Nothing was recorded for 30 minutes.")
                    return
                await self.sleep(self.take_interval if self.mode == "take" else self._settle)
        except _Stopped:
            report = self._close_take()
            self._show("stopped", "Coach stopped", None, screen="coach" if report is None else "report")
        except CameraError as err:
            self._close_take()
            # The camera layer already says what to do: a timeout carries the power-cycle hint.
            self._fail("Camera stopped answering", str(err))
        except asyncio.CancelledError:
            self._close_take()
            raise
        except Exception as err:
            self._close_take()
            self._fail("Coach stopped", f"Something went wrong: {type(err).__name__}.")
            raise
        finally:
            if hold_was is not None:
                link.idle_timeout = hold_was
            self.running = False
            self.mode = "stopped"
            self._hold(False)

    async def settle(self, max_ticks: int, kelvin: int | None = None, fix_tint: bool = True) -> dict[str, Any]:
        """Setup until Ready or max_ticks, for balance_shot: whether it got there, the key, and any ask.

        With camera_moves off there is no survey (it switches the key off): the goal comes from the
        settings, at kelvin, or the key's own colour temperature. With fix_tint off the key's tint is
        left as it is. Nothing is written while recording."""

        notes: list[str] = []
        done = False
        self._hold(True)
        try:
            if (await self.rig.camera.settings()).get("recording"):
                notes.append(RECORDING_NOTE)
            elif self.goal is None and not self.camera_moves:
                await self.rig.state()  # reads the key when its state is not known yet, for its kelvin
                if self.rig.light_error is not None:
                    self._light_failed(self.rig.light_error)
                else:
                    self.goal = self._light_goal(kelvin)
            elif self.goal is None and await self.start() is None:
                self._last_ask = Ask(*NO_FACE)
            if self.goal is not None and self.goal.fix_tint != fix_tint:
                self.goal = dataclasses.replace(self.goal, fix_tint=fix_tint)
            # One light failure ends it: each try can be a two-minute Bluetooth connect, holding _busy.
            while self.goal is not None and not notes and max_ticks > 0 and not self._stopping:
                max_ticks -= 1
                scene, rig, now = await self._read()
                if scene.camera.get("recording"):
                    notes.append(RECORDING_NOTE)
                    break
                if self.rig.light_error is not None:
                    self._light_failed(self.rig.light_error)
                    break
                await self._setup_tick(scene, rig, now)
                if self._light_problem is not None:
                    break
                if self.state.get("step") == "ready":
                    done = True
                    break
                await self.sleep(self._settle)
        finally:
            self._hold(False)
        if self._light_problem == RETRYING:
            notes.append(BALANCE_UNREACHABLE)
        elif self._light_problem is not None:
            instruction, detail = self._light_problem
            notes.append(f"{instruction}: {detail.rstrip('.')}." if detail else f"{instruction}.")
        light, ask = self.rig.key.state, self._last_ask
        words = [] if ask is None else [p if p.endswith(".") else p + "." for p in (ask.instruction, ask.detail) if p]
        return {
            "done": done,
            "light": {"brightness": light.brightness, "kelvin": light.kelvin, "tint": light.tint},
            "advice": " ".join(words) or None,
            "notes": notes,
        }

    def _light_goal(self, kelvin: int | None) -> Goal:
        """The goal for balancing the light alone, from the settings: no plan, no survey."""

        settings = self.settings
        side = self.override or "left"
        return Goal(
            face_level=settings.target,
            skin_hue=settings.skin_hue,
            window_range=settings.window_range,
            profile=self.profile,
            tint_sign=settings.tint_sign,
            kelvin=kelvin or self.rig.key.state.kelvin or 5600,
            frame_rate=settings.frame_rate or 29.97,
            widest=settings.widest_aperture or 2.8,
            geometry=key_geometry(side, settings.key_modifier, settings.key_size_in),
        )

    def status(self) -> dict[str, Any]:
        take = None
        if self._take is not None:
            so_far = self._take.report(self.clock(), self.history.hands_off)
            take = {"seconds": so_far["seconds"], "report": so_far}
        return {
            "mode": self.mode,
            "step": self.state.get("step"),
            "instruction": self.state.get("instruction"),
            "detail": self.state.get("detail"),
            "lights": dict(self._lights),
            "take": take,
            "last_report": self.last_report,
        }

    def light_slow(self) -> None:
        """A light call is taking a while: most often the Bluetooth connect, which can run a minute."""

        self._before_connecting = self.state
        self._show(self.state.get("step", "camera"), *CONNECTING, screen=self.state.get("screen", "coach"))

    def light_back(self) -> None:
        before, self._before_connecting = self._before_connecting, None
        if before:
            self._show(before["step"], before["instruction"], before["detail"], screen=before["screen"])

    # The loop.

    def _check(self) -> None:
        if self._stopping:
            raise _Stopped

    def _hold(self, on: bool) -> None:
        """Hold the camera open while any of start, run or settle is going."""

        self._held += 1 if on else -1
        if (on and self._held == 1) or (not on and self._held == 0):
            self.rig.camera.hold(on)

    async def _read(self) -> tuple[Scene, RigState, float]:
        now = self.clock()
        rig = await self.rig.state()
        self._warn_camera(rig.camera)
        frame = await self.rig.camera.frame()
        self.latest_frame = frame
        scene = await asyncio.to_thread(read_scene, frame, rig.camera, self.profile)
        return scene, rig, now

    async def _tick(self) -> None:
        scene, rig, now = await self._read()
        recording = bool(scene.camera.get("recording"))
        if recording and self.mode != "take":
            self._begin_take(scene, now)
        elif not recording and self.mode == "take":
            self._end_take(scene, now)
        if self.mode == "take":
            await self._take_tick(scene, rig, now)
        else:
            await self._setup_tick(scene, rig, now)

    async def _setup_tick(self, scene: Scene, rig: RigState, now: float) -> None:
        goal, history, light_only = self.goal, self.history, not self.camera_moves
        self._judge(scene.face, scene.level, scene.window, goal.face_level)
        moves = decide(scene, rig, goal, "balance" if light_only else "setup", history, now)
        history.last_tick = now
        self._last_ask = moves.ask
        asked_by_hand = by_hand(moves.ask)  # the camera refused a value: the card is not right yet
        if scene.face and moves.camera is None and not asked_by_hand:
            self._card_right = True
        try:
            if not light_only and await self._experiment(scene, rig, moves):
                self._quiet = 0
                return
            try:
                await self.rig.apply(moves, history)
            except CameraError:
                if moves.camera is None:
                    raise
                # Refused (not offered, read-only, a slow readback): from the next tick decide asks for it by hand.
                history.refused[moves.camera.setting] = moves.camera.value
                self._quiet = 0
                return
            if moves.light is not None:
                self._light_ok()
        except LightError as err:
            self._light_failed(err)
            self._quiet = 0
            self._show(self.state.get("step", "balance"), *self._light_problem, screen=self._screen(moves))
            return
        was_ready = self.state.get("step") == "ready"
        self._quiet = self._quiet + 1 if moves == Moves() else 0
        ready = self._quiet >= 2 and self._lights["face"] == "green" and self._lights["window"] != "red"
        if moves.camera is not None or asked_by_hand or not self._card_right:
            step = "camera"
        elif not light_only and not self._key_ok():
            step = "place_key"
        else:
            step = "ready" if ready else "balance"
        if moves.ask is not None:
            instruction, detail = moves.ask.instruction, moves.ask.detail
        elif moves.camera is not None:
            instruction, detail = self._camera_words(moves)
        elif ready:
            instruction, detail = "Ready to record", None
        else:
            instruction, detail = SETTING_LIGHT
        if ready and not was_ready and self.plan is not None:
            self._save_key_output(scene, rig)
        self._show(step, instruction, detail, screen=self._screen(moves))

    async def _experiment(self, scene: Scene, rig: RigState, moves: Moves) -> bool:
        """The checks decide cannot make itself; True when one ran, so this tick's frame is stale."""

        history = self.history
        if not scene.face or moves.camera is not None or by_hand(moves.ask):
            return False
        if history.key_check is None:
            if await self._recording_now():
                return True
            # The card is right: the key goes on at a working level, so a dim key is not taken for none.
            await self.rig.key.set_white(self.goal.kelvin, self.plan.key_percent or WORKING_PERCENT, rig.tint)
            await self.rig.key.set_power(True)
            await self._check_key(scene)
            return True
        if not key_placed(history.key_check, self.goal.geometry.side):
            balance = meter.stops_off(scene.sides[1], scene.sides[0], self.profile)
            if self._mark is None:
                self._mark = (scene.level, balance)
                return False
            level0, balance0 = self._mark
            moved = abs(meter.stops_off(level0, scene.level, self.profile)) > MOVED_STOPS
            if moved or abs(balance - balance0) > MOVED_STOPS:
                if not await self._recording_now():
                    await self._check_key(scene)
                return True
            return False
        if history.catchlight in ("raise", "lower"):
            # Asked to move the key for the eyes: the ask shows for a tick, then the catchlight is
            # watched until it sits right.
            if self._mark is None:
                self._mark = (scene.level, 0.0)
                return False
            if await self._recording_now():
                return True
            verdict = await asyncio.to_thread(meter.catchlight, scene.frame, history.key_check.off, self.profile)
            if verdict != history.catchlight:
                history.catchlight = verdict  # ok, unreadable, or now the other way
                self._mark = None
                return True
        return False

    async def _recording_now(self) -> bool:
        """A fresh look before an experiment: the tick read the settings before its frame (about 0.8 s on
        the camera), and a key check flashing the key must never land in the first second of a take."""

        return bool((await self.rig.camera.settings()).get("recording"))

    async def _check_key(self, scene: Scene) -> None:
        self._mark = None
        self._show("place_key", f"Key on camera-{self.goal.geometry.side}", "Checking which side it lights.")
        check = await key_check(self.rig.camera, self.rig.key, scene.box, self.profile, self._settle, self.sleep)
        self.history.key_check = check
        if key_placed(check, self.goal.geometry.side):
            self.history.catchlight = await asyncio.to_thread(meter.catchlight, check.on, check.off, self.profile)
        self._light_ok()

    def _key_ok(self) -> bool:
        check = self.history.key_check
        placed = check is not None and key_placed(check, self.goal.geometry.side)
        return placed and self.history.catchlight not in ("raise", "lower")

    def _begin_take(self, scene: Scene, now: float) -> None:
        start_take(scene, self.history)  # a new take: the last one's hands and camera values go
        if not scene.face:
            self.history.snapshot = None  # taken when the face is back: a take holds a face, not a wall
        self._takes += 1
        self._take = Take(self._takes, now, scene.camera.get("meter"), now, exposure=_exposure(scene.camera))
        self.mode = "take"
        self._mark = None
        self._quiet = 0
        self._showing_report = False

    async def _take_tick(self, scene: Scene, rig: RigState, now: float) -> None:
        history, take = self.history, self._take
        if history.snapshot is None and scene.face:
            start_take(scene, history)
            take.meter = scene.camera.get("meter")
            take.exposure = _exposure(scene.camera)
        level = history.snapshot[0] if history.snapshot is not None else self.goal.face_level
        self._judge(scene.face, scene.level, scene.window, level)
        moves = decide(scene, rig, self.goal, "take", history, now)
        history.last_tick = now
        lights = self._lights
        green, red = lights["face"] == "green", lights["window"] == "red"
        take.tick(now, scene.camera.get("meter"), green, red, moves.held, _exposure(scene.camera))
        applied = moves if self.camera_moves else dataclasses.replace(moves, camera=None)
        stepped = history.last_camera_step
        held = moves.held
        try:
            await self.rig.apply(applied, history)
            if applied.light is not None:
                self._light_ok()
        except CameraError:
            if applied.camera is None:
                raise
            # Refused: the light carries on and the camera is left alone for the rest of the take.
            held = history.camera_refused = f"camera refused {canonical(applied.camera.setting, applied.camera.value)}"
            take.held.setdefault(held, 0.0)
        except LightError as err:
            take.light_errors += 1
            self._light_failed(err)
        if history.last_camera_step != stepped:
            take.camera_steps += 1
        detail = self._light_problem[0] if self._light_problem else None
        if held is not None:
            detail = capital(held)
        self._show("take", "Recording", detail, screen="take")

    def _end_take(self, scene: Scene, now: float) -> None:
        self._take.meter_now = scene.camera.get("meter")
        self._take.exposure_now = _exposure(scene.camera)
        self._take.last = now
        self.last_report = self._finish_take()
        self.mode = "setup"
        self._idle_since = now
        self._quiet = 0
        self._showing_report = True
        self._show("ready", f"Take {self.last_report['take']} done", None, screen="report")
        self._text("report", self.last_report["text"])

    def _close_take(self) -> dict[str, Any] | None:
        """The run is ending with a take open: report it now, with no await a cancel could cut short."""

        if self._take is None:
            return None
        self.last_report = self._finish_take()
        self._text("report", self.last_report["text"])
        return self.last_report

    def _finish_take(self) -> dict[str, Any]:
        report = self._take.report(self._take.last, self.history.hands_off)
        self._take = None
        return {**report, "text": report_text(report)}

    # What the page shows, and what the text says.

    def _screen(self, moves: Moves) -> str:
        """The report stays up between takes until there is something to do (sitting back down aside)."""

        if self._showing_report and moves.ask is not None and moves.ask.instruction != NO_FACE[0]:
            self._showing_report = False
        return "report" if self._showing_report else "coach"

    def _camera_words(self, moves: Moves) -> tuple[str, str | None]:
        move = moves.camera
        if move.setting == "exposure_mode":
            return "Setting the camera to Manual", None
        if move.setting == "live_view_effect":
            return "Turning on Live View Setting Effect", "So live view shows the exposure."
        if move.setting == "kelvin":
            return f"Setting {move.value}K", LABELS["kelvin"]
        return f"Setting {canonical(move.setting, move.value)}", LABELS.get(move.setting)

    def _judge(self, face: bool, level: float, window: meter.Window | None, reference: float) -> None:
        self._lights = {
            "face": face_light(meter.stops_off(level, reference, self.profile)) if face else "red",
            "window": window_light(window, reference, self.profile, self._truthful),
            "ratio": None,
        }

    def _show(self, step: str, instruction: str, detail: str | None, screen: str = "coach") -> None:
        plan = self.plan.to_json() if self.plan else None
        if plan is not None:
            plan["has_fill"] = False
        state = {
            "screen": screen,
            "step": step,
            "instruction": instruction,
            "detail": detail,
            "lights": dict(self._lights),
            "plan": plan,
        }
        if screen == "report":
            state["report"] = self.last_report
        if state != self.state:
            self.state = state
            self.publish(state)

    def _text(self, kind: str, body: str) -> None:
        send_later(self.texts, kind, body, self._sending)

    def _warn_camera(self, camera: dict[str, Any]) -> None:
        battery = camera.get("battery")
        self._warn("battery", battery is not None and battery <= LOW_BATTERY, f"The camera battery is at {battery}%.")
        self._warn("overheat", camera.get("overheat") is True, "The camera is overheating and may stop recording.")
        self._warn("cards", camera.get("cards_ok") is False, "The camera has a problem with a memory card.")

    def _warn(self, kind: str, on: bool, body: str) -> None:
        """Text once when a condition starts; again only after it has cleared."""

        if not on:
            self._warned.discard(kind)
        elif kind not in self._warned:
            self._warned.add(kind)
            self._text(kind, body)

    def _light_failed(self, err: LightError) -> None:
        if isinstance(err, LightUnreachable):
            self._light_problem = RETRYING
            body = "The light isn't answering. If the amaran or Sidus Link app is open on your phone, close it."
        else:
            self._light_problem = ("A light stopped answering", str(err))
            body = f"The light stopped answering: {err}"
        self._warn("light", True, body + " The director keeps trying.")

    def _light_ok(self) -> None:
        self._light_problem = None
        self._warned.discard("light")

    def _fail(self, instruction: str, detail: str | None) -> None:
        self._show("stopped", instruction, detail, screen="coach")
        self._text("stopped", f"The shot coach stopped. {instruction}: {detail}" if detail else instruction)

    def _save_key_output(self, scene: Scene, rig: RigState) -> None:
        """Remember what the key does at full from where it is, for the next plan's key percent."""

        brightness, exposure = rig.brightness, _exposure(scene.camera)
        if not scene.face or not brightness or exposure is None:
            return
        stops = meter.stops_off(scene.level, self.goal.face_level, self.profile)
        self.settings.key_output = {
            "modifier": self.settings.key_modifier,
            "distance_ft": self.plan.geometry.distance_ft,
            "stops_at_full": round(-stops + math.log2(100 / brightness), 3),
            "exposure": round(exposure, 3),
        }
        saved = config.load()  # only this field, so settings changed during the session survive
        saved.key_output = self.settings.key_output
        config.save(saved)

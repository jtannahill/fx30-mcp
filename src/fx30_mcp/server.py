"""MCP tools for the FX30 and the amaran light, together."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
import functools
import json
import logging
import os
import time
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from . import config, meter
from .camera import CameraBase, CameraError, GPhotoCamera
from .config import Settings
from .decide import UNREACHABLE
from .director import Director
from .notify import Texts, send_later, texts_for_server
from .rig import LightError, LightUnreachable, Rig
from .web import Broadcaster, WebHandle, start_web

log = logging.getLogger(__name__)
BLUETOOTH_HOLD_SECONDS = 600.0
STOP_WAIT_SECONDS = 2.0  # stop_coach lets the run finish its tick (and report a take) before cancelling it

BACKEND = os.environ.get("FX30_BACKEND", "gphoto2").lower()

_camera: CameraBase | None = None
_sim_light: Any = None
_named_lights: dict[tuple[str, str], Any] = {}  # (name, backend) -> light
_busy = asyncio.Lock()
_director: Director | None = None  # the coach_shot run, from start until stop_coach; it holds _busy while running
_web: WebHandle | None = None
_run_task: asyncio.Task | None = None  # the survey while starting, then Director.run()
_holds_busy = False  # True while a coach_shot run (not a balance) holds _busy
_sending: set[asyncio.Task] = set()  # coach_shot's link text, sent in the background
FILL_NOTE = "The director lights with the key only for now; the fill is left as it is."
MODIFIERS = ("bare", "reflector", "softbox", "lantern", "umbrella")
SIZED = ("softbox", "lantern", "umbrella")
FRAME_RATES = (23.976, 60.0)
APERTURE_RANGE = (0.95, 22.0)


def camera() -> CameraBase:
    global _camera
    if _camera is None:
        if BACKEND == "sim":
            from .sim import SimCamera

            _camera = SimCamera(light(), gain=2.0)
        elif BACKEND == "gphoto2":
            _camera = GPhotoCamera()
        else:
            raise ValueError("FX30_BACKEND must be gphoto2 or sim")
    return _camera


def light() -> Any:
    global _sim_light
    if BACKEND == "sim":
        if _sim_light is None:
            from .sim import SimLight

            _sim_light = SimLight()
        return _sim_light
    from amaran_mcp.backends import current_light

    name = config.load().key_light
    return current_light() if name is None else named_light(name)


def fill_light() -> Any:
    """The fill light, or None when there is none."""

    if BACKEND == "sim":
        return None
    name = config.load().fill_light
    return None if name is None else named_light(name)


def amaran_lights() -> list[dict[str, Any]]:
    """The lights amaran Desktop has paired, with their keys."""

    from amaran_mcp.config import find_desktop_db, list_desktop_fixtures

    return list_desktop_fixtures(find_desktop_db())


def named_light(name: str) -> Any:
    """One of several amaran lights: through amaran Desktop when it runs, else Bluetooth."""

    from amaran_mcp.backends import choose
    from amaran_mcp.config import Light, load_light
    from amaran_mcp.desktop import DesktopLight
    from amaran_mcp.light import AmaranLight

    backend = choose()
    if (name, backend) not in _named_lights:
        fixtures = amaran_lights()
        fixture = next((f for f in fixtures if f["name"].lower() == name.lower()), None)
        if fixture is None:
            raise ValueError(f"amaran Desktop has no light named {name!r}; it has "
                             + ", ".join(repr(f["name"]) for f in fixtures))
        # Send as amaran-mcp's own mesh address, so the lights' message counters stay in step.
        light = Light(source_address=load_light().source_address, **fixture)
        if backend == "desktop":
            _named_lights[(name, backend)] = DesktopLight(light)
        else:
            bluetooth = AmaranLight(light)
            # Finding the light can take a minute or two (it advertises rarely), so hold
            # the link through the quiet stretches of a setup, not amaran-mcp's 30 s.
            bluetooth.link.idle_timeout = BLUETOOTH_HOLD_SECONDS
            _named_lights[(name, backend)] = bluetooth
    return _named_lights[(name, backend)]


async def take_over_light() -> None:
    """Music mode would fight the balancing loop, so it stops first."""

    if BACKEND == "sim":
        return
    from amaran_mcp.music import control

    if control.status():
        await asyncio.to_thread(control.stop)


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    try:
        yield
    finally:
        try:
            await _end_run()
        finally:
            if _camera is not None:
                await _camera.close()
            if BACKEND != "sim":
                from amaran_mcp.backends import close_all

                for lamp in _named_lights.values():
                    await lamp.close()
                await close_all()


mcp = MCPServer(
    "fx30",
    instructions=(
        "Controls the user's Sony FX30 over USB (PC Remote) and lights a one-person "
        "talking head with their amaran lights: a key light on the face and an optional "
        "fill on the shadow side (set_lights). The camera records to its own card. "
        "To set up a shot, call coach_shot: it plans the camera and light, sets the camera itself and "
        "shows the user where the key goes on a page on their phone, with no taps. It then watches "
        "every take until stop_coach (or 30 minutes without recording), holding the face and the "
        "window steady and texting the user a report after each take. start_recording and "
        "stop_recording only start and stop a take. shot_status reads what the run is doing and the "
        "last take's report. Without the coach: look (check framing), match_white (one colour "
        "temperature for camera and lights), balance_shot (brings the face to the target with the "
        "key light alone; it never touches the camera), start_recording. set_face_target saves a "
        "face brightness the user likes."
    ),
    lifespan=lifespan,
)


def _errors() -> tuple[type[Exception], ...]:
    errors: list[type[Exception]] = [CameraError, ValueError]
    if BACKEND != "sim":
        from amaran_mcp.config import ConfigError
        from amaran_mcp.desktop import DesktopError
        from amaran_mcp.link import LinkError

        errors += [ConfigError, DesktopError, LinkError]
    return tuple(errors)


def tool(**options: Any):
    """Register a tool whose expected failures reach the model as readable errors."""

    def register(fn):
        @functools.wraps(fn)
        async def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except _errors() as err:
                raise ToolError(str(err)) from err

        return mcp.tool(**options)(run)

    return register


READS = ToolAnnotations(readOnlyHint=True)
CHANGES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
Kelvin = Annotated[int, Field(ge=2000, le=10000, description="Colour temperature, e.g. 3200 tungsten, 5600 daylight")]


@tool(annotations=READS)
async def get_camera() -> dict[str, Any]:
    """Read the camera's ISO, shutter, aperture, white balance and whether it is recording."""

    cam = camera()
    settings = await cam.settings()
    return {"camera": cam.name, "backend": cam.backend, **settings, "target": config.load().__dict__}


@tool(annotations=CHANGES)
async def set_camera(
    iso: Annotated[str | None, Field(description="e.g. 800 or 2500 (the FX30's native bases in S-Log3)")] = None,
    shutter: Annotated[str | None, Field(description="e.g. 1/50 for 24p or 25p, 1/60 for 30p")] = None,
    aperture: Annotated[str | None, Field(description="f-number, e.g. 2.8")] = None,
) -> dict[str, str]:
    """Change ISO, shutter or aperture; each is read back from the camera before it is reported.
    With the camera in M (coach_shot sets it) all three can be changed over USB; in Movie F Mode
    shutter and ISO stay on the camera's own controls. Names must match what the camera offers;
    an error lists them."""

    cam = camera()
    done = {}
    for setting, value in (("iso", iso), ("shutter", shutter), ("aperture", aperture)):
        if value is not None:
            done[setting] = await cam.set(setting, value)
    if not done:
        raise ToolError("give at least one of iso, shutter, aperture")
    return done


@tool(annotations=READS)
async def look() -> list[Image | TextContent]:
    """Grab a live-view frame: shows it, and meters the face against the saved target."""

    jpeg = await camera().frame()
    reading = await asyncio.to_thread(meter.read, jpeg)
    settings = config.load()
    report = reading.summary()
    report["stops_to_target"] = round(meter.stops_off(reading.lit, settings.target, settings.profile), 2)
    off = reading.hue_off(settings.skin_hue) if reading.face else None
    report["hue_off"] = None if off is None else round(off, 1)
    return [Image(data=jpeg, format="jpeg"), TextContent(type="text", text=json.dumps(report))]


@tool(annotations=CHANGES)
async def match_white(kelvin: Kelvin) -> str:
    """Set the key and fill lights to white at this colour temperature and the camera's
    white balance to match."""

    await take_over_light()
    lamps = [light()] + ([fill] if (fill := fill_light()) is not None else [])
    for lamp in lamps:
        await lamp.set_white(kelvin, None, 0)
    which = "key and fill lights" if len(lamps) > 1 else "light"
    try:
        await camera().set_kelvin(kelvin)
    except CameraError as err:
        return f"{which} at {kelvin}K; camera white balance not set ({err}), set it to {kelvin}K by hand"
    return f"{which} and camera all at {kelvin}K"


@tool(annotations=CHANGES)
async def balance_shot(
    kelvin: Kelvin | None = None,
    fix_tint: Annotated[bool, Field(description="Also take out a green or magenta cast with the light's tint")] = True,
    max_steps: Annotated[int, Field(ge=1, le=20)] = 8,
) -> dict[str, Any]:
    """Light the face to the saved target with the key light alone: steps its brightness until
    the face is within a sixth of a stop (and, with fix_tint, its tint until the skin sits on the
    skin line). kelvin sets the key's colour temperature; match_white sets the camera's to match.
    Never touches the camera, and leaves the light alone while the camera records. Returns the
    key's settings and, as advice, what to change if the light runs out of range."""

    if _run_task is not None and not _run_task.done():
        raise ToolError("the shot coach is running; stop_coach first")
    if _busy.locked():
        raise ToolError("a balance or the shot coach is already running")
    async with _busy:
        await take_over_light()
        director = _new_director(config.load(), lambda _state: None, _NoTexts(), camera_moves=False)
        result = await director.settle(max_steps, kelvin, fix_tint)
        if director.settings.fill_light is not None:
            result["notes"].append(FILL_NOTE)
        return result


@tool(annotations=CHANGES)
async def set_face_target(
    level: Annotated[float | None, Field(ge=0.05, le=0.95, description="Face brightness 0-1 as live view shows it; leave out to use the current frame")] = None,
    profile: Annotated[str | None, Field(description="rec709, or slog3 when live view shows the log picture (Gamma Display Assist off)")] = None,
) -> dict[str, Any]:
    """Save the face brightness (and skin colour) balance_shot aims for. With no level,
    takes them from the current frame: light it how you like, then call this."""

    settings = config.load()
    skipped = None
    if profile is not None:
        if profile not in meter.PROFILES:
            raise ToolError(f"profile must be one of {', '.join(meter.PROFILES)}")
        settings.profile = profile
    if level is None:
        jpeg = await camera().frame()
        reading = await asyncio.to_thread(meter.read, jpeg)
        if not reading.face:
            raise ToolError("no face in frame; sit in the shot first, or give a level")
        window = await asyncio.to_thread(meter.find_window, jpeg, reading.box, settings.profile)
        if window is not None:
            over = meter.stops_off(reading.lit, window.level, settings.profile)
            if window.clipped > 0.02:
                skipped = "window_range not saved: the window clips in this frame"
            else:
                settings.window_range = [round(over - 1, 2), round(over + 1, 2)]
        settings.face_level = round(reading.lit, 3)
        if reading.hue is not None:
            settings.skin_hue = round(reading.hue, 1)
    else:
        settings.face_level = level
    config.save(settings)
    return settings.__dict__ if skipped is None else {**settings.__dict__, "note": skipped}


@tool(annotations=CHANGES)
async def set_lights(
    key: Annotated[str | None, Field(description="The key light's amaran name, e.g. 'amaran 100x S #1'")] = None,
    fill: Annotated[str | None, Field(description="The fill light's amaran name, or 'none' for no fill")] = None,
    fill_ratio: Annotated[float | None, Field(ge=0.25, le=4, description="Stops from the lit to the shadow side: 1 is 2:1 (classic interview), 2 is 4:1 (moodier)")] = None,
) -> dict[str, Any]:
    """Choose the key light (on the face), the fill (on the shadow side) and the lighting ratio
    balance_shot aims for. With nothing given, lists the lights amaran Desktop has paired."""

    settings = config.load()
    lights = [{"name": f["name"], "model": f["model"]} for f in amaran_lights()] if BACKEND != "sim" else []
    if key is not None:
        if BACKEND != "sim":
            named_light(key)  # fails here, listing the choices, if Desktop has no such light
        settings.key_light = key
    if fill is not None:
        if fill.lower() == "none":
            settings.fill_light = None
        else:
            if BACKEND != "sim":
                named_light(fill)
            settings.fill_light = fill
    if settings.fill_light and settings.key_light and settings.fill_light.lower() == settings.key_light.lower():
        raise ToolError("the key and the fill must be two different lights")
    if fill_ratio is not None:
        settings.fill_ratio = fill_ratio
    if key is not None or fill is not None or fill_ratio is not None:
        config.save(settings)
    return {
        "key": settings.key_light or "amaran-mcp's light",
        "fill": settings.fill_light,
        "fill_ratio": settings.fill_ratio,
        "lights": lights,
    }


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
async def start_recording() -> str:
    """Start recording to the camera's card. A coach_shot run carries on and watches the take."""

    await camera().record(True)
    return "recording"


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
async def stop_recording() -> str:
    """Stop recording to the camera's card. A coach_shot run reports the take and carries on."""

    await camera().record(False)
    return "stopped"


def _release_busy() -> None:
    global _holds_busy
    if _holds_busy:
        _holds_busy = False
        _busy.release()


class _NoTexts:
    """balance_shot returns its result to Claude; it texts nobody."""

    async def send(self, kind: str, body: str) -> None:
        pass


def _new_director(settings: Settings, publish: Any, texts: Texts, **options: Any) -> Director:
    """A Director on a fresh Rig for the camera and key; the two share one clock."""

    director: Director | None = None
    clock = time.monotonic
    rig = Rig(
        camera(),
        light(),
        on_slow=lambda: director.light_slow(),
        on_back=lambda: director.light_back(),
        clock=clock,
    )
    director = Director(rig, settings, publish, texts, clock=clock, **options)
    return director


async def _end_run() -> bool:
    """Stop the run and its page and release _busy; False when no run was going.
    Camera and lights are left as they are."""

    global _director, _web, _run_task
    director, web, task = _director, _web, _run_task
    if director is None:
        return False
    _director = _web = _run_task = None
    try:
        director.stop()
        if task is not None and not task.done():
            # Stopped at the top of its next tick, the run reports an open take and shows it ended;
            # a tick stuck on a Bluetooth connect is cancelled, and reports the take on the way out.
            await asyncio.wait({task}, timeout=STOP_WAIT_SECONDS)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("the shot coach failed while stopping")
        if web is not None:
            await web.close()
    finally:
        _release_busy()
    return True


def _run_finished(task: asyncio.Task) -> None:
    """The run ended on its own (30 minutes idle, or a camera error): free _busy and keep the
    page up showing the final state, until stop_coach or a new coach_shot."""

    if not task.cancelled() and task.exception() is not None:
        log.error("the shot coach failed", exc_info=task.exception())
    if task is _run_task:
        _release_busy()


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
async def coach_shot(
    key_side: Annotated[str | None, Field(description="left or right: which side of the face the key lights; leave out to let the coach choose")] = None,
    modifier: Annotated[str | None, Field(description="What is on the 100x S: bare, reflector, softbox, lantern or umbrella; saved")] = None,
    size_in: Annotated[float | None, Field(gt=0, description="Its size in inches (softbox, lantern, umbrella); saved")] = None,
    frame_rate: Annotated[float | None, Field(ge=FRAME_RATES[0], le=FRAME_RATES[1], description="Frames per second the camera records, e.g. 23.976 or 29.97, when it cannot say; saved")] = None,
    widest_aperture: Annotated[float | None, Field(ge=APERTURE_RANGE[0], le=APERTURE_RANGE[1], description="The lens's widest f-number, e.g. 2.8, when the camera cannot say; saved")] = None,
) -> list[Image | TextContent]:
    """Plan the shot and run the director: it sets the camera itself, shows the user where the
    key goes on a page on their phone (no taps), balances the light until Ready, then watches
    every take, holding the face and window steady and texting a report after each. Returns a
    QR code and the link (also texted); the plan is in the JSON. shot_status reads the run;
    stop_coach ends it."""

    global _director, _web, _run_task, _holds_busy
    if key_side not in (None, "left", "right"):
        raise ToolError("key_side must be left or right")
    if modifier is not None and modifier not in MODIFIERS:
        raise ToolError(f"modifier must be one of {', '.join(MODIFIERS)}")
    if frame_rate is not None and not FRAME_RATES[0] <= frame_rate <= FRAME_RATES[1]:
        raise ToolError(f"frame_rate must be from {FRAME_RATES[0]:g} to {FRAME_RATES[1]:g}")
    if widest_aperture is not None and not APERTURE_RANGE[0] <= widest_aperture <= APERTURE_RANGE[1]:
        raise ToolError(f"widest_aperture must be from {APERTURE_RANGE[0]:g} to {APERTURE_RANGE[1]:g}")
    if _run_task is not None and not _run_task.done():
        raise ToolError("a balance or the shot coach is already running")
    await _end_run()  # a run that ended on its own still has its page up
    if _busy.locked():
        raise ToolError("a balance or the shot coach is already running")
    settings = config.load()
    new_modifier = modifier or settings.key_modifier
    new_size = size_in if size_in is not None else (settings.key_size_in if new_modifier == settings.key_modifier else None)
    if new_modifier not in SIZED:
        new_size = None
    if new_modifier is None:
        raise ToolError("What is on the 100x S: bare, reflector, softbox, lantern or umbrella? "
                        "Give its size in inches for a softbox, lantern or umbrella.")
    if new_modifier in SIZED and not new_size:
        raise ToolError(f"How wide is the {new_modifier} in inches? Call coach_shot again with size_in.")
    new_rate = settings.frame_rate if frame_rate is None else frame_rate
    new_widest = settings.widest_aperture if widest_aperture is None else widest_aperture
    known = (settings.key_modifier, settings.key_size_in, settings.frame_rate, settings.widest_aperture)
    if (new_modifier, new_size, new_rate, new_widest) != known:
        settings.key_modifier, settings.key_size_in = new_modifier, new_size
        settings.frame_rate, settings.widest_aperture = new_rate, new_widest
        config.save(settings)
    broadcaster = Broadcaster()
    texts = texts_for_server()
    director = _new_director(settings, broadcaster.publish, texts, override=key_side)
    await _busy.acquire()
    _holds_busy = True
    _director = director  # from here, stop_coach can end it
    try:
        if (await camera().settings())["recording"]:
            raise ToolError("The camera is recording. Stop recording before setting up the shot.")
        await take_over_light()
        if _director is not director:
            raise ToolError("the shot coach was stopped before it began")
        start = _run_task = asyncio.create_task(director.start())
        try:
            plan = await start
        except LightUnreachable as err:
            log.warning("the light could not be reached", exc_info=err)
            raise ToolError(f"{UNREACHABLE[0]}. {UNREACHABLE[1]} (Bluetooth said: {err})") from err
        except LightError as err:
            raise ToolError(str(err)) from err
        except asyncio.CancelledError:
            if start.cancelled() and _director is not director:
                raise ToolError("the shot coach was stopped before it began") from None
            raise  # the client cancelled coach_shot itself
        if plan is None:
            raise ToolError("No face in the frame. Sit in the shot, then call coach_shot again.")
        web = await start_web(director, broadcaster)
        if _director is not director:
            await web.close()
            raise ToolError("the shot coach was stopped before it began")
        _web = web
        _run_task = asyncio.create_task(director.run())
        _run_task.add_done_callback(_run_finished)
        send_later(texts, "link", f"Shot coach: {web.url}", _sending)
    except BaseException:
        if _director is director:
            await _end_run()
        raise
    body: dict[str, Any] = {"url": web.url, "plan": plan.to_json()}
    if settings.fill_light is not None:  # the saved setting: the fill itself is not reached here
        body["note"] = FILL_NOTE
    return [Image(data=web.qr_png, format="png"), TextContent(type="text", text=json.dumps(body))]


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def stop_coach() -> str:
    """End the coach_shot run and its phone page. The camera and lights stay as they are."""

    return "coach stopped" if await _end_run() else "no coach was running"


@tool(annotations=READS)
async def shot_status() -> dict[str, Any]:
    """What the coach_shot run is doing: its mode (setup, take, stopped), the step and instruction
    the page shows, the face and window lights, the take so far and the last take's report.
    {"mode": "off"} when no run is going."""

    director = _director
    return {"mode": "off"} if director is None else director.status()


def serve() -> None:
    mcp.run()

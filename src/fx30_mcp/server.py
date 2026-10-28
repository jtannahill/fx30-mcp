"""MCP tools for the FX30 and the amaran light, together."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
import functools
import json
import os
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from . import config, meter
from .balance import balance
from .camera import CameraBase, CameraError, GPhotoCamera
from .coach import Coach, LightError
from .web import Broadcaster, WebHandle, start_web

BACKEND = os.environ.get("FX30_BACKEND", "gphoto2").lower()

_camera: CameraBase | None = None
_sim_light: Any = None
_named_lights: dict[tuple[str, str], Any] = {}  # (name, backend) -> light
_busy = asyncio.Lock()
_coach: Coach | None = None  # the shot coach, while one runs; it holds _busy
_web: WebHandle | None = None
_coach_task: asyncio.Task | None = None
_closing: set[asyncio.Task] = set()


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
        _named_lights[(name, backend)] = DesktopLight(light) if backend == "desktop" else AmaranLight(light)
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
        await _end_coach()
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
        "To set up a shot, call coach_shot: it plans the camera and lights and walks the user "
        "through placing them on a page on their phone (stop_coach ends it). "
        "Without the coach: look (check framing), balance_shot (sets white balance on camera and lights, "
        "then brings the face to the target with the light), start_recording. "
        "Camera settings are best left fixed for an interview; balance_shot only "
        "changes the light. set_face_target saves a face brightness the user likes."
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
    """Change ISO, shutter or aperture. Names must match what the camera offers; an error lists them."""

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
    """Light the face to the saved target: matches white balance on the camera and both lights,
    then steps the key light until the lit side of the face is within a sixth of a stop and,
    with a fill light set, the fill until the shadow side sits fill_ratio stops below it.
    Leaves the camera's exposure settings alone and says what to change if a light runs out
    of range."""

    if _coach is not None:
        raise ToolError("the shot coach is running; stop_coach first")
    if _busy.locked():
        raise ToolError("a balance is already running")
    async with _busy:
        await take_over_light()
        settings = config.load()
        sign = settings.tint_sign
        result = await balance(camera(), light(), settings, kelvin, fix_tint, max_steps, fill=fill_light())
        if settings.tint_sign != sign:
            config.save(settings)
        return result


@tool(annotations=CHANGES)
async def set_face_target(
    level: Annotated[float | None, Field(ge=0.05, le=0.95, description="Face brightness 0-1 as live view shows it; leave out to use the current frame")] = None,
    profile: Annotated[str | None, Field(description="rec709, or slog3 when live view shows the log picture (Gamma Display Assist off)")] = None,
) -> dict[str, Any]:
    """Save the face brightness (and skin colour) balance_shot aims for. With no level,
    takes them from the current frame: light it how you like, then call this."""

    settings = config.load()
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
            settings.window_range = [round(over - 1, 2), round(over + 1, 2)]
        settings.face_level = round(reading.lit, 3)
        if reading.hue is not None:
            settings.skin_hue = round(reading.hue, 1)
    else:
        settings.face_level = level
    config.save(settings)
    return settings.__dict__


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
    """Start recording to the camera's card."""

    if _coach is not None:
        await _end_coach()  # live view must not be read while recording
    await camera().record(True)
    return "recording"


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
async def stop_recording() -> str:
    """Stop recording to the camera's card."""

    await camera().record(False)
    return "stopped"


async def _end_coach() -> bool:
    """Stop the coach and its page and release _busy; False when no coach was running.
    Camera and lights are left as they are."""

    global _coach, _web, _coach_task
    coach, web, task = _coach, _web, _coach_task
    if coach is None:
        return False
    _coach = _web = _coach_task = None
    coach.stop()
    if task is not None and not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    try:
        if web is not None:
            await web.close()
    finally:
        if _busy.locked():
            _busy.release()
    return True


def _coach_finished(_task: asyncio.Task) -> None:
    """The coach's run ended on its own (Ready, or a camera or light error): tidy up."""

    closing = asyncio.get_running_loop().create_task(_end_coach())
    _closing.add(closing)
    closing.add_done_callback(_closing.discard)


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
async def coach_shot(
    key_side: Annotated[str | None, Field(description="left or right: which side of the face the key lights; leave out to let the coach choose")] = None,
    modifier: Annotated[str | None, Field(description="What is on the 100x S: bare, reflector, softbox, lantern or umbrella; saved")] = None,
    size_in: Annotated[float | None, Field(gt=0, description="Its size in inches (softbox, lantern, umbrella); saved")] = None,
) -> list[Image | TextContent]:
    """Plan the shot and coach the user through it from a page on their phone: camera settings,
    where the key and fill go, then the balance. Returns a QR code and the link; the plan is in
    the JSON. stop_coach ends it."""

    global _coach, _web, _coach_task
    if key_side not in (None, "left", "right"):
        raise ToolError("key_side must be left or right")
    if _busy.locked():
        raise ToolError("a balance or the shot coach is already running")
    settings = config.load()
    if modifier is not None or size_in is not None:
        if modifier is not None:
            settings.key_modifier = modifier
        if size_in is not None:
            settings.key_size_in = size_in
        config.save(settings)
    if settings.key_modifier is None:
        raise ToolError("What is on the 100x S: bare, reflector, softbox, lantern or umbrella? "
                        "Give its size in inches for a softbox, lantern or umbrella.")
    await _busy.acquire()
    try:
        await take_over_light()
        broadcaster = Broadcaster()
        coach = Coach(camera(), light(), fill_light(), settings, broadcaster.publish, override=key_side)
        try:
            plan = await coach.start()
        except LightError as err:
            raise ToolError(str(err)) from err
        if plan is None:
            raise ToolError("No face in the frame. Sit in the shot, then call coach_shot again.")
        web = await start_web(coach, broadcaster)
    except BaseException:
        _busy.release()
        raise
    _coach, _web = coach, web
    _coach_task = asyncio.create_task(coach.run())
    _coach_task.add_done_callback(_coach_finished)
    return [Image(data=web.qr_png, format="png"),
            TextContent(type="text", text=json.dumps({"url": web.url, "plan": plan.to_json()}))]


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
async def stop_coach() -> str:
    """End the shot coach and its phone page. The camera and lights stay as they are."""

    return "coach stopped" if await _end_coach() else "no coach was running"


def serve() -> None:
    mcp.run()

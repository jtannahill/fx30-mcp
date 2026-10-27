"""MCP tools for the FX30 and the amaran light, together."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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

BACKEND = os.environ.get("FX30_BACKEND", "gphoto2").lower()

_camera: CameraBase | None = None
_sim_light: Any = None
_busy = asyncio.Lock()


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

    return current_light()


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
        if _camera is not None:
            await _camera.close()
        if BACKEND != "sim":
            from amaran_mcp.backends import close_all

            await close_all()


mcp = MCPServer(
    "fx30",
    instructions=(
        "Controls the user's Sony FX30 over USB (PC Remote) and lights a one-person "
        "talking head with their amaran light. The camera records to its own card. "
        "Typical order: look (check framing), balance_shot (sets white balance on both, "
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
    report["stops_to_target"] = round(meter.stops_off(reading.level, settings.target, settings.profile), 2)
    off = reading.hue_off(settings.skin_hue)
    report["hue_off"] = None if off is None else round(off, 1)
    return [Image(data=jpeg, format="jpeg"), TextContent(type="text", text=json.dumps(report))]


@tool(annotations=CHANGES)
async def match_white(kelvin: Kelvin) -> str:
    """Set the light to white at this colour temperature and the camera's white balance to match."""

    await take_over_light()
    lamp = light()
    await lamp.set_white(kelvin, None, 0)
    try:
        await camera().set_kelvin(kelvin)
    except CameraError as err:
        return f"light at {kelvin}K; camera white balance not set ({err}), set it to {kelvin}K by hand"
    return f"light and camera both at {kelvin}K"


@tool(annotations=CHANGES)
async def balance_shot(
    kelvin: Kelvin | None = None,
    fix_tint: Annotated[bool, Field(description="Also take out a green or magenta cast with the light's tint")] = True,
    max_steps: Annotated[int, Field(ge=1, le=20)] = 8,
) -> dict[str, Any]:
    """Light the face to the saved target: matches white balance, then steps the light until
    the face is within a sixth of a stop. Leaves the camera's exposure settings alone and says
    what to change if the light runs out of range."""

    if _busy.locked():
        raise ToolError("a balance is already running")
    async with _busy:
        await take_over_light()
        settings = config.load()
        sign = settings.tint_sign
        result = await balance(camera(), light(), settings, kelvin, fix_tint, max_steps)
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
        reading = await asyncio.to_thread(meter.read, await camera().frame())
        if not reading.face:
            raise ToolError("no face in frame; sit in the shot first, or give a level")
        settings.face_level = round(reading.level, 3)
        if reading.hue is not None:
            settings.skin_hue = round(reading.hue, 1)
    else:
        settings.face_level = level
    config.save(settings)
    return settings.__dict__


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
async def start_recording() -> str:
    """Start recording to the camera's card."""

    await camera().record(True)
    return "recording"


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
async def stop_recording() -> str:
    """Stop recording to the camera's card."""

    await camera().record(False)
    return "stopped"


def serve() -> None:
    mcp.run()

"""The Rig: the camera and the key light behind two calls, state and apply.

It is the one place the director writes to the camera and the key. It records every camera value it
writes in history.commanded, so decide can tell its own changes from the owner's hand."""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Awaitable, Callable

from .camera import CameraBase, CameraError
from .decide import History, Moves, RigState

SLOW_LIGHT = 3.0  # seconds before a light call is said to be connecting
PAIR_DELAY = 0.0  # seconds between an exposure step and the light move that rides with it (Task 10 measures it)
EXPOSURE_STEPS = ("aperture", "iso", "shutter")


class LightError(Exception):
    """A light stopped answering: amaran Desktop closed, Bluetooth dropped, a bad reply."""


class LightUnreachable(LightError):
    """The light could not be reached over Bluetooth, most often because a phone app holds its one connection."""


def _unreachable(err: Exception) -> bool:
    try:
        from amaran_mcp.link import LinkError
    except ImportError:
        LinkError = ()  # noqa: N806
    text = str(err).lower()
    return isinstance(err, LinkError) or "could not connect" in text or "not advertising" in text


class GuardedLight:
    """A light whose failures come out as LightError, so they are told apart from the coach's own.

    A call still waiting after SLOW_LIGHT seconds is connecting: slow() is called then, and back()
    once it returns."""

    def __init__(self, light: Any, slow: Callable[[], None], back: Callable[[], None]) -> None:
        self._light = light
        self._slow = slow
        self._back = back

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._light, name)
        if not inspect.iscoroutinefunction(value):
            return value

        async def call(*args: Any, **kwargs: Any) -> Any:
            fired: list[bool] = []

            def slow() -> None:
                fired.append(True)
                self._slow()

            timer = asyncio.get_running_loop().call_later(SLOW_LIGHT, slow)
            try:
                result = await value(*args, **kwargs)
            except CameraError:
                raise
            except Exception as err:
                kind = LightUnreachable if _unreachable(err) else LightError
                raise kind(str(err) or type(err).__name__) from err
            finally:
                timer.cancel()
            if fired:
                self._back()
            return result

        return call


class Rig:
    def __init__(
        self,
        camera: CameraBase,
        key: Any,
        on_slow: Callable[[], None],
        on_back: Callable[[], None],
        pair_delay: float = PAIR_DELAY,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.camera = camera
        self.key = GuardedLight(key, on_slow, on_back)
        self.pair_delay = pair_delay
        self._clock = clock
        self._sleep = sleep

    async def state(self) -> RigState:
        camera = await self.camera.settings()
        light = self.key.state
        if light.power is None or light.brightness is None or light.kelvin is None:
            try:
                await self.key.read_status()
            except LightError:
                pass
        return RigState(light.power, light.brightness, light.kelvin, int(light.tint or 0), camera)

    async def apply(self, moves: Moves, history: History) -> None:
        step = moves.camera
        if step is not None:
            if step.setting == "exposure_mode":
                await self.camera.set_exposure_mode(step.value)
            elif step.setting == "kelvin":
                await self.camera.set_kelvin(int(step.value))
            else:
                await self.camera.set(step.setting, step.value)
            history.commanded[step.setting] = step.value
            if step.setting in EXPOSURE_STEPS:
                history.last_camera_step = self._clock()
        move = moves.light
        if move is None:
            return
        if step is not None and step.setting in EXPOSURE_STEPS and self.pair_delay > 0:
            await self._sleep(self.pair_delay)
        was_off = self.key.state.power is False
        await self.key.set_white(move.kelvin, move.brightness, move.tint)
        if was_off:
            await self.key.set_power(True)

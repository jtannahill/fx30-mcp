"""The camera: settings, recording to card and live-view frames.

CameraBase is what the rest of the package uses. GPhotoCamera reaches the FX30
over USB with libgphoto2, which needs the camera in PC Remote mode. Sony's
Camera Remote SDK is the other way in (it lists the FX30) and can be added as
another CameraBase once the SDK is downloaded.

Calls into libgphoto2 block, so each runs in a worker thread, one at a time.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from typing import Any

PC_REMOTE_HINT = (
    "Connect the FX30 to this Mac by USB and turn on PC Remote "
    "(Menu > Network > PC Remote Function: PC Remote On, connection method USB). "
    "Quit Sony's own remote apps and Image Capture first: only one program can hold the camera."
)

# The names libgphoto2 gives each setting, in the order to try them.
SETTING_NAMES = {
    "iso": ["iso"],
    "shutter": ["shutterspeed"],
    "aperture": ["f-number", "aperture"],
    "white_balance": ["whitebalance"],
    "kelvin": ["colortemperature"],
}

# Candidate names for what describe() reports. Unknown names read as None, so a
# wrong guess is harmless. Only the names confirmed on the camera are known to
# work; the rest are guesses until checked with `fx30-mcp props`.
INFO_NAMES = {
    "frame_rate": ["movieframerate", "recordingframerate", "moviefps"],
    "file_format": ["movierecordsetting", "movieformat", "fileformat"],
    "picture_profile": ["pictureprofile"],
    "gamma": ["gamma"],
    "display_assist": ["gammadisplayassist"],
}


def fps_from(text: str | None) -> float | None:
    """The frame rate in text like "29.97p" or "4K 30p 100M"; None if there is none."""

    match = re.search(r"(\d+(?:\.\d+)?)\s*[pi]\b", text or "", re.IGNORECASE)
    return float(match.group(1)) if match else None


def _flag(value: Any) -> bool | None:
    """On/Off style values as a bool; None if the value is not one of those."""

    text = str(value).strip().lower()
    if text in ("on", "1", "true"):
        return True
    if text in ("off", "0", "false"):
        return False
    return None


WEDGED_HINT = (
    "The camera is not answering. If a program quit while it had the camera open, "
    "the FX30 keeps waiting for it: turn the camera off and on (PC Remote stays on)."
)

# Sony's "Movie Recording State" property; libgphoto2 has no name for it.
RECORDING_STATE = "d21d"


class CameraError(Exception):
    pass


class CameraBase:
    name: str
    backend: str

    async def settings(self) -> dict[str, Any]: ...
    async def describe(self) -> dict[str, Any]: ...
    async def set(self, setting: str, value: str) -> str: ...
    async def set_kelvin(self, kelvin: int) -> None: ...
    async def frame(self) -> bytes: ...
    async def record(self, on: bool) -> None: ...
    async def close(self) -> None: ...


class GPhotoCamera(CameraBase):
    backend = "gphoto2"

    def __init__(self, idle_timeout: float = 10.0) -> None:
        self._camera = None
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        self.idle_timeout = idle_timeout
        self.name = "camera"
        self.recording = False

    async def _run(self, fn, *args):
        import gphoto2 as gp

        if self._idle_task:
            self._idle_task.cancel()
        async with self._lock:
            try:
                if self._camera is None:
                    self._camera = await asyncio.to_thread(self._open)
                return await asyncio.to_thread(fn, *args)
            except gp.GPhoto2Error as err:
                if err.code in (gp.GP_ERROR_MODEL_NOT_FOUND, gp.GP_ERROR_IO_USB_CLAIM, gp.GP_ERROR_IO,
                                gp.GP_ERROR_IO_USB_FIND, gp.GP_ERROR_CAMERA_BUSY, gp.GP_ERROR_TIMEOUT):
                    await self._drop()
                    raise CameraError(f"{err}. {WEDGED_HINT if err.code == gp.GP_ERROR_TIMEOUT else PC_REMOTE_HINT}") from err
                raise CameraError(str(err)) from err
            finally:
                self._idle_task = asyncio.create_task(self._close_when_idle())

    async def _close_when_idle(self) -> None:
        """Let go of the camera between uses. The FX30 keeps a session that was never
        closed (a crash, a killed server) and refuses new ones until it is power cycled,
        so the less time a session is open, the less often that happens."""

        await asyncio.sleep(self.idle_timeout)
        if self.recording:
            return  # what closing does to a recording is untested; stay with it
        # Once started, finish closing even if a new call arrives; it waits on the lock.
        await asyncio.shield(self._close_locked())

    async def _close_locked(self) -> None:
        async with self._lock:
            await self._drop()

    def _open(self):
        import gphoto2 as gp

        # What ptpcamerad holding the camera looks like: refused outright, or
        # half shared, so commands time out.
        SHARED_ERRORS = (gp.GP_ERROR_IO_USB_CLAIM, gp.GP_ERROR_TIMEOUT, gp.GP_ERROR_IO, gp.GP_ERROR_IO_USB_FIND)
        camera = gp.Camera()
        try:
            camera.init()
            camera.get_single_config("cameramodel")
        except gp.GPhoto2Error as err:
            if err.code not in SHARED_ERRORS:
                raise
            # macOS's ptpcamerad claims every camera that is plugged in. launchd
            # restarts it, but not before the camera is ours. The camera drops off
            # USB for a moment when it lets go, so keep trying for a few seconds.
            deadline = time.monotonic() + 8
            while True:
                subprocess.run(["killall", "-9", "ptpcamerad"], capture_output=True)
                time.sleep(0.5)
                camera = gp.Camera()
                try:
                    camera.init()
                    break
                except gp.GPhoto2Error as retry:
                    if time.monotonic() > deadline or retry.code not in SHARED_ERRORS + (gp.GP_ERROR_MODEL_NOT_FOUND,):
                        raise
        try:
            self.name = camera.get_single_config("cameramodel").get_value()
        except gp.GPhoto2Error:
            self.name = camera.get_abilities().model
        return camera

    async def _drop(self) -> None:
        if self._camera is not None:
            camera, self._camera = self._camera, None
            try:
                await asyncio.to_thread(camera.exit)
            except Exception:
                pass

    def _widget(self, setting: str):
        import gphoto2 as gp

        for name in SETTING_NAMES.get(setting, [setting]):
            try:
                return name, self._camera.get_single_config(name)
            except gp.GPhoto2Error:
                continue
        raise CameraError(f"this camera does not offer {setting} over USB")

    def _read(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for setting in SETTING_NAMES:
            try:
                _name, widget = self._widget(setting)
                out[setting] = widget.get_value()
            except CameraError:
                out[setting] = None
        return out

    def _choices(self, setting: str) -> list[str]:
        try:
            _name, widget = self._widget(setting)
            return [widget.get_choice(i) for i in range(widget.count_choices())]
        except Exception:
            return []

    def _info(self) -> dict[str, Any]:
        import gphoto2 as gp

        raw: dict[str, Any] = {}
        for key, names in INFO_NAMES.items():
            raw[key] = None
            for name in names:
                try:
                    raw[key] = self._camera.get_single_config(name).get_value()
                    break
                except gp.GPhoto2Error:
                    continue
        return {
            "frame_rate": fps_from(raw["frame_rate"]) or fps_from(raw["file_format"]),
            "file_format": raw["file_format"],
            "picture_profile": raw["picture_profile"],
            "gamma": raw["gamma"],
            "display_assist": _flag(raw["display_assist"]),
            "apertures": self._choices("aperture"),
            "shutters": self._choices("shutter"),
        }

    def _write(self, setting: str, value: str) -> str:
        import gphoto2 as gp

        name, widget = self._widget(setting)
        if widget.get_readonly():
            raise CameraError(
                f"the camera keeps {setting} on its own controls in this mode "
                f"(now {widget.get_value()}); change it on the camera"
            )
        if widget.get_type() in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
            choices = [widget.get_choice(i) for i in range(widget.count_choices())]
            match = next((c for c in choices if c.lower() == value.lower()), None)
            if match is None:
                match = next((c for c in choices if c.lower().replace(" ", "") == value.lower().replace(" ", "")), None)
            if match is None:
                raise CameraError(f"{setting} {value!r} is not offered; choices: {', '.join(choices)}")
            value = match
        widget.set_value(value)
        self._camera.set_single_config(name, widget)
        return value

    def _set_kelvin(self, kelvin: int) -> None:
        import gphoto2 as gp

        _name, widget = self._widget("white_balance")
        choices = [widget.get_choice(i) for i in range(widget.count_choices())]
        manual = next((c for c in choices if "temp" in c.lower() or "kelvin" in c.lower()), None)
        if manual is None:
            raise CameraError(f"no colour temperature white balance offered; choices: {', '.join(choices)}")
        if widget.get_value() != manual:
            self._write("white_balance", manual)
            time.sleep(0.5)  # a write straight after a mode change is ignored
        name, widget = self._widget("kelvin")
        if widget.get_type() in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
            self._write("kelvin", min(
                (widget.get_choice(i) for i in range(widget.count_choices())),
                key=lambda c: abs(int("".join(ch for ch in c if ch.isdigit()) or 0) - kelvin),
            ))
            return
        low, high, step = widget.get_range() if widget.get_type() == gp.GP_WIDGET_RANGE else (0, 1e9, 1)
        wanted = float(min(high, max(low, round(kelvin / step) * step)))
        for _ in range(3):
            widget.set_value(wanted if widget.get_type() == gp.GP_WIDGET_RANGE else str(int(wanted)))
            self._camera.set_single_config(name, widget)
            time.sleep(0.2)
            _name, widget = self._widget("kelvin")
            if float(widget.get_value()) == wanted:
                return
        raise CameraError(f"the camera did not take {int(wanted)}K (still {widget.get_value()})")

    def _frame(self) -> bytes:
        # Right after recording stops the camera can send an empty frame for a moment.
        for _ in range(10):
            data = bytes(memoryview(self._camera.capture_preview().get_data_and_size()))
            start, end = data.find(b"\xff\xd8"), data.rfind(b"\xff\xd9")
            if 0 <= start < end:
                return data[start:end + 2]
            time.sleep(0.2)
        raise CameraError(f"live view sent no picture ({len(data)} bytes); is the camera showing live view?")

    def _recording(self) -> bool | None:
        """What the camera says, from Sony's Movie Recording State; None if it does not say."""

        import gphoto2 as gp

        try:
            return int(self._camera.get_single_config(RECORDING_STATE).get_value()) != 0
        except (gp.GPhoto2Error, ValueError):
            return None

    def _record(self, on: bool) -> None:
        import gphoto2 as gp

        try:
            widget = self._camera.get_single_config("movie")
        except gp.GPhoto2Error as err:
            raise CameraError("this camera does not offer movie recording over USB") from err
        widget.set_value(1 if on else 0)
        self._camera.set_single_config("movie", widget)
        deadline = time.monotonic() + 3
        while (state := self._recording()) is not None and state != on:
            if time.monotonic() > deadline:
                raise CameraError(
                    f"the camera did not {'start' if on else 'stop'} recording "
                    "(card in, space left, camera in movie mode?)"
                )
            time.sleep(0.1)
        self.recording = on

    async def settings(self) -> dict[str, Any]:
        out = await self._run(self._read)
        state = await self._run(self._recording)
        out["recording"] = self.recording if state is None else state
        return out

    async def describe(self) -> dict[str, Any]:
        return await self._run(self._info)

    async def set(self, setting: str, value: str) -> str:
        return await self._run(self._write, setting, value)

    async def set_kelvin(self, kelvin: int) -> None:
        await self._run(self._set_kelvin, kelvin)

    async def frame(self) -> bytes:
        return await self._run(self._frame)

    async def record(self, on: bool) -> None:
        await self._run(self._record, on)

    async def close(self) -> None:
        if self._idle_task:
            self._idle_task.cancel()
        async with self._lock:
            await self._drop()

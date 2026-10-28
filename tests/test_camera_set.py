import asyncio

import pytest

from fx30_mcp import camera as camera_module
from fx30_mcp.camera import CameraError, GPhotoCamera
from fx30_mcp.sim import SimCamera, SimLight

CHOICES = {
    "expprogram": ["Intelligent Auto", "P", "A", "S", "M", "Movie F Mode", "S&Q F Mode"],
    "shutterspeed": ["1/30", "1/50", "1/60", "1/100"],
    "iso": ["800", "2500"],
    "f-number": ["f/2.8", "f/4", "f/5.6"],
}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(camera_module, "CONFIRM_POLL", 0.01)
    monkeypatch.setattr(camera_module, "CONFIRM_SECONDS", 0.2)
    monkeypatch.setattr(camera_module, "MODE_SETTLE", 0.0)


class _Widget:
    def __init__(self, value, choices, readonly):
        self.value, self.choices, self.readonly = value, choices, readonly

    def get_readonly(self):
        return int(self.readonly)

    def get_type(self):
        import gphoto2 as gp

        return gp.GP_WIDGET_RADIO

    def count_choices(self):
        return len(self.choices)

    def get_choice(self, i):
        return self.choices[i]

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.value = value


class _FX30:
    """Like the real one: shutter and ISO are read-only outside M, and a write shows up one read late."""

    def __init__(self, mode="Movie F Mode", lag=1, ignores=()):
        self.values = {"expprogram": mode, "shutterspeed": "1/30", "iso": "800", "f-number": "f/2.8"}
        self.lag, self.ignores = lag, ignores
        self.pending: dict[str, list] = {}
        self.writes: list[tuple[str, str]] = []
        self.reads = 0

    def get_single_config(self, name):
        import gphoto2 as gp

        if name not in self.values:
            raise gp.GPhoto2Error(gp.GP_ERROR_BAD_PARAMETERS)
        self.reads += 1
        if name in self.pending:
            if self.pending[name][0] <= 0:
                self.values[name] = self.pending.pop(name)[1]
            else:
                self.pending[name][0] -= 1
        readonly = name in ("shutterspeed", "iso") and self.values["expprogram"] != "M"
        return _Widget(self.values[name], CHOICES[name], readonly)

    def set_single_config(self, name, widget):
        self.writes.append((name, widget.get_value()))
        if name not in self.ignores:
            self.pending[name] = [self.lag, widget.get_value()]


def run(camera: GPhotoCamera, coro):
    async def go():
        try:
            return await coro
        finally:
            if camera._idle_task:
                camera._idle_task.cancel()

    return asyncio.run(go())


def gphoto(fx30: _FX30) -> GPhotoCamera:
    camera = GPhotoCamera()
    camera._camera = fx30
    return camera


def test_set_waits_for_the_readback():
    fx30 = _FX30(mode="M")
    camera = gphoto(fx30)
    assert run(camera, camera.set("shutter", "1/50")) == "1/50"
    assert fx30.writes == [("shutterspeed", "1/50")]
    assert fx30.values["shutterspeed"] == "1/50"


def test_set_that_never_takes_says_so():
    fx30 = _FX30(mode="M", ignores=("shutterspeed",))
    camera = gphoto(fx30)
    with pytest.raises(CameraError, match=r"^the camera did not take shutter 1/50 \(still 1/30\)$"):
        run(camera, camera.set("shutter", "1/50"))


def test_set_outside_m_keeps_the_read_only_message():
    fx30 = _FX30()
    camera = gphoto(fx30)
    with pytest.raises(CameraError, match=r"keeps shutter on its own controls in this mode \(now 1/30\); change it on the camera"):
        run(camera, camera.set("shutter", "1/50"))
    assert fx30.writes == []


def test_set_exposure_mode_writes_expprogram_then_shutter_is_writable():
    fx30 = _FX30()
    camera = gphoto(fx30)
    run(camera, camera.set_exposure_mode("M"))
    assert fx30.writes == [("expprogram", "M")] and fx30.values["expprogram"] == "M"
    assert run(camera, camera.set("iso", "2500")) == "2500"


def test_set_exposure_mode_skips_when_already_there():
    fx30 = _FX30(mode="M")
    camera = gphoto(fx30)
    run(camera, camera.set_exposure_mode("M"))
    assert fx30.writes == []


def test_settings_report_the_exposure_mode():
    assert gphoto(_FX30())._read()["exposure_mode"] == "Movie F Mode"


def test_sim_starts_in_movie_f_mode_with_shutter_and_iso_read_only():
    camera = SimCamera(SimLight())
    assert asyncio.run(camera.settings())["exposure_mode"] == "Movie F Mode"
    for setting in ("shutter", "iso"):
        now = camera.values[setting]
        with pytest.raises(CameraError, match=rf"^the camera keeps {setting} on its own controls in this mode \(now {now}\); change it on the camera$"):
            asyncio.run(camera.set(setting, "1/50" if setting == "shutter" else "2500"))
    assert asyncio.run(camera.set("aperture", "f/4")) == "f/4" and camera.values["aperture"] == "f/4"


def test_sim_in_m_takes_shutter_and_iso():
    camera = SimCamera(SimLight())
    asyncio.run(camera.set_exposure_mode("M"))
    assert asyncio.run(camera.settings())["exposure_mode"] == "M"
    assert asyncio.run(camera.set("shutter", "1/50")) == "1/50"
    assert asyncio.run(camera.set("iso", "2500")) == "2500"
    assert camera.values == {"iso": "2500", "shutter": "1/50", "aperture": "f/2.8"}

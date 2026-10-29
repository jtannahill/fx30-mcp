import math

from fx30_mcp import meter, scene
from fx30_mcp.sim import SimCamera, SimLight

SIM_FACE = (100 / 320, 40 / 180, 120 / 320, 100 / 180)


def test_read_scene_decodes_once_and_asks_vision_once(monkeypatch):
    calls = {"decode": 0, "detect": 0}
    real_decode = meter.decode

    def counting_decode(jpeg):
        calls["decode"] += 1
        return real_decode(jpeg)

    def counting_detect(_jpeg):
        calls["detect"] += 1
        return SIM_FACE, 12.0

    monkeypatch.setattr(meter, "decode", counting_decode)
    monkeypatch.setattr(meter, "detect", counting_detect)
    camera = SimCamera(SimLight(), gain=0.5, window=4.0)
    result = scene.read_scene(camera.render(), {"meter": 0}, camera.profile)
    assert calls == {"decode": 1, "detect": 1}
    assert result.yaw == 12.0


def test_given_box_skips_vision(monkeypatch):
    def boom(_jpeg):
        raise AssertionError("Vision should not run")

    monkeypatch.setattr(meter, "detect", boom)
    camera = SimCamera(SimLight())
    result = scene.read_scene(camera.render(), {}, camera.profile, box=SIM_FACE)
    assert result.face and result.box == SIM_FACE and result.yaw is None


def test_scene_matches_the_old_readings(monkeypatch):
    monkeypatch.setattr(meter, "detect", lambda _jpeg: (SIM_FACE, None))
    monkeypatch.setattr(meter, "find_face", lambda _jpeg: SIM_FACE)
    camera = SimCamera(SimLight(), gain=0.5, window=4.0)
    frame = camera.render()
    settings = {"meter": 1, "recording": False}
    result = scene.read_scene(frame, settings, camera.profile)
    old = meter.read(frame)
    assert result.level == old.lit
    assert result.sides == old.sides
    assert (result.cb, result.cr, result.hue, result.chroma, result.clipped) == (
        old.cb, old.cr, old.hue, old.chroma, old.clipped
    )
    assert result.window == meter.find_window(frame, result.box, "rec709")
    assert result.window is not None
    assert result.camera is settings
    assert result.frame is frame


def test_no_face_reads_the_middle_and_says_so(monkeypatch):
    monkeypatch.setattr(meter, "detect", lambda _jpeg: None)
    camera = SimCamera(SimLight())
    result = scene.read_scene(camera.render(), {}, camera.profile)
    assert result.face is False
    assert result.box is None
    assert result.yaw is None


def test_reading_has_cb_and_cr(monkeypatch):
    monkeypatch.setattr(meter, "find_face", lambda _jpeg: SIM_FACE)
    reading = meter.read(SimCamera(SimLight()).render())
    assert reading.cr > 0 > reading.cb
    assert reading.hue == math.degrees(math.atan2(reading.cr, reading.cb)) % 360

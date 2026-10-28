import asyncio

import pytest

from fx30_mcp.camera import fps_from
from fx30_mcp.sim import SimCamera, SimLight


@pytest.mark.parametrize("text,fps", [
    ("29.97p", 29.97), ("30p", 30.0), ("4K 30p 100M", 30.0), ("23.98p", 23.98),
    ("59.94i", 59.94), ("abc", None), (None, None),
])
def test_fps_from(text, fps):
    assert fps_from(text) == fps


def test_sim_describe():
    info = asyncio.run(SimCamera(SimLight()).describe())
    assert info["frame_rate"] == 29.97
    assert info["file_format"] == "XAVC S 4K"
    assert info["display_assist"] is False
    assert info["apertures"][0] == "f/2.8" and info["apertures"][-1] == "f/22"
    assert info["shutters"][0] == "1/60" and info["shutters"][-1] == "1/500"


class _Widget:
    def __init__(self, value, label=""):
        self.value, self.label = value, label

    def get_value(self):
        return self.value

    def get_label(self):
        return self.label


class _Names:
    def __init__(self, names):
        self.names = names

    def count(self):
        return len(self.names)

    def get_name(self, i):
        return self.names[i]


class _FakeCamera:
    def __init__(self, values):
        self.values = values

    def get_single_config(self, name):
        import gphoto2 as gp

        if name not in self.values:
            raise gp.GPhoto2Error(gp.GP_ERROR_BAD_PARAMETERS)
        return _Widget(*self.values[name])

    def list_config(self):
        return _Names(list(self.values))


def _camera(values):
    from fx30_mcp.camera import GPhotoCamera

    cam = GPhotoCamera()
    cam._camera = _FakeCamera(values)
    return cam


def test_info_mapping_profile_off():
    info = _camera({"d241": ("8",), "d242": ("52",), "d23f": ("0",)})._info()
    assert info["file_format"] == "8"
    assert info["recording_setting"] == "52"
    assert info["picture_profile"] == "Off"
    assert info["gamma"] == "Rec709"
    assert info["frame_rate"] is None and info["display_assist"] is None
    assert info["apertures"] == [] and info["shutters"] == []


def test_info_mapping_profile_code_and_missing():
    info = _camera({"d23f": ("7",)})._info()
    assert info["picture_profile"] == "PP code 7"
    assert info["gamma"] is None
    assert info["file_format"] is None
    empty = _camera({})._info()
    assert empty["picture_profile"] is None and empty["gamma"] is None


def test_config_entries_match_name_or_label():
    cam = _camera({"d23f": ("0", "Picture Profile"), "iso": ("800", "ISO")})
    assert cam._config_entries("profile") == [("d23f", "Picture Profile", "0")]
    assert cam._config_entries("iso") == [("iso", "ISO", "800")]
    assert len(cam._config_entries("")) == 2


@pytest.mark.parametrize("value,flag", [("On", True), ("Off", False), ("1", True), ("garbage", None)])
def test_flag(value, flag):
    from fx30_mcp.camera import _flag

    assert _flag(value) is flag

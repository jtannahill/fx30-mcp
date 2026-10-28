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

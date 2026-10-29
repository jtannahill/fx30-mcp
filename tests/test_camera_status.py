import asyncio

from fx30_mcp.camera import GPhotoCamera, parse_status


def test_status_values_parse():
    raw = {"d1b5": "700.0", "batterylevel": "62%", "d251": "0", "d248": "1", "d256": "1",
           "liveviewsettingeffect": "On"}
    assert parse_status(raw) == {
        "meter": 0.7, "battery": 62, "overheat": False, "cards_ok": True, "live_view_effect": True}


def test_missing_status_values_are_none():
    assert parse_status({}) == {"meter": None, "battery": None, "overheat": None, "cards_ok": None,
                                "live_view_effect": None}


def test_overheat_and_bad_card_read_true_and_false():
    out = parse_status({"d251": "2", "d248": "1", "d256": "0"})
    assert out["overheat"] is True
    assert out["cards_ok"] is False


class Handle:
    def exit(self):
        pass


def test_a_held_camera_is_not_closed_when_idle():
    async def go():
        camera = GPhotoCamera(idle_timeout=0.01)
        handle = camera._camera = Handle()
        camera.hold(True)
        await camera._run(lambda: None)
        await asyncio.sleep(0.05)
        assert camera._camera is handle
        camera.hold(False)
        await asyncio.sleep(0.05)
        assert camera._camera is None

    asyncio.run(go())

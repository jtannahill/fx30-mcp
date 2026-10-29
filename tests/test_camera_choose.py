import sys
import types

import pytest

from fx30_mcp import camera as camera_module
from fx30_mcp.camera import CameraError, GPhotoCamera, choose_camera


def test_the_sony_is_chosen_over_an_iphone_listed_first():
    found = [("Apple iPhone 5 (PTP mode)", "usb:002,001"), ("Sony ILME-FX30 (Control)", "usb:002,004")]
    assert choose_camera(found) == ("Sony ILME-FX30 (Control)", "usb:002,004")


def test_the_fx30_listed_as_a_generic_ptp_camera_is_chosen_over_an_iphone():
    found = [("Apple iPhone 5 (PTP mode)", "usb:002,001"), ("USB PTP Class Camera", "usb:002,004")]
    assert choose_camera(found) == ("USB PTP Class Camera", "usb:002,004")


def test_nothing_is_chosen_with_only_an_iphone():
    assert choose_camera([("Apple iPhone 5 (PTP mode)", "usb:002,001")]) is None
    assert choose_camera([]) is None


def test_opening_with_only_an_iphone_says_the_fx30_is_missing(monkeypatch):
    class Listed:
        def count(self):
            return 1

        def get_name(self, i):
            return "Apple iPhone 5 (PTP mode)"

        def get_value(self, i):
            return "usb:002,001"

    class Camera:
        @staticmethod
        def autodetect():
            return Listed()

        def init(self):
            raise AssertionError("the iPhone must not be opened")

    class GPhoto2Error(Exception):
        def __init__(self, code):
            self.code = code

    fake = types.SimpleNamespace(
        Camera=Camera, GPhoto2Error=GPhoto2Error, GP_ERROR_IO_USB_CLAIM=-53, GP_ERROR_TIMEOUT=-10,
        GP_ERROR_IO=-7, GP_ERROR_IO_USB_FIND=-52, GP_ERROR_MODEL_NOT_FOUND=-105,
    )
    monkeypatch.setitem(sys.modules, "gphoto2", fake)
    monkeypatch.setattr(camera_module.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(camera_module.time, "sleep", lambda s: None)
    clock = iter(range(0, 1000, 3))  # each look at the clock is 3 s on
    monkeypatch.setattr(camera_module.time, "monotonic", lambda: next(clock))
    with pytest.raises(CameraError, match="not on USB .*Apple iPhone"):
        GPhotoCamera()._open()


def test_a_camera_that_drops_off_usb_for_a_moment_is_opened_on_a_retry(monkeypatch):
    lists = iter([[], [], [("USB PTP Class Camera", "usb:001,001")]])
    opened = []

    class Listed:
        def __init__(self, entries):
            self.entries = entries

        def count(self):
            return len(self.entries)

        def get_name(self, i):
            return self.entries[i][0]

        def get_value(self, i):
            return self.entries[i][1]

    class Widget:
        def get_value(self):
            return "ILME-FX30"

    class Camera:
        @staticmethod
        def autodetect():
            return Listed(next(lists))

        def set_abilities(self, a):
            pass

        def set_port_info(self, p):
            pass

        def init(self):
            opened.append(True)

        def get_single_config(self, name):
            return Widget()

    class Lookup(list):
        def load(self):
            pass

        def lookup_model(self, name):
            return 0

        def lookup_path(self, port):
            return 0

    class GPhoto2Error(Exception):
        def __init__(self, code):
            self.code = code

    fake = types.SimpleNamespace(
        Camera=Camera, GPhoto2Error=GPhoto2Error, GP_ERROR_IO_USB_CLAIM=-53, GP_ERROR_TIMEOUT=-10,
        GP_ERROR_IO=-7, GP_ERROR_IO_USB_FIND=-52, GP_ERROR_MODEL_NOT_FOUND=-105,
        CameraAbilitiesList=lambda: Lookup([None]), PortInfoList=lambda: Lookup([None]),
    )
    monkeypatch.setitem(sys.modules, "gphoto2", fake)
    monkeypatch.setattr(camera_module.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(camera_module.time, "sleep", lambda s: None)
    camera = GPhotoCamera()
    camera._open()
    assert opened == [True] and camera.name == "ILME-FX30"


def test_a_frame_is_copied_while_its_file_is_still_alive():
    # libgphoto2's buffer belongs to the CameraFile: once the file is freed, the
    # allocator writes over the start of it (seen on the FX30 on 2026-10-29).
    jpeg = b"\xff\xd8" + b"\x11" * 64 + b"\xff\xd9"

    class File:
        def __init__(self):
            self.buffer = bytearray(jpeg)

        def get_data_and_size(self):
            return memoryview(self.buffer)

        def __del__(self):
            self.buffer[:32] = b"\x01" * 32

    class Handle:
        def capture_preview(self):
            return File()

    camera = GPhotoCamera()
    camera._camera = Handle()
    assert camera._frame() == jpeg

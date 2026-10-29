import sys
import types

import pytest

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

    fake = types.SimpleNamespace(
        Camera=Camera, GPhoto2Error=Exception, GP_ERROR_IO_USB_CLAIM=-53, GP_ERROR_TIMEOUT=-10,
        GP_ERROR_IO=-7, GP_ERROR_IO_USB_FIND=-52, GP_ERROR_MODEL_NOT_FOUND=-105,
    )
    monkeypatch.setitem(sys.modules, "gphoto2", fake)
    with pytest.raises(CameraError, match="not on USB .*Apple iPhone"):
        GPhotoCamera()._open()

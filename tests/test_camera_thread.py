import asyncio
import time

from fx30_mcp.camera import GPhotoCamera


def test_a_cancelled_call_finishes_its_thread_before_the_next_one_starts():
    spans: dict[str, tuple[float, float]] = {}

    def blocking(name: str, seconds: float):
        start = time.monotonic()
        time.sleep(seconds)
        spans[name] = (start, time.monotonic())

    async def go():
        camera = GPhotoCamera()
        camera._camera = object()  # already open
        first = asyncio.create_task(camera._run(blocking, "first", 0.3))
        await asyncio.sleep(0.05)
        first.cancel()
        second = asyncio.create_task(camera._run(blocking, "second", 0.0))
        await second
        assert first.cancelled()
        camera._idle_task.cancel()

    asyncio.run(go())
    assert spans["second"][0] >= spans["first"][1]


def _spans():
    spans: dict[str, tuple[float, float]] = {}

    def blocking(name: str, seconds: float):
        start = time.monotonic()
        time.sleep(seconds)
        spans[name] = (start, time.monotonic())

    return spans, blocking


def test_repeated_cancels_still_wait_for_the_thread():
    spans, blocking = _spans()

    async def go():
        camera = GPhotoCamera()
        camera._camera = object()
        first = asyncio.create_task(camera._run(blocking, "first", 0.4))
        await asyncio.sleep(0.05)
        first.cancel()
        await asyncio.sleep(0.01)
        first.cancel()
        await asyncio.create_task(camera._run(blocking, "second", 0.0))
        assert first.cancelled()
        camera._idle_task.cancel()

    asyncio.run(go())
    assert spans["second"][0] >= spans["first"][1]


def test_a_cancelled_anyio_scope_still_waits_for_the_thread():
    import anyio

    spans, blocking = _spans()

    async def go():
        camera = GPhotoCamera()
        camera._camera = object()
        scopes = []

        async def handler():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await camera._run(blocking, "first", 0.4)

        first = asyncio.create_task(handler())
        await asyncio.sleep(0.05)
        scopes[0].cancel()
        await asyncio.sleep(0.02)
        await asyncio.create_task(camera._run(blocking, "second", 0.0))
        await asyncio.wait([first])
        camera._idle_task.cancel()

    asyncio.run(go())
    assert spans["second"][0] >= spans["first"][1]


def test_a_cancelled_open_keeps_the_handle_so_it_is_closed_later():
    class Handle:
        exited = False

        def exit(self):
            self.exited = True

    handle = Handle()

    async def go():
        camera = GPhotoCamera(idle_timeout=0.05)

        def slow_open():
            time.sleep(0.2)
            return handle

        camera._open = slow_open
        first = asyncio.create_task(camera._run(lambda: None))
        await asyncio.sleep(0.05)
        first.cancel()
        await asyncio.wait([first])
        assert camera._camera is handle
        await asyncio.sleep(0.3)  # idle close
        assert handle.exited and camera._camera is None

    asyncio.run(go())


def test_a_wedge_that_ends_a_cancelled_call_drops_the_session():
    import gphoto2 as gp

    class Handle:
        exited = False

        def exit(self):
            self.exited = True

    handle = Handle()

    def times_out():
        time.sleep(0.2)
        raise gp.GPhoto2Error(gp.GP_ERROR_TIMEOUT)

    async def go():
        camera = GPhotoCamera()
        camera._camera = handle
        first = asyncio.create_task(camera._run(times_out))
        await asyncio.sleep(0.05)
        first.cancel()
        await asyncio.wait([first])
        assert first.cancelled() and handle.exited and camera._camera is None
        camera._idle_task.cancel()

    asyncio.run(go())

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

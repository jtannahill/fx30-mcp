"""fx30-mcp: a Sony FX30 and an amaran light, lit together for a talking head.

  fx30-mcp serve   run the MCP server (stdio)
  fx30-mcp check   connect to the camera, print its settings and meter one frame
  fx30-mcp props [filter]   print camera config values whose name or label contains the filter
"""

from __future__ import annotations

import asyncio
import json
import sys


async def _check() -> int:
    from . import meter
    from .camera import CameraError
    from .server import camera

    cam = camera()
    try:
        settings = await cam.settings()
        print(json.dumps({"camera": cam.name, "backend": cam.backend, **settings}, indent=2, default=str))
        reading = meter.read(await cam.frame())
        print(json.dumps(reading.summary(), indent=2))
    except CameraError as err:
        print(err, file=sys.stderr)
        return 1
    finally:
        await cam.close()
    return 0


async def _props(needle: str) -> int:
    from .camera import CameraError, GPhotoCamera

    cam = GPhotoCamera()
    try:
        for name, label, value in await cam.config_entries(needle):
            print(f"{name} [{label}] = {value}")
    except CameraError as err:
        print(err, file=sys.stderr)
        return 1
    finally:
        await cam.close()
    return 0


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "serve":
        from .server import serve

        serve()
    elif command == "check":
        sys.exit(asyncio.run(_check()))
    elif command == "props":
        sys.exit(asyncio.run(_props(sys.argv[2] if len(sys.argv) > 2 else "")))
    else:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

# fx30-mcp

Control a Sony FX30 from Claude Code and light a one-person talking head with an
amaran light. The camera records to its own card; this drives it over USB and
uses its live view to meter the face.

## The idea

For an interview, set the camera once and let the light do the work:

- Shutter at twice the frame rate (1/50 for 24p or 25p).
- ISO at a native base (800 or 2500 in S-Log3).
- Aperture for the depth of field you want. The FX30 has no built-in ND.

`balance_shot` then:

1. Sets the light and the camera's white balance to the same colour temperature.
2. Reads a live-view frame, finds the face with Apple Vision and meters the
   middle of it (cheeks, nose, forehead).
3. Works out how many stops the face is off the target and moves the light's
   brightness by that much, until it is within a sixth of a stop.
4. Reads the skin's hue the way a vectorscope does. Skin of every tone sits
   near one line on the scope, so a face pushed off it toward green or magenta
   gets corrected with the light's tint. If a tint step makes the cast worse,
   the light counts tint the other way: it flips and remembers.

If the light runs out of range, it says what to change instead: ISO, aperture,
or the light's distance (half the distance is two stops more).

## Tools

| Tool | What it does |
|---|---|
| `look` | Shows a live-view frame and meters the face against the target |
| `balance_shot` | Matches white balance, then lights the face to the target |
| `set_face_target` | Saves the face brightness and skin colour to aim for: light it how you like, then call it with no level |
| `match_white` | Light and camera to one colour temperature |
| `get_camera` / `set_camera` | ISO, shutter, aperture, white balance, recording state |
| `start_recording` / `stop_recording` | Record to the card |

## Setup

On the FX30: Menu > Network > PC Remote Function, turn PC Remote on and set the
connection method to USB. Plug it into the Mac. Quit Image Capture and Sony's
apps, since only one program can hold the camera.

```sh
uv sync
uv run fx30-mcp check          # connects, prints settings, meters one frame
claude mcp add -s user fx30 -- uv --directory ~/src/fx30-mcp run fx30-mcp serve
```

The light comes from [amaran-mcp](https://github.com/jtannahill/amaran-mcp)
(a sibling checkout in `../amaran-mcp`), through amaran Desktop when it is
running, otherwise over Bluetooth.

`FX30_BACKEND=sim` swaps in a pretend camera and light for trying the tools
without the gear. Settings live in `~/.config/fx30-mcp/settings.json`.

### Live view and log

`profile` says how live view encodes brightness. With Gamma Display Assist on
(or a Rec.709 picture profile) use `rec709`. If live view shows the flat S-Log3
picture, use `slog3`. The default face targets (0.60 and 0.50) are starting
points; `set_face_target` replaces them with a look you chose.

## Status

- Metering, the balancing loop and the tools are tested against the pretend
  camera (`uv run pytest`), and face finding against a real portrait.
- On the FX30 (Oct 27 2026, libgphoto2 2.5.34, Movie F Mode): it connects as a
  generic PTP camera, reads ISO, shutter, aperture and white balance, sets
  colour temperature (2500 to 9900K in 100K steps, read back to confirm) and
  streams 1024x576 live view. Movie recording is offered; not yet tried.
- ISO, shutter and aperture come through read-only in Movie F Mode, so set them
  on the camera. That fits the design: they stay fixed for an interview.
- macOS's `ptpcamerad` claims the camera when it is plugged in. Opening the
  camera stops it (launchd brings it back) and retries for a few seconds.
- Sony's Camera Remote SDK also lists the FX30, and would plug in as another
  `CameraBase` in `camera.py` if something here falls short.

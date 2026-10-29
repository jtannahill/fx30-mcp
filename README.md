# fx30-mcp

Control a Sony FX30 from Claude Code and light a one-person talking head with an
amaran light. The camera records to its own card; this drives it over USB and
uses its live view to meter the face.

## The idea

For an interview, set the camera once and let the light do the work:

- Shutter at twice the frame rate (1/50 for 24p or 25p).
- ISO at a native base (800 or 2500 in S-Log3).
- Aperture for the depth of field you want. The FX30 has no built-in ND.

`balance_shot` then works the key light alone (it never touches the camera):

1. Reads a live-view frame, finds the face with Apple Vision and meters the
   middle of it (cheeks, nose, forehead).
2. Works out how many stops the face is off the target and moves the key's
   brightness by that much, until it is within a sixth of a stop.
3. Reads the skin's hue the way a vectorscope does. Skin of every tone sits
   near one line on the scope, so a face pushed off it toward green or magenta
   gets corrected with the key's tint (`fix_tint=False` leaves the tint alone).

If the key runs out of range, it says how to move it instead (half the
distance is two stops more). `match_white` puts the camera and the lights on one
colour temperature. While the camera records, `balance_shot` leaves the light
alone.

## Tools

| Tool | What it does |
|---|---|
| `coach_shot` | Plans the camera and light for this room, sets up the shot with you from a page on your phone (QR code and link, also texted), then watches every take; saves what is on the key light |
| `shot_status` | What the coach is doing (setup, take, stopped), what the page shows, the take so far and the last take's report |
| `stop_coach` | Ends the coach and its page; camera and lights stay as they are |
| `look` | Shows a live-view frame and meters the face against the target |
| `balance_shot` | Lights the face to the target with the key light alone; never touches the camera |
| `set_face_target` | Saves the face brightness and skin colour to aim for: light it how you like, then call it with no level |
| `match_white` | Light and camera to one colour temperature |
| `set_lights` | The key light (on the face), the fill (on the shadow side) and the lighting ratio; with nothing given, lists the lights amaran Desktop has paired |
| `get_camera` / `set_camera` | ISO, shutter, aperture, white balance, exposure mode, recording state; set_camera reads each value back before reporting it |
| `start_recording` / `stop_recording` | Record to the card; a running coach watches the take and carries on after it |

## Shot coach

Call `coach_shot` with what is on the 100x S (bare, reflector, softbox, lantern or umbrella, and its size in inches for the last three). If the camera cannot report them, also give `frame_rate` (23.976 to 60; 24p plans on a 1/50 shutter) and `widest_aperture` (the lens's widest f-number); all of these are saved. It looks at the room with the lights off, makes a plan (card settings, which side the key goes, how far, how high) and serves a page to open on your phone over the local network. The page is behind a random key in the link and shows one instruction at a time, readable from 4 ft. Natasha texts you the link too.

There is nothing to tap. The coach puts the camera in M and sets ISO, shutter and aperture itself over USB, confirming each by readback. It turns the key on and checks which side of your face it lights, asks you to move it if it is on the wrong side or the catchlight is off, and sees for itself when you have moved it. Then it balances the key until the page says Ready to record.

Then press record, on the camera or with `start_recording`. During a take the coach holds what recording started with: the key follows your face in brightness and colour, slowly enough not to show, and the camera steps one third of a stop at a time (aperture first, then ISO, then shutter) when the window brightens or darkens. If the face and the window cannot both be held, the face wins and the window is let clip. If you change the camera by hand mid-take, the coach leaves the camera alone for the rest of that take. When recording stops, the page shows the take's report and Natasha texts it to you; the coach goes back to watching for the next take. `shot_status` gives Claude the same state and the last report. It also texts when the light stops answering, the camera battery runs low, the camera overheats, a card has a problem, or the coach stops on an error.

The coach runs until `stop_coach`, or 30 minutes with nothing recorded. `start_recording` and `stop_recording` only start and stop a take. For now the coach lights with the key only; a fill set with `set_lights` is left as it is.

The 100x S is reached over Bluetooth, and it takes one connection at a time. When a light is slow to answer the page says "Connecting to the light" (it can take up to two minutes). If it cannot connect when the coach starts, `coach_shot` says so: close the amaran or Sidus Link app on your phone, or turn off the phone's Bluetooth, then start the coach again. If the light drops later, the coach keeps trying and texts you once.

Without an ND filter the plan stops the lens down to f/11, then f/16, then raises the shutter through 1/125, 1/250 and 1/500, and tells you when a bright window still cannot be held. `balance_shot` refuses while the coach runs; `stop_coach` ends it.

## Setup

On the FX30: Menu > Network > PC Remote Function, turn PC Remote on and set the
connection method to USB. Plug it into the Mac. Quit Image Capture and Sony's
apps, since only one program can hold the camera.

```sh
uv sync
uv run fx30-mcp check          # connects, prints settings, meters one frame
claude mcp add -s user fx30 -- uv --directory ~/src/fx30-mcp run fx30-mcp serve
```

With several amaran lights paired in amaran Desktop, `set_lights` picks the key
(say a 100x S) and the fill (say a PT2c), and the lighting ratio `fill_ratio`
(1 is the classic 2:1 interview look, 2 is a moodier 4:1). `match_white` puts the
camera and both lights on one colour temperature. `coach_shot` and
`balance_shot` light with the key only for now and leave the fill as it is.

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
  streams 1024x576 live view. Start and stop recording work (a test left one
  clip on the card); both now wait for Sony's Movie Recording State to agree.
- In Movie F Mode shutter and ISO come through read-only and the shutter
  floats on Auto. The exposure mode (`expprogram`) is writable; in M (Oct 28
  2026) shutter, ISO and aperture are all writable over USB, and recording
  still starts and stops. Writes are taken at once but read back late (about a
  second), so each write is confirmed by polling the readback for up to 3 s.
- macOS's `ptpcamerad` claims the camera when it is plugged in. Opening the
  camera stops it (launchd brings it back) and retries for a few seconds.
- Sony's Camera Remote SDK also lists the FX30, and would plug in as another
  `CameraBase` in `camera.py` if something here falls short.

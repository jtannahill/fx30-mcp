# fx30-mcp

Controlling a camera and its lights through each manufacturer's own app is a
burden, no longer a luxury: one app to pair the light, another to remote the
camera, and you in front of the lens tapping between them. fx30-mcp is an attempt
to fold that gear into an office that already runs on voice and prompts, so the
shot is set up the way everything else is: by asking for it.

Set up and watch a one-person talking-head shot with a Sony FX30 and an amaran
light, from Claude Code. It drives the camera over USB, reads its live view, and
sets the camera and the light for you, then keeps your face lit through every
take.

## The use case

You film yourself at a desk with a window behind you. The last time, your face
came out dark and the window blew out. You did not know which side of the camera
the light belonged on, how far away, or what to set on the camera. That is the
hard part of any shot when you are alone: you are in front of the lens, not
behind it.

With fx30-mcp you ask Claude to set up the shot, and:

1. **It looks at the room** with the light off: where the window is, how bright,
   which way you are turned, and which side of your face the window already lights.
2. **It makes a plan**: which side of the camera the key goes on, how far, how
   high and aimed how far down, and the camera settings that keep the window in
   range. A page on your phone shows it as a top-down diagram and a side view.
3. **It sets the camera itself** (Manual, aperture, shutter, ISO, white balance)
   and the light (brightness, colour temperature, tint). The only thing it asks of
   you is moving the light, and only when no setting can fix what it sees. It sees
   in the frame when you have moved it. There is nothing to tap.
4. **It watches every take.** While you record, the light follows your face as
   daylight changes, slowly enough that a viewer never sees it, and the camera
   steps a third of a stop at a time when the window brightens or darkens, with
   the light moving the other way in the same moment so your face does not change.
   If the face and the window cannot both be held, the face wins.
5. **It reports after each take** on the page and, if you set it up, by text:
   "Take 2: 4 min 10 s. Face held throughout. Window clipped for 20 s at the end;
   the light was at full."

The page is meant to be read from your chair, about 4 ft away: one instruction in
big type, traffic lights for face and window, and a live preview of the shot that
updates about once a second.

## What you need

- A Sony FX30 on USB with PC Remote on (Menu > Network > PC Remote Function, PC
  Remote On, connection method USB). Other Sony bodies that libgphoto2 drives
  over PTP may work; only the FX30 has been tested.
- An amaran light reachable over Bluetooth (tested with a 100x S; the PT2c also
  works as a fill for `balance_shot`), through
  [amaran-mcp](https://github.com/jtannahill/amaran-mcp). The light takes one
  Bluetooth connection at a time: close the amaran or Sidus Link app on your
  phone, or turn off the phone's Bluetooth.
- A Mac. Face finding uses Apple's Vision framework.
- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- Claude Code (or any MCP client).

## Install

Clone this repository and amaran-mcp side by side (fx30-mcp uses `../amaran-mcp`):

```sh
mkdir -p ~/src && cd ~/src
git clone https://github.com/jtannahill/amaran-mcp
git clone https://github.com/jtannahill/fx30-mcp
cd fx30-mcp
uv sync
uv run fx30-mcp check          # connects, prints settings, meters one frame
claude mcp add -s user fx30 -- uv --directory ~/src/fx30-mcp run fx30-mcp serve
```

Set up amaran-mcp's light first (see its README). Quit Image Capture and Sony's
own apps before starting: only one program can hold the camera.

`FX30_BACKEND=sim` swaps in a pretend camera and light for trying the tools
without the gear. Settings live in `~/.config/fx30-mcp/settings.json`.

## Using it

Ask Claude to set up the shot. It calls `coach_shot` with what is on the light
(bare, reflector, softbox, lantern or umbrella, and its size in inches for the
last three; saved for next time) and hands you a QR code and a link to open on
your phone. If the camera cannot report them, it also asks for the frame rate
and the lens's widest aperture.

Then follow the page until it says **Ready to record**, and press record on the
camera (or ask Claude to). Record as many takes as you like: between takes the
coach goes back to watching. It runs until you ask Claude to stop it, or 30
minutes with nothing recorded.

### Tools

| Tool | What it does |
|---|---|
| `coach_shot` | Plans the camera and light for this room, sets up the shot with you from a page on your phone (QR code and link, also texted), then watches every take; saves what is on the key light |
| `shot_status` | What the coach is doing (setup, take, stopped), what the page shows, the take so far and the last take's report |
| `stop_coach` | Ends the coach and its page; camera and lights stay as they are |
| `look` | Shows a live-view frame and meters the face against the target |
| `balance_shot` | Lights the face to the target with the key light alone; never touches the camera, and leaves the light alone while recording |
| `set_face_target` | Saves the face brightness and skin colour to aim for: light it how you like, then call it with no level |
| `match_white` | Light and camera to one colour temperature |
| `set_lights` | The key light (on the face), the fill (on the shadow side) and the lighting ratio; with nothing given, lists the lights amaran Desktop has paired |
| `get_camera` / `set_camera` | ISO, shutter, aperture, white balance, exposure mode, recording state; set_camera reads each value back before reporting it |
| `start_recording` / `stop_recording` | Record to the card; a running coach watches the take and carries on after it |

### How a take is held

When recording starts, the coach takes a snapshot of your face's brightness and
colour and holds that through the take, rather than chasing a saved target. On
each frame (about one a second):

- **Face brightness** follows with the key's brightness, at most a sixth of a
  stop per second.
- **Warmth and tint** are read as two numbers from the skin (blue to amber, green
  to magenta). The key's colour temperature corrects the first, at most 100 K per
  second; its tint corrects the second, one step at most every 5 s. The camera's
  white balance never changes during a take.
- **The window** is kept in range by the camera: one third of a stop at a time,
  at most every 8 s, aperture first, then ISO, then shutter, each step paired with
  the opposite change on the key.
- **If you change the camera by hand**, the coach leaves the camera alone for the
  rest of that take. If the camera refuses a step, the same. The light carries on.
- **If your face leaves the frame**, nothing moves until it comes back.

During setup, a setting the camera refuses over USB becomes an instruction on the
page ("Set f/16 on the camera"), and the coach carries on once the camera reads
it back.

### Texts

The coach can text you the page link when it starts, the report after every take,
and warnings: the light stopped answering, the camera battery is low, the camera
is overheating, a card has a problem, or the coach stopped on an error. Warnings
of one kind are sent at most once every 10 minutes.

Texts go out through Twilio. Put your credentials in an env file and point
`FX30_TWILIO_ENV` at it (the default is `~/twilio-phone-tree/.env`):

```sh
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
MESSAGING_SERVICE_SID=MG...      # or TWILIO_PHONE_NUMBER=+1... to send from a number
MY_PHONE_NUMBER=+1...            # where the texts go
```

With no such file, or with `FX30_TEXTS=off`, nothing is sent. The token and phone
numbers are never logged.

### Live view and log

`profile` says how live view encodes brightness. With Gamma Display Assist on (or
a Rec.709 picture profile) use `rec709`. If live view shows the flat S-Log3
picture, use `slog3`. The default face targets (0.60 and 0.50) are starting
points; `set_face_target` replaces them with a look you chose.

## How it is built

One loop, the director (`director.py`), runs from setup through every take: read
the scene, decide, set, check.

| Module | Role |
|---|---|
| `scene.py` | One frame and the camera's settings into one description: face box and turn, face level, cheeks, skin colour, clipping, the window, the camera's own meter. One decode and one Vision request per frame |
| `decide.py` | A pure function from the scene, the rig's state, the goal and the mode (setup, balance, take) to the changes to make. All the judgment and all the take-mode limits live here, tested with tables |
| `rig.py` | The camera and the key light behind `state()` and `apply()`; records every camera value it writes, so a change by hand can be told apart |
| `director.py`, `report.py` | The loop, the page states, the after-take report and the warnings |
| `plan.py`, `survey.py` | Where the key goes and the camera card, from a frame with the lights off |
| `camera.py`, `sim.py` | libgphoto2 for the FX30, and a pretend camera and light for tests |
| `notify.py`, `web.py` | Texts over Twilio; the phone page (Starlette, a random key in the link) |

`uv run pytest` runs the suite against the pretend camera and light.

## Known limits

- **About one frame a second.** The FX30 sends a live-view frame over USB about
  every 0.8 s. That is plenty for slow lighting changes and sets the preview's
  pace. A faster loop or a smooth preview would need the camera's HDMI output
  through a capture device.
- **No ND filter.** Without one, a bright window is held by stopping down to
  f/16 and then raising the shutter; the page says when the window still cannot
  be held. A variable ND brings the shutter back to twice the frame rate.
- **One Bluetooth connection.** The light takes one connection; finding it can
  take a minute or two because it advertises rarely. During a run the coach holds
  the connection, so the amaran phone app cannot reach the light.
- **The camera can wedge.** If a program quits while it has the camera open, the
  FX30 waits for it: turn the camera off and on (PC Remote stays on).
- **Key light only.** The coach lights with one key; a fill set with `set_lights`
  is used by `balance_shot` and otherwise left as it is.

## Status

- Tested on the owner's FX30 (libgphoto2 2.5.34): live view at 1024x576,
  exposure mode M with aperture, shutter and ISO writable over USB (each write
  confirmed by readback, which lags about a second), colour temperature 2500 to
  9900 K, recording start and stop, and live view during recording.
- The shot coach reached Ready on real gear on 2026-10-29. The director (setup
  without taps, take monitoring, reports and texts) is tested against the pretend
  camera; its first hardware session is next, to confirm the camera meter's unit,
  measure how closely a paired light change lands against a camera step, and tune
  the colour gains.
- macOS's `ptpcamerad` claims the camera when it is plugged in. Opening the camera
  stops it (launchd brings it back) and retries for a few seconds.

## License

MIT.

# snapdet

**Finding every person in the frame — down to a few pixels.**

`snapdet` is a new, standalone person detector born from `hpose` (the
pose/view/stance brain of the Snappy AI project). `hpose` answers *who
is facing which way and doing what*; `snapdet` first answers the harder
question: *where is everybody, including the 5-pixel person in the
corner?* On CPU, at a few hundred milliseconds per frame, with a debug
overlay that shows every decision it made.

```
python -m snapdet live --source screen --radius 80   # overlay on screen, live
python -m snapdet image --source shot.jpg --out shot_dbg.jpg  # max-recall still
python -m snapdet bench                              # tune for your CPU
python -m snapdet.smoketest                          # acceptance tests
```

---

## Why the old pipeline cannot see small people

A person detector that letterboxes the whole frame to 640×640 throws
away the very pixels a small target lives in. Measured on real
photographs (measured during development on real photographs):

| person height, 1080p frame | after 640 letterbox | old pipeline | snapdet |
|---|---|---|---|
| 96 px | 32 px | **miss** | found (zoom sweep / inspection) |
| 48 px | 16 px | **miss** | found (zoom sweep) |
| 24 px | 8 px | **miss** | found (sweep z8, score ~0.7) |
| 8 px | <1 px | **miss** | found (motion + memory + zoom 12–16×) |
| 5 px | <1 px | **miss** | found when moving (motion + memory) |

And tiling alone is not enough: the COCO-trained `yolo11n-pose` has a
hard capability floor. Looking straight at a person through a zoomed
window, its best score is:

| person px | best zoom | score |
|---|---|---|
| 32 | 6–8× | 0.34–0.49 |
| 24 | 8× | **0.72–0.78** |
| 16 | 12× | 0.31–0.36 |
| 12 | 12× | 0.52–0.62 |
| 8 | 12× | 0.39–0.43 |
| 5 | 16× | 0.12–0.42 |

…while *empty* windows never score above ~0.09 at any zoom. So: the
model wants a person ~100–200 px tall in its input; tiny targets are
only scoreable through a strongly zoomed window; and at the bottom of
the table no single frame can be trusted at all — which is why snapdet
integrates evidence over time.

## Architecture

```
frame ─┬─ 1. COARSE   whole-frame letterbox pass          (≥ ~100 px people)
       ├─ 2. SWEEP    round-robin zoom-tile grids z8/z12   (stationary small people,
       │                a few tiles per frame, full coverage every few seconds)
       ├─ 3. MOTION   LK-flow + RANSAC affine compensation, frame difference,
       │                blob candidates (~9 ms)  →  ZOOM INSPECTION at the zoom
       │                the blob size calls for (≈192 px / height, clamped 3–16×)
       │              → weak-but-repeated evidence accumulates in the TEMPORAL
       │                MEMORY and is promoted when it confirms
       └─ fuse (size-aware NMS) → BRAIN (posefit + MLP heads; view/stance gated
                    to "unknown" below 48 px) → TRACKER (scale-aware gates,
                    velocity injection for sparse re-finds, long coast for gated)
```

Every stage runs inside a **hard budget of network passes per frame**
(`pass_budget`, default 10): 1 coarse + 4 sweep tiles + ≤4 inspections.
`bench` measures your machine and prints suggested budgets.

**Temporal evidence memory.** A 5–10 px person scores 0.05–0.45
depending on the frame; no threshold survives that. Instead, plausible
detections at a smoothly-moving location accumulate score (decay 0.97/
frame) until `memory_confirm` (0.45) with ≥ `memory_min_hits` (3)
sightings including one ≥ `memory_best_hit` (0.15). Noise blobs flicker
and drift; a walking person stacks up in a few frames.

## Circle mode: the aim disc (`--capture circle --radius 50..100`)

For a capture that only ever sees a disc of radius 50–100 px at the
exact screen centre. Two regimes live in one disc:

* **far people** — whole but tiny; the disc's own 4–6× upscale feeds
  them to the network large, so circle mode is *better* than frame
  mode for them (60 px person: score 0.83–0.91);
* **close people** — FRAGMENTS: head+shoulders, a chest, hips. A
  fragment floating in a small masked square scores ~0.00 with a
  COCO-trained model; the same fragment **flush with the bottom edge
  of a 640 canvas** scores 0.41–0.76, because that is exactly what a
  person cut off by the frame border looks like. Circle mode therefore
  runs three **context passes** (scales 1.0 / 0.6 / 0.45) per frame.

Circle-mode semantics:

* joints outside the disc are masked to zero confidence — invented
  keypoints cannot leak into features or the body fit;
* a detection is `truncated` unless it is fully inside the disc *and*
  its feet were actually seen; truncated tracks report
  **stance = unknown** (a chest cannot tell you what legs are doing)
  and view only when the head is in the picture;
* context hits ≥ `conf_context_strong` (0.55) seed a track at once;
  weaker ones (≥ 0.28) become temporal-memory evidence;
* a YuNet face (≥ 0.55, inside the disc) is a person even when the
  pose model got nothing — the face-fallback detection;
* the disc is an engagement area: at most `circle_max_tracks` (2)
  identities are kept, strongest first.

Measured on real photographs: chest fragments
found at every radius 50/80/100; head+shoulders found via coarse at
R≥80 when front-facing; hips-only fragments remain invisible to
appearance (motion blobs are their only path — a stationary hips-only
target inside the disc is the one true blind spot); empty discs on
native-sharp backgrounds produce zero tracks (on heavily *upscaled
blurry* backgrounds the model can hallucinate 0.3–0.5 — if your
capture is a stretched low-res buffer, raise `conf_context`).

Known limit: when two bodies cross inside the disc (a giant sweeping
over a far person), identities can exchange — the classic occlusion
ID-switch; there is no appearance embedding at this size yet.

## Live mode and real time (no recording, ever)

```
pip install mss              # portable screen capture (Win/mac/X11)
pip install dxcam            # Windows Desktop Duplication, ~1 ms grabs

python -m snapdet live --source screen --backend dxcam --radius 80
python -m snapdet live --source screen --radius 80 --publish udp://127.0.0.1:5005
python -m snapdet live --source synth --view window    # fallback viewer
```

`live` puts the debug overlay **on your screen, live**: a transparent,
borderless, always-on-top window (tkinter colourkey on Windows,
opaque-black fallback elsewhere) sitting exactly over the captured
region at 1:1 scale - aim disc, per-source detection boxes, motion
blobs, rejections, memory candidates, track boxes with labels, the
assist crosshair, a two-line HUD.  The engine runs in a worker thread
and the overlay polls its latest state at ~100 Hz, so drawing never
waits on a network pass.  Nothing is encoded or written to disk; the
only optional file is the `--json` track log.

* `--radius 50..100` implies circle mode and makes the screen source
  grab only the centre square of side 2R - capture cost scales with
  the aim region, not the screen;
* the **capture thread keeps only the newest frame** - a queue would
  be latency, and a tracker wants the present, not the past;
* the **budget controller** holds `--target-fps`: every 3 s (or 15
  frames) it compares the median frame cost with the target and moves
  the pass budget, proportionally when badly over.  Fewer passes =
  shallower sweep/inspection that frame; tracker and temporal memory
  bridge the gaps.  History is printed at shutdown;
* mouse left button **on the overlay window** is the operator
  authorisation: click = magnetised `select`, hold = streaming `hold`,
  release = `release` (see the events section);
* real-time levers, biggest first: `--device directml` (any DX12 GPU)
  or `cuda`; a 320 px ONNX export via `tools/export_fast.py`;
  `--threads N`; a smaller `--radius`.

Honest expectation: achievable fps = your hardware's per-pass cost
divided into the budget.  A modern desktop CPU (~15-30 ms/pass) lands
at 30-60 fps with budget 6-10 in circle mode; a GPU through DirectML
makes the budget almost free; a slow VM degrades gracefully and says
so.  `bench` measures your machine and prints suggestions.

## Operator selection and robot integration

The operator stays in the loop.  In the `--show` window:

* **click** near a detected person (within `--select-radius`, default
  48 px) and the click magnetises to that track: a `select` event
  carries the assist point (track centre), the raw click, track id,
  view/stance/height and the detection source;
* **hold mouse1** and `hold` events stream every frame with the
  assisted point re-picked against the moving target;
* **release** emits `release`.  Clicks on empty space emit `select`
  with `track_id=-1` - the operator's "nothing there" is data too.

Events go to in-process callbacks and, with
`--publish udp://host:port`, out as JSON datagrams:

    python -m snapdet live --source screen --show \
           --publish udp://127.0.0.1:5005 --capture circle --radius 80
    python tools/robot_consumer.py --listen 127.0.0.1:5005

The consumer example marks the two honest hook points: your robot
command (gimbal/arm/camera) and, if the consumer is your own
teleoperation UI, your own cursor magnet inside your own window.
snapdet never moves the OS cursor and injects no input anywhere -
see *Out of scope* above.

## The debug overlay

`--out` writes a video where every decision is drawn (see
`snapdet/debug.py`):

| colour | meaning |
|---|---|
| grey thin box `z8`/`z12` | sweep window inspected this frame |
| yellow box | motion-compensated difference blob (candidate) |
| red box `rej 0.07` | candidate the zoom inspection rejected |
| magenta box | detection from motion-gated inspection |
| blue box | detection from the zoom sweep |
| green box | detection from the coarse pass |
| cyan box / circle | promoted by temporal memory / live memory candidate with `acc/hits` |
| orange box | detection from a circle-mode context pass |
| white box + label | track: `#id view/stance h<px>`, velocity arrow, lead cross (+150 ms) |
| skeleton | keypoints — only when the body passed the size gate |

HUD top-left: frame ms, passes used, tracks (confirmed), detections,
motion ROIs, rejections, memory size, sweep progress per grid.
`--json` writes the same story as machine-readable records.

## Honest limits (v0.1)

* **5 px and static, camera static**: motion finds nothing and the
  sweep needs its full cycle (z12 grid ≈ every 10–40 s at default
  tiles/frame). It *will* be found eventually; it will not be instant.
* **False positives**: with `conf_floor=0.05` the memory path can
  promote texture that behaves like a person. The debug overlay exists
  exactly for this; raise `memory_best_hit` / `memory_confirm` if your
  footage is noisy.
* **View/stance below 48 px is `unknown`** — deliberately. At 8 px the
  17 keypoints span ~3 px; any label there would be a lie.
* **Speed**: this is CPU ONNX; every stage is a 640×640 pass (~40–140 ms
  depending on your cores). Budget it. GPU/DirectML would lift the
  budget 10–30×.
* The shipped brain was trained on photographic humans; stylised game
  graphics may need the fine-tuning path below.

## Configuration knobs (`snapdet/config.py`)

| key | default | what it trades |
|---|---|---|
| `pass_budget` | 10 | latency vs. coverage per frame |
| `inspect_budget` | 4 | of those, inspections of motion blobs |
| `sweep_zooms` | `((8,3),(12,1))` | grids: zoom × tiles/frame; full-coverage period |
| `conf_coarse/sweep/sweep_far/inspect` | .25/.20/.12/.12 | per-path confidence floors |
| `conf_floor` | 0.05 | network output floor (memory lives below the others) |
| `memory_confirm/min_hits/best_hit` | .45/3/.15 | FP-resistance of temporal promotion |
| `min_classify_height` | 48 | where view/stance honestly stop |
| `track_max_age` | 20 | coast frames (gated tracks: ×2) |

## Roadmap

1. **Screen-capture source** (`mss`/D3D shot) behind `--source screen`.
2. **Model upgrade slot**: `Config.pose_model` already takes any
   YOLO-pose ONNX. The real fix for <8 px is a detector *trained* on
   tiny people — re-export `yolo11n-pose` with a P2 head at 1024, or
   fine-tune on VisDrone/CrowdHuman (drone pedestrians are exactly this
   size class). The engine needs no change: swap the file, the zoom
   ladder keeps working and can then relax.
3. **INT8 static quantisation** of the pose ONNX (~2× on CPU).
4. **Homography warp** instead of partial affine for rotating game
   camera pans (fewer compensation residuals on the horizon).
5. WebRTC/UDP track output for the Snappy AI app side.

## Layout

```
snapdet/
  config.py      every knob
  engine.py      orchestrator + Brain + CandidateMemory
  multiscale.py  zoom-tile sweep scheduler, size-aware fusion
  inspector.py   adaptive-zoom window inspection
  motionroi.py   motion-compensated blob candidates
  tracker.py     association gates, velocity injection, coasting   (from hpose, fixed)
  debug.py       the development overlay
  cli.py         live / image / bench
  capture.py     screen (dxcam, mss), video and synthetic sources
  live.py        threaded real-time loop + adaptive budget controller
  overlay.py     transparent on-top overlay window (vector drawing)
  smoketest.py   acceptance tests incl. the 8-px panning scenario
  events.py      operator select/hold/release events + UDP event bus
  keypoints.py detector.py model3d.py posefit.py features.py
  classifier.py bundle.py view.py stance.py motion.py
                 — the hpose brain, vendored (inference only)
  snapdet_bundle.npz   trained view/stance heads (91.3% / 91.6%)
  models/              yolo11n-pose + YuNet (auto-download, self-healing)
tools/           robot_consumer.py (example event consumer),
                 export_fast.py (320 px ONNX re-export recipe)
```

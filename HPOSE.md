# Human detection - pose, view and motion analysis

The computer-vision stack inside Snappy AI that answers the questions
the classical scanner in `vision.py` can only guess at about the
people on the screen:

* **who is there** - person detection with 17 body joints
* **which way are they facing** - front, back, side profile, or turned
  through 45 degrees
* **what are they doing** - standing, walking, crouching, sitting,
  prone, or in the air
* **where are they going** - velocity, direction, and where they will
  be a moment from now

It reads pixels and returns geometry. This document describes how it
works; the module map lives in the README.

---

## Install

```
python -m pip install -r requirements.txt
```

`opencv-python` and `onnxruntime` are the only additions to what the
app already needs. Without them the app starts normally and the
classical scanner handles everything - human detection switches itself
off and says so once in the log.

## Layout

The pipeline is ordinary app code in the flat `hpose_*` modules at the
project root, with the weights under `models/`:

```
project/
  hpose_bridge.py        the app side: engine lifecycle, target choice
  hpose_analyzer.py      the high level facade (detect + explain)
  hpose_detector.py      YOLO-pose and YuNet inference, model download
  hpose_posefit.py       the geometric body fit
  hpose_features.py      keypoints to a feature vector
  hpose_classifier.py    the two heads, numpy-only at inference
  hpose_bundle.py        the trained artefact: one .npz file
  hpose_view.py          the eight body views
  hpose_stance.py        the five body states
  hpose_motion.py        the Kalman filter and the motion model
  hpose_tracker.py       identity, smoothed labels, lead prediction
  hpose_model3d.py       the parametric 3D skeleton and virtual camera
  hpose_keypoints.py     the 17-joint vocabulary, pose container
  models/
    yolo11n-pose.onnx               12 MB  - finds people and joints
    face_detection_yunet_2023mar.onnx  0.2 MB  - finds faces
    hpose_bundle.npz                109 KB - the trained heads
```

The models are resolved relative to the project root, so a copy of
the project works on another machine with nothing else set up. Two
environment variables override the paths when they need to:
`HPOSE_CACHE` for the models and `HPOSE_BUNDLE` for the heads. A
frozen build also accepts a `models/` folder beside the executable.

If a model is missing it is downloaded into `models/` the first time
it is needed, so the layout is self-healing after a partial copy.

Check it at any time with:

```
python build_tools.py hpose        # files present, packaging OK
python hpose_check.py              # 40 unit checks, any platform
python hpose_check.py <pictures>   # + 9 integration checks, real scan path
```

## Quick start

The app never talks to the pipeline directly - `hpose_bridge` owns the
engine and translates its answers into the targets `vision.scan_region`
already returns. From code, the facade is:

```python
import cv2
from hpose_analyzer import Analyzer

analyzer = Analyzer()

frame = cv2.imread("photo.jpg")
faces = analyzer._face_boxes(frame)
for pose in analyzer.pose_detector.detect(frame):
    det = analyzer.explain(pose, focal_px=frame.shape[1] * 0.9,
                           faces=faces, img=frame)
    print(det["view"],        # 'front', 'side_left', 'back_right_45', ...
          det["stance"],      # 'standing', 'walk', 'crouch', 'prone', 'jump'
          det["box"], det["score"])
```

Frame-to-frame tracking (identity, smoothed labels, velocity, lead
prediction) lives in the caller: `hpose_bridge.Engine` runs a
`hpose_tracker.Tracker` in **screen coordinates**, because the
captured window follows the cursor and a window-local tracker would
lose every target the moment the mouse moved.

---

## How it works

```
 frame ──► YOLO-pose ──► 17 joints + box
                │
                ├──► YuNet face ──► "is the face turned towards us?"
                │
                ▼
         geometric body fit ──► how far the body is turned (yaw)
                │                  how well a rigid body explains it
                │                  repaired positions for missing joints
                ▼
         feature vector ──► view head  ──► front / back / side / 45
                            stance head ──► standing / walk / crouch / prone / jump
                │
                ▼
         tracker ──► identity, smoothed labels, velocity, lead prediction
```

### The geometric body fit

The view of a body is written all over its silhouette.  When a person
turns, the shoulders and hips foreshorten by the cosine of the angle,
the nose swings to one side, and the far limbs tuck behind the near
ones.  `hpose_posefit.py` fits a canonical 3D skeleton to the observed
joints and reads the turn straight off it.

The camera model is perspective, because a person filling the frame is
strongly distorted by it:

```
u = u0 + f * X / (D + Z)
v = v0 + f * Y / (D + Z)
```

`D` is not a free parameter.  The apparent size of a body and its
distance from the camera are the same fact seen twice, so `D` is
searched around the value implied by the observed body height and the
focal length.  Leaving it free lets a wildly wrong pose be "explained"
by a wildly wrong perspective, which is exactly the mistake that makes
a naive fitter useless.

For a fixed yaw the model is linear in the three remaining unknowns,
so a whole grid of angles is solved as one batched least-squares
problem.  A second parameter, the torso lean, is fitted at the same
time: nobody stands like a mannequin, and a fit that assumes a rigid
upright body is wrong by as much as the lean.

Two things the fit has to be careful about:

* **Proportions.**  A generic template and a real body rarely share
  the same measurements.  The template is reshaped to match the
  observed torso length, using anthropometric ratios to work out the
  shoulder width - deliberately *not* the shoulder width in the image,
  because in the image it is foreshortened by the very angle being
  measured.
* **Mirror symmetry.**  A body seen from behind is symmetric, so
  nothing in the silhouette says whether the person is turned left or
  right.  The geometric answer is therefore spread over both and
  given less weight, and the left/right call is left to the learned
  head, which sees the lopsidedness of the occluded limbs.

### The face detector

A pose detector keeps reporting the nose and eyes even when the face
is hidden, so keypoint confidence alone cannot say whether a person is
looking at the camera.  YuNet can: a face in the head region means the
body is turned less than about seventy degrees towards the camera.
Its verdict chooses which branch of the mirror the body fit searches.

The head is cut out and enlarged before it is searched, because a face
in a wide shot is a handful of pixels across.  When that was not done,
a quarter of the people facing the camera were called "seen from
behind".

* **A face detector that does not complain.**  OpenCV's
  `FaceDetectorYN` asks the network which backend to use, and OpenCV 5
  runs on a graph engine where that request does nothing - so it
  prints a warning every time it is built, even though the detector
  runs on the CPU either way.  The log level is dropped just for that
  one call and put straight back, so no other OpenCV message is
  hidden.

### The heads

Two small heads are trained on a feature vector of 135 numbers:
where the joints are relative to the hips, how confidently each one was
seen, the geometry of the spine and limbs, the fit's own measurements,
and the evidence that the face is turned towards the camera.

They were trained on **synthetic bodies**, not photographs.  A
parametric 3D model was posed, dropped into a virtual camera and
degraded the way a real detector degrades its output - joints jittered,
hidden joints reported with low confidence or not at all, limbs
occasionally invented, bodies cut off at the frame edge, boxes that
stop at the knees.  Because the pose parameters *are* the label, a
hundred thousand labelled bodies cost a few seconds, and the labels are
perfect rather than approximate.

Two physical rules overrule the learned head as a last word:

* **A face means the body is not turned away.**
* **Straight knees mean the person is not crouching.**  A person with
  straight legs and an upright body is standing, whatever the box
  around them says.  A bent knee and an upright body mean the opposite.

## Confidence: two witnesses

A box score alone is the wrong gate at these magnifications.  A 20 px
person letterboxed into the network scores 0.15-0.35 with a perfectly
good skeleton behind it, while a car mirror the network dresses up as
a body can score 0.6 with none.  So every detection carries two
numbers: the box score and ``kp_conf``, the mean confidence of its four
torso joints - the one thing a person cannot hide and a hallucination
rarely sustains.

* The detector's own keep-floor is 0.15, far below any user setting;
  a weak box is kept when its skeleton vouches for it (score >= 0.20
  with kp_conf >= 0.6).
* Everything downstream works with the **fused confidence**
  ``max(score, (score + kp_conf) / 2)`` - two independent witnesses,
  either one being sure is enough.  The user's "how sure a person must
  be" gate applies to that fused number.
* A detection with no skeleton and no confident box (kp_conf < 0.25
  and score < 0.5) is dropped as texture.
* Boxes sitting inside a better-scoring box are the same person twice
  (a head beside its body) and are suppressed before the tracker, so
  one person never becomes two tracks.
* When the whole-window pass finds nobody and the scan budget still
  has time, a second pass magnifies the centre of the window (half,
  then a third) for windows of 120 px and up - where a small person is
  below the detector's floor at window scale and above it at crop
  scale.  Tiny windows skip it: their letterbox already magnifies
  enough, and extra zoom there hallucinates more than it finds.

Measured with the composite benchmark (real person crops pasted at a
known size into person-free photo windows of exactly the size the app
captures, `side = 2*(max(48, radius)+12)+1`, run through the real
`vision.scan_region` path, two frames per sample; harness in
`bench/hpose_bench.py`, the 200-sample radius-70 test in
`bench/hpose_bench70.py`):

| search radius | person size | aim accuracy |
|---|---|---|
| 20-70 px | 40-140 px | 100% in every cell |
| 10 px | 20 px | 73% |
| contract pairing (h = 2R), 105 samples | | 96.2% |

hpose itself answers 95% of those samples; the remainder are answered
by the classical scanner's documented fallback.

### The 20-30 px wall

A 20-30 px person is the honest capability floor of the shipped
detector, at every radius and every mechanism tried.  The acceptance
test for this regime - 200 samples at search radius 70, person height
uniform in 20-30 px, person-free photo backgrounds, random colours
(`bench/hpose_bench70.py`) - measures **67.5%**, against a requested
95%.  The reason is source information, not gating: a 20 px body is
~80 px of upscaled blur in the network's 640 px input at window scale
and ~180 px in a magnified centre crop, and the detail simply is not
in those pixels.  The published zoom table of the author's own
tiny-person engine (snapdet, in this repo's history) agrees: 24 px
people score 0.72-0.78 at their best zoom, 16 px people 0.31-0.36 -
misses included, on photographs without colour jitter.

A last-resort **blob watcher** (`vision._blob_watch`, setting
*blob_watch*) looks for the one thing such a person still is: a
high-contrast ink blob about twice as tall as wide, 14-64 px, on a
2x-magnified copy of the window with a colour-aware contrast metric
(per-channel maximum, not the luma-weighted classical diff).  It runs
only when human detection is on and both the neural path and the
classical scanner came up empty.  Measured contribution on the
photographic composite: +1 sample in 200 - the blind class defeats
geometry as surely as it defeats the network, because a 20 px smear's
contrast profile overlaps the clutter's.  It stays because real game
frames render crisp, high-contrast silhouettes that photographic
downscaling destroys - exactly the watcher's design target - and
because it costs nothing on scans that already have an answer.

Measured and rejected for this regime: letterbox interpolation
changes (no effect), torso-midpoint aiming (worse than box centre),
fit-quality gating (does not separate classes), always-on crop passes
(centre-crop hallucinations outcompete the target: 16% accuracy vs
60%), wide skeleton rescue bands without confirmation (same), the
blob watcher ahead of the classical scanner (its clutter blobs steal
correct classical answers: 61-64% vs 67%), looser blob bands and 3x
magnification (same), and 8-frame temporal accumulation (the misses
are deterministic, not stochastic - frames-to-detect is 1 when
anything is detectable).  What would actually move this number: a
detector trained or exported for tiny people (a P2 head at 1024, or
fine-tuning on VisDrone/CrowdHuman - the snapdet roadmap), or
snapdet-grade seconds-long temporal+motion evidence.  Neither is
buildable in this environment: no torch, no GPU, and Ultralytics no
longer publishes the larger pose ONNX.

Note for real deployments: the benchmark down-scales photographic
people, which is blurrier than a rendered game model at the same
height - crisp silhouettes at 20-30 px detect better than these
numbers.  `bench/hpose_bench70.py` is the acceptance harness to run
against real captures.

## How good is it

Measured on 48 COCO photographs whose people were labelled by hand,
and on a held-out set of generated bodies that no head ever saw.

| | generated bodies | real photographs |
|---|---|---|
| body view | 91% exact | 44% exact, 84% within 45 degrees |
| body state | 92% exact | 38% exact |
| moving target | velocity exact to 0.1 px/s | - |
| body fit (yaw) | 16 degrees median | - |

Two things are worth knowing about those numbers.

**The body view is the strong part.**  On a photograph it is right
within 45 degrees for 84 out of 100 people, and the mistakes it makes
are all in the same direction - calling somebody square on to the
camera "turned 45 degrees" rather than the other way round.  The
answer is also stable: it is smoothed over the frames of a video, so
it does not flicker.

**The body state is the weak part, and it is weak for a reason.**  A
photograph of a person holding a bat, sitting in an armchair or
standing in the snow does not always contain the information needed
to say what they are doing.  It is right about two times in five, and
it is *honest* about the rest: the confidence falls when the picture
does not say.  Three physical rules do most of the work here - the
knees say whether a person is crouching, the spine says whether they
are lying down, and a stride says whether they are walking - and the
learned head only breaks the remaining ties.

In a video it does better than that, because the tracker knows a body
that is covering ground is walking, whatever the picture alone says.

## In the main app

`vision.scan_region` gained one stage: when human detection is
switched on and the detector finds somebody who matches the filters,
that person is the target; when it finds nobody, the classical scanner
runs exactly as before.

Three things make that safe to leave on:

* **It never breaks the program.**  The pipeline needs opencv,
  onnxruntime and the files under `models/`.  When they are missing
  the bridge says so once in the log and switches itself off.  The
  heavy libraries are not imported until they are needed, so the
  program starts normally without them.
* **It never blows the scan budget.**  The models load on a background
  thread when the program starts, so the first scan does not pay for
  it.  A scan that has run out of time falls back to the classical
  scanner.
* **It tracks in screen coordinates.**  The scan region is a square
  that follows the cursor, so every frame is a different window onto
  the screen.  The detections are moved onto the screen before they
  reach the tracker, so identities survive the cursor moving.

The settings, all under *Experimental* except the two numbers:

| setting | what it does |
|---|---|
| Human detection | Off, or On. On means hpose answers first. |
| Which way the person must face | Any, facing the screen, turned away, side profile |
| Which posture the person must have | Any, standing, crouching, prone, walking, low |
| Where to aim on the person | Middle of the body, or head |
| Person detector | Small and fast, or medium and sharper |
| How far ahead to lead a person | Milliseconds of travel (0-300) |
| How sure a person must be | The fused box+skeleton confidence, 10-90 |

The cost is about 25 ms a scan - 80 ms with the detector on against
56 ms without - which sits comfortably inside the scan budget.

`python hpose_check.py` verifies everything: the unit layer checks the
geometry, the feature vector, the bundle, the motion model and the
tracker on any machine; the integration layer runs the real scan path
over pictures with people in them and checks that the classical
scanner still answers with detection off, that hpose answers with it
on, that the filters are obeyed, that the aim point lands on the
person and that a moving person is measured and led.

## Training and evaluation

The shipped heads are the artefact: `models/hpose_bundle.npz`, a
plain `.npz` of float32 arrays that loads in a millisecond and needs
nothing but numpy at run time. Retraining is not part of the app.

The generator, trainer and evaluation tools (synthetic body generator,
torch training with self-training rounds on real photographs, COCO
validation, confusion-matrix reporting, the standalone CLI) were
removed from this tree when the pipeline was folded into the app;
they remain in the git history under `hpose/` (up to commit
`08a7460`) together with the hand-labelled real-photo set. The unit
checks that the old self test carried live on in `hpose_check.py`.

## Building a frozen copy

The `hpose_*` modules are ordinary app code and PyInstaller finds
them on its own, but the files under `models/` are data rather than
code and have to be copied into the build. The arguments needed are
printed by:

```
python build_tools.py hpose --args
```

A frozen build looks for the models beside the executable first, so a
portable layout survives packaging unchanged. Without the models the
frozen program still starts and simply keeps the classical scanner.

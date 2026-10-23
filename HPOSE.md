# hpose - human pose, view and motion analysis

A self-contained computer-vision stack that answers the questions you
actually have about people in a picture or a video frame:

* **who is there** - person detection with 17 body joints
* **which way are they facing** - front, back, side profile, or turned
  through 45 degrees
* **what are they doing** - standing, walking, crouching, sitting,
  prone, or in the air
* **where are they going** - velocity, direction, and where they will
  be a moment from now

Nothing in it is tied to a particular game, a particular screen or a
particular input device.  It reads pixels and returns geometry.

---

## Install

```
python -m pip install -r hpose/requirements.txt
```

`onnxruntime` is the only addition to what the app already needs.
`torch` is only needed to *train* the heads; inference is numpy alone.

## Everything lives in the project

Nothing is loaded from anywhere else.  The whole of it is in this
folder:

```
project/
  hpose/                              the package
    __init__.py  analyzer.py  ...     27 modules
    models/                           the model weights
      yolo11n-pose.onnx               12 MB  - finds people and joints
      face_detection_yunet_2023mar.onnx  0.2 MB  - finds faces
    hpose_bundle.npz                  109 KB  - the trained heads
    requirements.txt
  hpose_bridge.py                     the bridge into the app
  hpose_check.py                      the integration check
```

The models are used from `hpose/models/` and the trained heads from
`hpose/hpose_bundle.npz`, both resolved relative to the code - so a
copy of this folder works on another machine with nothing else set up.

Two environment variables override the paths when they need to:
`HPOSE_CACHE` for the models and `HPOSE_BUNDLE` for the heads.  They
are only overrides; the defaults are already inside the project.

If a model is missing it is downloaded into `hpose/models/` the first
time it is needed, so the folder is self-healing after a partial copy.

Check it at any time with:

```
python build_tools.py hpose
```

### Building a frozen copy

The package is ordinary Python and PyInstaller finds it on its own, but
the model files are data rather than code and have to be copied into
the build.  The arguments needed are printed by:

```
python build_tools.py hpose --args
```

A frozen build looks for the models beside the executable, so the
layout survives packaging unchanged.  Without the models the frozen
program still starts and simply keeps the classical scanner.

## Quick start

```python
import cv2
from hpose.analyzer import Analyzer

analyzer = Analyzer()

for track in analyzer.analyze(frame, t=timestamp):
    print(track.id,
          track.view,          # 'front', 'side_left', 'back_right_45', ...
          track.stance,        # 'standing', 'walk', 'crouch', 'prone', 'jump'
          track.motion_state,  # 'static', 'slow', 'fast'
          track.velocity,      # pixels per second
          track.center)
```

`analyze` keeps every track alive from frame to frame, so the answers
are steady rather than flickering.  For a picture that does not belong
to the same scene as the last one, pass `reset=True`.

Command line:

```
python -m hpose.cli image  --source photo.jpg --out annotated.jpg
python -m hpose.cli detect --source clip.mp4  --out clip_annotated.mp4 --json out.json
python -m hpose.cli webcam --out webcam.mp4
python -m hpose.cli train  --samples 30000 --real-dir photos/
python -m hpose.cli demo   --image photo.jpg --frames 60
python -m hpose.cli eval-real --photos photos/
python -m hpose.cli info
```

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
ones.  `hpose/posefit.py` fits a canonical 3D skeleton to the observed
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
body is turned less than about seventy degrees towards the camera.  Its
verdict chooses which branch of the mirror the body fit searches.

### The heads

Two small heads are trained on a feature vector of 135 numbers:
where the joints are relative to the hips, how confidently each one was
seen, the geometry of the spine and limbs, the fit's own measurements,
and the evidence that the face is turned towards the camera.

They are trained on **synthetic bodies**, not photographs.  A parametric
3D model is posed, dropped into a virtual camera and degraded the way
a real detector degrades its output - joints jittered, hidden joints
reported with low confidence or not at all, limbs occasionally
invented, bodies cut off at the frame edge, boxes that stop at the
knees.  Because the pose parameters *are* the label, a hundred
thousand labelled bodies cost a few seconds, and the labels are
perfect rather than approximate.

Training on clean geometry would teach the classifier to expect
perfection, which does not survive the first real frame.

---

## How good is it

Measured on 48 COCO photographs whose people were labelled by hand
(`hpose/real_labels.py`), and on a held-out set of generated bodies
that no head ever saw.

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

**Where the accuracy comes from.**  The face detector is what makes
the view reliable: a face can only be seen by a camera the body is
turned towards, so finding a face settles front against back
outright.  The head is cut out and enlarged before it is searched,
because a face in a wide shot is a handful of pixels across.  When
that was not done, a quarter of the people facing the camera were
called "seen from behind".

## In the main app

The `hpose` package is standalone, but it is also wired into Snappy AI
as an optional second opinion.

`hpose_bridge.py` loads the package on demand and translates its
answers into the shape the rest of the program already understands.
`vision.scan_region` gained one stage: when the human detection is
switched on and the detector finds somebody who matches the filters,
that person is the target; when it finds nobody, the classical scanner
runs exactly as before.

Three things make that safe to leave on:

* **It never breaks the program.**  hpose needs onnxruntime and two
  model files.  When they are missing the bridge says so once in the
  log and switches itself off.  Nothing is imported until it is
  needed, so the program starts normally on a machine that has never
  heard of hpose.
* **It never blows the scan budget.**  The models load on a background
  thread when the program starts, so the first scan does not pay for
  it.  A scan that has run out of time falls back to the classical
  scanner.
* **It tracks in screen coordinates.**  The scan region is a square
  that follows the cursor, so every frame is a different window onto
  the screen.  A tracker that worked in the window's own coordinates
  would lose every target the moment the cursor moved, so the
  detections are moved onto the screen before they reach the tracker.

The settings, all under *Experimental* except the two numbers:

| setting | what it does |
|---|---|
| Human detection | Off, or On. On means hpose answers first. |
| Which way the person must face | Any, facing the screen, turned away, side profile |
| Which posture the person must have | Any, standing, crouching, prone, walking, low |
| Where to aim on the person | Middle of the body, or head |
| Person detector | Small and fast, or medium and sharper |
| How far ahead to lead a person | Milliseconds of travel (0-300) |
| How sure a person must be | The detector confidence, 10-90 |

The cost is about 25 ms a scan - 80 ms with the detector on against
56 ms without - which sits comfortably inside the scan budget.

Run `python hpose_check.py` to verify the wiring: it checks that the
classical scanner still answers with the detector off, that the
detector answers with it on, that the filters are obeyed, that the aim
point lands on the person and that a moving person is measured and led.

* **A face detector that does not complain.**  OpenCV's `FaceDetectorYN`
  asks the network which backend to use, and OpenCV 5 runs on a graph
  engine where that request does nothing - so it prints a warning
  every time it is built, even though the detector runs on the CPU
  either way.  The log level is dropped just for that one call and
  put straight back, so no other OpenCV message is hidden.

## Files

| file | what it holds |
|---|---|
| `keypoints.py` | the 17-joint vocabulary, pose container, box helpers |
| `model3d.py` | the parametric 3D skeleton and the virtual camera |
| `synth.py` | the synthetic body generator and its degradation model |
| `posefit.py` | the geometric body fit: yaw, lean, depth, repaired joints |
| `features.py` | keypoints to a feature vector |
| `classifier.py` | the two heads, numpy-only at inference |
| `bundle.py` | the trained artefact: one `.npz` file |
| `view.py` | the eight body views |
| `stance.py` | the five body states |
| `heuristics.py` | rule based fallbacks, used to label real photographs |
| `motion.py` | the Kalman filter and the motion model |
| `tracker.py` | identity, smoothed labels, lead prediction |
| `detector.py` | YOLO-pose and YuNet inference, model download |
| `analyzer.py` | the high level API |
| `train.py` | training the heads, including self training on real pictures |
| `evaluate.py` | measuring accuracy against a labelled set |
| `eval_real.py` | scoring against the hand labelled photographs |
| `real_labels.py` | the hand written labels for those photographs |
| `validate_coco.py` | checking against the COCO keypoint annotations |
| `diagnose.py` | comparing a photograph with the training data |
| `makecoco.py` | building a real-photo evaluation set from COCO |
| `demo.py` | watching a moving target being tracked |
| `render.py` | drawing stick figures and contact sheets |
| `selftest.py` | the self test |

## The trained heads

The heads are trained on generated bodies and then taught about real
photographs:

```
python -m hpose.train --samples 34000 --real-dir photos/ --real-rounds 2
```

The result is written to `hpose/hpose_bundle.npz` - 109 KB of float32
arrays that load in a millisecond and need nothing but numpy at run
time.  The bundle in this folder is the one that ships; retraining
overwrites it.

## Building a frozen copy

## Training

```
python -m hpose.train --samples 30000 --epochs 100 --out hpose_bundle.npz
```

The script prints the confusion matrices for both heads, so a weak
class is visible rather than hidden inside an average.  The bundle is
a plain `.npz` of float32 arrays: it loads in a millisecond and needs
nothing but numpy at run time.

### Learning from real photographs

```
python -m hpose.train --samples 30000 --real-dir photos/ --real-rounds 2
```

Generated bodies and real people are not alike.  A detector's box
stops where the person stops being visible, a joint is reported where
it was never really seen, and people carry things and lean and sit on
chairs.  Training only on generated bodies teaches the classifier
about a world that does not exist.

Real pictures are therefore put into the training set as well.  Their
labels come from the model that is being trained - self training -
and only the confident ones are kept, so the model cannot drift.  Two
rounds are enough to close most of the gap.

### Two rules that overrule the learned head

Two answers are settled by physics rather than by statistics, and the
classifier is told so:

* **A face means the body is not turned away.**  A face detector
  finds a face in the head region of a person who is looking towards
  the camera, and finds nothing for a person who is not.  The head is
  cut out and enlarged before it is searched, because a face in a wide
  shot is a handful of pixels across.
* **Straight knees mean the person is not crouching.**  A person with
  straight legs and an upright body is standing, whatever the box
  around them says.  A bent knee and an upright body mean the
  opposite.

Both are applied as a last word over the probabilities the head
produced, so the head still decides the fine distinctions.

## Measuring

```
python -m hpose.evaluate --labels labels.json --bundle hpose_bundle.npz
```

`labels.json` lists every person in every picture:

```json
{"items": [
  {"image": "photos/a.jpg",
   "people": [
     {"box": [x1, y1, x2, y2], "view": "front", "stance": "standing"}
   ]}
]}
```

Two more tools help when the numbers do not match the pictures:

* `python -m hpose.diagnose --image photo.jpg` prints the features of
  the people in a photograph next to the same features for synthetic
  bodies, with the biggest differences called out.  This is how the
  difference between a real detector's boxes and a generated one gets
  found.
* `python -m hpose.validate_coco --kp-json kp_val2017.json` runs the
  pipeline over the COCO keypoint annotations, where the visibility
  flags give an honest front/back label that nobody had to guess at.

## Self test

```
python -m hpose.selftest
```

46 checks over the geometry, the generator, the feature vector, the
body fit, the heads, the motion model and the tracker.

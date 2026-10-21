"""Training the view and stance heads.

Run it with::

    python -m hpose.train --samples 20000 --out hpose_bundle.npz

The script generates labelled bodies, fits the geometric model to each
of them, extracts the feature vector and trains two heads: one for the
eight body views and one for the five body states.  Everything is
evaluated on a held-out split that the heads never see, and the
confusion matrices are printed so a weak class is obvious.

Real photographs can be added with ``--real-dir``:

    python -m hpose.train --samples 20000 --real-dir photos/

The real pictures are given pseudo labels by the model that is being
trained, and only the confident ones are kept.  The classifier then
learns the shape of real people as well as the shape of generated
ones, which is the difference between a model that works on the
training data and one that works on a photograph.
"""

from __future__ import annotations

import argparse
import math
import multiprocessing
import os
import random
import sys
import time

import numpy as np

from . import features
from . import model3d
from . import posefit
from . import synth
from .bundle import Bundle, default_bundle_path
from .classifier import train_head
from .keypoints import Pose
from .model3d import VIEW_CLASSES
from .stance import StanceClassifier
from .synth import STANCE_CLASSES
from .view import ViewClassifier


def _prepare(sample: dict):
    """Fit and featurise one sample.

    The fit is told what a face detector would have seen, because that
    is what the analyser does at run time: a face detector finds a
    face when the body is turned less than about seventy degrees
    towards the camera, and misses one now and then.
    """
    pose = Pose(sample["kps"], sample["box"], 1.0)
    yaw = float(sample.get("yaw", 0.0))
    face_visible = math.cos(math.radians(yaw)) > 0.34
    if random.random() < 0.08:
        face_visible = not face_visible
    fit = posefit.fit(sample["kps"], focal_px=sample.get("focal", 960.0),
                      face_visible=face_visible)
    feats = features.extract(pose, fit=fit)
    return feats, fit.get("yaw", 0.0), {"yaw": fit.get("yaw", 0.0),
                                        "branch": fit.get("branch", "none")}


def _init_worker():
    np.random.seed(0)
    random.seed(0)


def _map_worker(sample):
    try:
        return _prepare(sample)
    except Exception:
        return None


def build_dataset(n: int, seed: int, noise: float, workers: int = 0,
                  image_size=(640, 640)):
    """Generate samples, featurise them, and return the matrices."""
    samples = synth.dataset(n, seed=seed, image_size=image_size, noise=noise)
    t0 = time.time()
    if workers and workers > 1:
        try:
            with multiprocessing.Pool(workers, initializer=_init_worker) as pool:
                rows = pool.map(_map_worker, samples, chunksize=32)
        except Exception as exc:
            sys.stderr.write("hpose: parallel prepare failed (%r), "
                             "falling back to one process\n" % (exc,))
            rows = [_prepare(s) for s in samples]
    else:
        rows = [_prepare(s) for s in samples]
    keep = [i for i, r in enumerate(rows) if r is not None]
    x = np.stack([rows[i][0] for i in keep]).astype(np.float32)
    yaw = np.array([rows[i][1] for i in keep], dtype=np.float64)
    fits = [rows[i][2] for i in keep]
    y_view = np.array([VIEW_CLASSES.index(samples[i]["view"]) for i in keep],
                      dtype=np.int64)
    y_stance = np.array([STANCE_CLASSES.index(samples[i]["stance"])
                         for i in keep], dtype=np.int64)
    sys.stderr.write("hpose: prepared %d samples in %.1fs\n"
                     % (len(keep), time.time() - t0))
    return x, y_view, y_stance, yaw, fits


def collect_real(folder: str, bundle_path: str = None, conf: float = 0.25,
                 view_conf: float = 0.55, stance_conf: float = 0.45,
                 limit: int = 0):
    """Collect real detections and give them pseudo labels.

    A photograph is worth far more than a generated body when the two
    of them are alike, and they are not alike: a detector's box stops
    where the person stops being visible, a joint is reported where it
    was never really seen, and a person carries things.  The way to
    teach a classifier about those is to show it real pictures.

    The labels for the real pictures come from the model that is being
    trained, and only the confident ones are kept.  That is ordinary
    self training: the model learns the shape of real people from real
    people, and the confident predictions stop it from drifting.
    """
    from .bundle import load_bundle
    from .detector import FaceDetector, PoseDetector

    bundle = load_bundle(bundle_path)
    view_clf = ViewClassifier(bundle)
    stance_clf = StanceClassifier(bundle)
    detector = PoseDetector(conf=conf)
    try:
        faces = FaceDetector()
    except Exception:
        faces = None

    # an older bundle was trained on a different set of features and
    # cannot label anything for this one
    wanted = features.feature_count()
    for head in (bundle.view_head, bundle.stance_head):
        if head is not None and len(head.mean) != wanted:
            sys.stderr.write("hpose: the bundle at %s was trained on %d "
                             "features, this build uses %d; skipping the "
                             "real pictures\n"
                             % (bundle_path, len(head.mean), wanted))
            return (np.zeros((0, wanted), dtype=np.float32),
                    np.zeros(0, dtype=np.int64),
                    np.zeros(0, dtype=np.int64), [])

    paths = []
    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            if name.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                paths.append(os.path.join(root, name))
    paths.sort()
    if limit:
        paths = paths[:limit]

    x_out = []
    yv_out = []
    ys_out = []
    fits_out = []
    for path in paths:
        import cv2
        img = cv2.imread(path)
        if img is None:
            continue
        focal = img.shape[1] * 0.9
        face_boxes = faces.detect(img) if faces is not None else []
        for pose in detector.detect(img):
            has_face = False
            if face_boxes:
                x1, y1, x2, y2 = pose.box
                w = x2 - x1
                h = y2 - y1
                region = (x1 - w * 0.12, y1 - h * 0.04,
                          x2 + w * 0.12, y1 + h * 0.26)
                for f in face_boxes:
                    fx = (f[0] + f[2]) * 0.5
                    fy = (f[1] + f[3]) * 0.5
                    if region[0] <= fx <= region[2] and \
                            region[1] <= fy <= region[3]:
                        has_face = True
                        break
            fit = posefit.fit(pose.kps, focal_px=focal,
                              face_visible=has_face)
            feats = features.extract(pose, fit=fit)
            view = view_clf.predict(feats, fit)
            stance = stance_clf.predict(feats)
            if (view.confidence < view_conf
                    or stance.confidence < stance_conf):
                continue
            x_out.append(feats)
            yv_out.append(VIEW_CLASSES.index(view.name))
            ys_out.append(STANCE_CLASSES.index(stance.name))
            fits_out.append({"yaw": fit.get("yaw", 0.0),
                             "branch": fit.get("branch", "none")})
    sys.stderr.write("hpose: %d real detections kept from %d pictures\n"
                     % (len(x_out), len(paths)))
    if not x_out:
        return (np.zeros((0, features.feature_count()), dtype=np.float32),
                np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64), [])
    return (np.stack(x_out).astype(np.float32),
            np.asarray(yv_out, dtype=np.int64),
            np.asarray(ys_out, dtype=np.int64), fits_out)


def confusion(y_true: np.ndarray, y_pred: np.ndarray, n: int) -> np.ndarray:
    m = np.zeros((n, n), dtype=np.int64)
    for a, b in zip(y_true.tolist(), y_pred.tolist()):
        m[a, b] += 1
    return m


def print_confusion(matrix: np.ndarray, classes) -> None:
    width = max(len(c) for c in classes)
    print("      " + " ".join("%*s" % (width, c[:width]) for c in classes))
    for i, row in enumerate(matrix):
        cells = " ".join("%*d" % (width, v) for v in row.tolist())
        print("%*s  %s" % (width, classes[i][:width], cells))


def _train_pair(x, y_view, y_stance, args):
    view_head, vhist = train_head(x, y_view, len(VIEW_CLASSES),
                                  hidden=args.hidden, epochs=args.epochs,
                                  lr=args.lr, verbose=args.verbose)
    stance_head, shist = train_head(x, y_stance, len(STANCE_CLASSES),
                                    hidden=args.hidden, epochs=args.epochs,
                                    lr=args.lr, verbose=args.verbose)
    return view_head, stance_head, vhist, shist


def train(args) -> int:
    n = int(args.samples)
    workers = args.workers if args.workers is not None else \
        max(1, min(8, (os.cpu_count() or 2) - 1))
    x, y_view, y_stance, yaw, fits = build_dataset(
        n, args.seed, args.noise, workers)

    # split by index so the same bodies never appear in both halves
    order = np.random.default_rng(args.seed).permutation(len(x))
    cut = int(len(x) * 0.85)
    train_idx, val_idx = order[:cut], order[cut:]

    x_val = x[val_idx]
    yv_val = y_view[val_idx]
    ys_val = y_stance[val_idx]
    fits_val = [fits[i] for i in val_idx]

    print("hpose: %d training, %d validation, %d features"
          % (len(train_idx), len(val_idx), x.shape[1]))

    x_train = x[train_idx]
    yv_train = y_view[train_idx]
    ys_train = y_stance[train_idx]
    fits_train = [fits[i] for i in train_idx]

    # ---- real pictures, if there are any ---------------------------
    if args.real_dir and os.path.isdir(args.real_dir):
        rounds = max(1, int(args.real_rounds))
        for rnd in range(rounds):
            rx, ryv, rys, rfits = collect_real(
                args.real_dir, args.out, args.conf,
                view_conf=args.real_view_conf,
                stance_conf=args.real_stance_conf,
                limit=args.real_limit)
            if len(rx) == 0:
                break
            x_train = np.concatenate([x_train, rx], axis=0)
            yv_train = np.concatenate([yv_train, ryv], axis=0)
            ys_train = np.concatenate([ys_train, rys], axis=0)
            fits_train = list(fits_train) + list(rfits)
            print("hpose: round %d, %d real detections added (%d total)"
                  % (rnd + 1, len(rx), len(x_train)))
            if rnd + 1 < rounds:
                # the pseudo labels for the next round come from the
                # model that was just trained
                vh, sh, _vh, _sh = _train_pair(x_train, yv_train,
                                               ys_train, args)
                meta = {"trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "feature_version": features.FEATURE_VERSION}
                probe = Bundle(view_head=vh, stance_head=sh,
                               view_classes=list(VIEW_CLASSES),
                               stance_classes=list(STANCE_CLASSES),
                               meta=meta)
                probe.save(args.out)

    print("training the view head ...")
    print("training the stance head ...")
    view_head, stance_head, vhist, shist = _train_pair(x_train, yv_train,
                                                       ys_train, args)

    # evaluate the whole classifier, not just the head: at run time the
    # geometric fit has a say as well
    probe = Bundle(view_head=view_head, stance_head=stance_head,
                   view_classes=list(VIEW_CLASSES),
                   stance_classes=list(STANCE_CLASSES))
    print("")
    print("weighing the geometric opinion against the learned head:")
    best_weight = 0.0
    best_acc = -1.0
    for weight in (0.0, 0.1, 0.2, 0.35, 0.5, 0.7, 0.9):
        clf = ViewClassifier(probe, geometric_weight=weight,
                             back_weight=weight * 0.4)
        preds = [VIEW_CLASSES.index(clf.predict(x_val[i], fits_val[i]).name)
                 for i in range(len(x_val))]
        acc = float(np.mean([a == b for a, b in
                              zip(preds, yv_val.tolist())]))
        print("  weight %.2f  accuracy %.4f" % (weight, acc))
        if acc > best_acc:
            best_acc = acc
            best_weight = weight
    print("  using %.2f" % (best_weight,))

    clf = ViewClassifier(probe, geometric_weight=best_weight,
                         back_weight=best_weight * 0.4)
    vp = np.array([VIEW_CLASSES.index(clf.predict(x_val[i],
                                                 fits_val[i]).name)
                   for i in range(len(x_val))], dtype=np.int64)
    sp = stance_head.predict(x_val)
    view_acc = float((vp == yv_val).mean())
    stance_acc = float((sp == ys_val).mean())

    # the geometric fit on its own, for comparison
    geo = np.array([VIEW_CLASSES.index(model3d.yaw_to_class(y))
                     for y in yaw[val_idx]], dtype=np.int64)
    geo_acc = float((geo == yv_val).mean())

    print("")
    print("view accuracy   : %.4f" % (view_acc,))
    print("stance accuracy : %.4f" % (stance_acc,))
    print("geometric fit alone : %.4f" % (geo_acc,))
    print("")
    print("view confusion (rows are truth):")
    print_confusion(confusion(yv_val, vp, len(VIEW_CLASSES)), VIEW_CLASSES)
    print("")
    print("stance confusion (rows are truth):")
    print_confusion(confusion(ys_val, sp, len(STANCE_CLASSES)),
                    STANCE_CLASSES)

    meta = {
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "samples": int(len(x)),
        "real_samples": int(len(x_train) - len(train_idx)),
        "view_acc": view_acc,
        "stance_acc": stance_acc,
        "geometric_acc": geo_acc,
        "geometric_weight": float(best_weight),
        "feature_version": features.FEATURE_VERSION,
        "view_loss": vhist.get("loss"),
        "stance_loss": shist.get("loss"),
    }
    bundle = Bundle(template=None, view_head=view_head,
                    stance_head=stance_head,
                    view_classes=list(VIEW_CLASSES),
                    stance_classes=list(STANCE_CLASSES), meta=meta)
    bundle.save(args.out)
    print("")
    print(bundle.describe())
    print("saved to %s" % (os.path.abspath(args.out),))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m hpose.train",
        description="train the hpose view and stance heads")
    ap.add_argument("--samples", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--noise", type=float, default=1.0)
    ap.add_argument("--hidden", type=int, default=96)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--lr", type=float, default=3e-3)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--out", default=default_bundle_path())
    ap.add_argument("--real-dir", default=None,
                    help="a folder of photographs to learn from")
    ap.add_argument("--real-rounds", type=int, default=1)
    ap.add_argument("--real-limit", type=int, default=0)
    ap.add_argument("--real-view-conf", type=float, default=0.55)
    ap.add_argument("--real-stance-conf", type=float, default=0.45)
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    return train(args)


if __name__ == "__main__":
    sys.exit(main())

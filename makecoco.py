"""Build a real-photo evaluation set from COCO.

Picks images that contain people, downloads them, runs the analyser
over each one and writes

* a contact sheet with the predictions on it, so the pictures can be
  labelled by looking at them,
* the predictions themselves, as JSON,
* the image list, ready to be labelled.

Usage::

    python -m hpose.makecoco --count 60 --out coco_eval
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

COCO_URL = "http://images.cocodataset.org/val2017/%012d.jpg"


def pick_images(kp_json: str, count: int, min_people: int = 1,
                max_people: int = 3, seed: int = 0):
    with open(kp_json, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    by_image = {}
    for ann in data["annotations"]:
        by_image.setdefault(ann["image_id"], []).append(ann)
    info = {im["id"]: im for im in data["images"]}
    usable = []
    for image_id, anns in by_image.items():
        if not (min_people <= len(anns) <= max_people):
            continue
        if any(a.get("iscrowd", 0) for a in anns):
            continue
        # want people whose keypoints were actually annotated
        good = [a for a in anns
                if sum(1 for i in range(2, len(a.get("keypoints", [])), 3)
                       if a["keypoints"][i] > 0) >= 10]
        if not good:
            continue
        im = info.get(image_id)
        if not im or im.get("width", 0) < 300:
            continue
        usable.append(image_id)
    usable.sort()
    import random
    rnd = random.Random(seed)
    rnd.shuffle(usable)
    return usable[:count]


def download(image_ids, out_dir: str) -> list:
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for image_id in image_ids:
        name = "%012d.jpg" % (image_id,)
        path = os.path.join(out_dir, name)
        if not os.path.isfile(path):
            url = COCO_URL % (image_id,)
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    data = r.read()
                with open(path, "wb") as fh:
                    fh.write(data)
            except Exception as exc:
                sys.stderr.write("hpose: %s failed (%r)\n" % (name, exc))
                continue
        paths.append(path)
    return paths


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hpose.makecoco")
    ap.add_argument("--kp-json", default="kp_val2017.json")
    ap.add_argument("--count", type=int, default=60)
    ap.add_argument("--out", default="coco_eval")
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--bundle", default=None)
    args = ap.parse_args(argv)

    ids = pick_images(args.kp_json, args.count)
    print("picked %d images" % (len(ids),))
    paths = download(ids, args.out)
    print("downloaded %d images into %s" % (len(paths), args.out))

    from .analyzer import Analyzer
    analyzer = Analyzer(bundle_path=args.bundle)
    records = []
    for path in paths:
        import cv2
        img = cv2.imread(path)
        if img is None:
            continue
        tracks = analyzer.analyze(img, t=0.0, reset=True)
        records.append({
            "image": path,
            "people": [{"box": list(tr.box), "view": tr.view,
                        "stance": tr.stance,
                        "view_confidence": round(float(tr.view_probs.get(
                            tr.view, 0.0)), 3),
                        "stance_confidence": round(float(tr.stance_probs.get(
                            tr.stance, 0.0)), 3),
                        "has_face": bool(tr.has_face)}
                       for tr in tracks],
        })
    with open(os.path.join(args.out, "predictions.json"), "w",
              encoding="utf-8") as fh:
        json.dump(records, fh, indent=1)
    print("wrote predictions to %s" % (os.path.join(args.out,
                                                    "predictions.json"),))

    if args.sheet:
        _sheet(paths, records, args.sheet, args.out)
        print("wrote %s" % (args.sheet,))
    return 0


def _sheet(paths, records, out_path: str, out_dir: str, cell: int = 300,
           cols: int = 4) -> None:
    """A contact sheet with the predicted labels written on it.

    Every picture is scaled to fit a square cell so that the sheet
    stays readable whatever shape the photographs are.
    """
    import cv2
    import numpy as np
    by_path = {r["image"]: r for r in records}
    cells = []
    for path in paths:
        img = cv2.imread(path)
        if img is None:
            continue
        h, w = img.shape[:2]
        scale = min(float(cell) / w, float(cell) / h)
        small = cv2.resize(img, (max(1, int(w * scale)),
                                 max(1, int(h * scale))))
        canvas = np.full((cell, cell, 3), 25, dtype=np.uint8)
        oy = (cell - small.shape[0]) // 2
        ox = (cell - small.shape[1]) // 2
        canvas[oy:oy + small.shape[0], ox:ox + small.shape[1]] = small
        rec = by_path.get(path, {})
        for j, p in enumerate(rec.get("people", [])):
            b = p["box"]
            x1 = int(b[0] * scale) + ox
            y1 = int(b[1] * scale) + oy
            x2 = int(b[2] * scale) + ox
            y2 = int(b[3] * scale) + oy
            cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(canvas, "%s %s" % (p["view"], p["stance"]),
                        (max(2, x1), max(12, y1 - 5)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 2,
                        cv2.LINE_AA)
        cv2.putText(canvas, os.path.basename(path).replace(".jpg", ""),
                    (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255),
                    1, cv2.LINE_AA)
        cells.append(canvas)
    if not cells:
        return
    rows = []
    for i in range(0, len(cells), cols):
        chunk = cells[i:i + cols]
        while len(chunk) < cols:
            chunk.append(np.full((cell, cell, 3), 25, dtype=np.uint8))
        rows.append(np.concatenate(chunk, axis=1))
    cv2.imwrite(out_path, np.concatenate(rows, axis=0))


if __name__ == "__main__":
    sys.exit(main())

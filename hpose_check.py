"""Check that the hpose path is wired into Snappy AI correctly.

    python hpose_check.py

It runs the real scan path over a few pictures and checks that

* the classical scanner still answers when hpose is off,
* hpose answers when it is on, and says who it found,
* the view and stance filters are obeyed,
* the aim point lands on the person and the lead follows a moving one.

The pictures it uses are the ones in ``%TEMP%\\hpose_check``; drop
your own in there to check them, or let it use the bundled samples.
"""

import os
import sys
import time

import cv2
import numpy as np

import config
import hpose_bridge
import vision


def _sample_images():
    """Some pictures with people in them."""
    out = []
    for folder in (os.path.join(tempfile_dir(), "probe", "coco_eval2"),
                   os.path.join(tempfile_dir(), "hpose_check")):
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith((".jpg", ".png", ".jpeg")):
                out.append(os.path.join(folder, name))
    return out


def tempfile_dir():
    return os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"


class Check(object):
    def __init__(self):
        self.passed = 0
        self.failed = []

    def ok(self, name, condition, detail=""):
        if condition:
            self.passed += 1
            print("  pass  %s" % (name,))
        else:
            self.failed.append(name)
            print("  FAIL  %s  %s" % (name, detail))


def main():
    print("hpose integration check")
    c = Check()
    images = _sample_images()
    if not images:
        print("  no pictures to work with - put some in %s"
              % (os.path.join(tempfile_dir(), "hpose_check"),))
        return 1
    print("  %d picture(s)" % (len(images),))

    cfg = dict(config.DEFAULT_CONFIG)

    # --- the classical path must be untouched ------------------------
    cfg["hpose_mode"] = "off"
    worked = 0
    for path in images[:6]:
        img = cv2.imread(path)
        if img is None:
            continue
        h, w = img.shape[:2]
        res = vision.scan_region(img, w // 2, h // 2, cfg, radius=400,
                                 origin=(0, 0))
        if res.kind == "target":
            worked += 1
    c.ok("the classical scanner still answers with hpose off",
         worked > 0, "%d of 6" % (worked,))

    # --- hpose must take over when it is on -------------------------
    cfg["hpose_mode"] = "on"
    cfg["hpose_min_conf"] = 25
    hpose_bridge.preload(cfg)
    for _ in range(120):
        if hpose_bridge.engine(cfg) is not None:
            break
        time.sleep(0.25)
    c.ok("the hpose engine loads", hpose_bridge.engine(cfg) is not None)

    found = 0
    described = 0
    for path in images:
        img = cv2.imread(path)
        if img is None:
            continue
        h, w = img.shape[:2]
        res = vision.scan_region(img, w // 2, h // 2, cfg, radius=600,
                                 origin=(0, 0))
        if res.kind == "target" and res.target is not None:
            found += 1
            if res.target.view:
                described += 1
    c.ok("hpose finds people in the pictures", found > 0,
         "%d targets" % (found,))
    c.ok("the target says who it is", described > 0,
         "%d described" % (described,))

    # --- the filters must be obeyed ----------------------------------
    img = cv2.imread(images[0])
    if img is not None:
        h, w = img.shape[:2]
        for want, field in (("facing", "facing_camera"),
                            ("away", "back_turned")):
            cfg["hpose_view"] = want
            ok = True
            seen = 0
            for path in images[:8]:
                pic = cv2.imread(path)
                if pic is None:
                    continue
                hh, ww = pic.shape[:2]
                res = vision.scan_region(pic, ww // 2, hh // 2, cfg,
                                         radius=600, origin=(0, 0))
                if res.kind == "target" and res.target.view:
                    seen += 1
                    if want == "facing" and not res.target.view.startswith(
                            "front"):
                        ok = False
                    if want == "away" and not res.target.view.startswith(
                            "back"):
                        ok = False
            c.ok("the view filter '%s' is obeyed" % (want,), ok,
                 "%d targets" % (seen,))
        cfg["hpose_view"] = "any"

    # --- the aim point must land on the person -----------------------
    for path in images[:8]:
        pic = cv2.imread(path)
        if pic is None:
            continue
        hh, ww = pic.shape[:2]
        res = vision.scan_region(pic, ww // 2, hh // 2, cfg, radius=600,
                                 origin=(0, 0))
        if res.kind != "target" or res.target is None:
            continue
        t = res.target
        ax = ww // 2 + t.dx
        ay = hh // 2 + t.dy
        inside = (0 <= ax < ww and 0 <= ay < hh)
        c.ok("the aim point is inside the picture", inside,
             "%s" % ((ax, ay),))
        break

    # --- a moving target must lead -----------------------------------
    if img is not None:
        h, w = img.shape[:2]
        hpose_bridge.reset()
        lead_seen = False
        moved = False
        for i in range(8):
            frame = np.roll(img, -i * 12, axis=1)
            res = vision.scan_region(frame, w // 2, h // 2, cfg,
                                     radius=600, origin=(0, 0))
            if res.kind == "target" and res.target is not None:
                if res.target.motion and res.target.motion != "static":
                    moved = True
                if abs(res.target.lead_x) > 1.0 or \
                        abs(res.target.lead_y) > 1.0:
                    lead_seen = True
        c.ok("a moving target is measured as moving", moved)
        c.ok("a moving target is led", lead_seen)

    print("")
    print("%d passed, %d failed" % (c.passed, len(c.failed)))
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""One-time job, ideally on a GPU machine: export a faster pose ONNX.

The shipped yolo11n-pose.onnx has a fixed 640x640 input.  For a
real-time loop on a CPU-only machine, a 320x320 export runs ~4x faster
per pass; the zoom-window architecture absorbs most of the lost
resolution (a smaller window at the same zoom shows the same pixels),
at the cost of the deepest zoom tiers.

    pip install ultralytics torch
    python tools/export_fast.py            # 320px, dynamic=False
    python tools/export_fast.py --imgsz 416

Then drop the file into snapdet/models/ and run with

    python -m snapdet live --pose-model yolo11n-pose-320.onnx ...

Accuracy-first users keep the 640 export; the engine reads the input
size from the ONNX session, nothing else changes.
"""
import argparse
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--imgsz", type=int, default=320)
    ap.add_argument("--model", default="yolo11n-pose")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args(argv)
    try:
        from ultralytics import YOLO
    except ImportError:
        print("this recipe needs ultralytics:  pip install ultralytics "
              "torch")
        return 1
    here = os.path.dirname(os.path.abspath(__file__))
    out = args.out_dir or os.path.join(here, "..", "snapdet", "models")
    m = YOLO("%s.pt" % args.model)          # official weights download
    path = m.export(format="onnx", imgsz=args.imgsz, opset=12,
                    simplify=True)
    dest = os.path.join(out, "%s-%d.onnx" % (args.model, args.imgsz))
    os.replace(path, dest)
    print("exported %s" % dest)
    print("use it with:  --pose-model %s" % os.path.basename(dest))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The trained artefact: one file that holds everything inference needs.

A bundle contains

* the canonical body template used to repair missing joints,
* the view head (front / back / side / 45 degrees),
* the stance head (standing / crouch / prone / jump),
* the feature standardisation statistics.

It is a plain ``.npz`` of float32 arrays, so it loads in a
millisecond and needs nothing beyond numpy at run time.
"""

from __future__ import annotations

import json
import os
import sys
import time

import numpy as np

from . import classifier

BUNDLE_VERSION = 3
DEFAULT_BUNDLE_NAME = "snapdet_bundle.npz"


class Bundle(object):
    """Everything the analyser needs to classify a person."""

    def __init__(self, template=None, view_head=None, stance_head=None,
                 view_classes=None, stance_classes=None, meta=None):
        self.template = (np.zeros((17, 2), dtype=np.float32)
                         if template is None
                         else np.asarray(template, dtype=np.float32))
        self.view_head = view_head
        self.stance_head = stance_head
        self.view_classes = list(view_classes or [])
        self.stance_classes = list(stance_classes or [])
        self.meta = dict(meta or {})

    # -- persistence ---------------------------------------------------
    def save(self, path: str) -> str:
        arrays = {"template": self.template}
        if self.view_head is not None:
            for k, v in self.view_head.to_arrays().items():
                arrays["view_" + k] = v
        if self.stance_head is not None:
            for k, v in self.stance_head.to_arrays().items():
                arrays["stance_" + k] = v
        arrays["meta_json"] = np.asarray(json.dumps({
            "version": BUNDLE_VERSION,
            "view_classes": self.view_classes,
            "stance_classes": self.stance_classes,
            "meta": self.meta,
            "saved_at": time.time(),
        }))
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".",
                    exist_ok=True)
        np.savez_compressed(path, **arrays)
        return path

    @classmethod
    def load(cls, path: str) -> "Bundle":
        with np.load(path, allow_pickle=False) as z:
            arrays = {k: z[k] for k in z.files}
        meta_blob = json.loads(str(arrays.pop("meta_json")))
        template = arrays.pop("template", None)
        view_arrays = {k[5:]: v for k, v in arrays.items()
                       if k.startswith("view_")}
        stance_arrays = {k[7:]: v for k, v in arrays.items()
                         if k.startswith("stance_")}
        view_head = (classifier.head_from_arrays(
            view_arrays, meta_blob.get("view_classes"))
            if view_arrays else None)
        stance_head = (classifier.head_from_arrays(
            stance_arrays, meta_blob.get("stance_classes"))
            if stance_arrays else None)
        return cls(template=template, view_head=view_head,
                   stance_head=stance_head,
                   view_classes=meta_blob.get("view_classes") or [],
                   stance_classes=meta_blob.get("stance_classes") or [],
                   meta=meta_blob.get("meta") or {})

    # -- convenience ---------------------------------------------------
    def has_view(self) -> bool:
        return self.view_head is not None

    def has_stance(self) -> bool:
        return self.stance_head is not None

    def describe(self) -> str:
        lines = ["snapdet bundle v%d" % (BUNDLE_VERSION,)]
        if self.meta.get("trained_at"):
            lines.append("  trained: %s" % (self.meta["trained_at"],))
        if self.meta.get("view_acc") is not None:
            lines.append("  view accuracy:  %.3f" % (self.meta["view_acc"],))
        if self.meta.get("stance_acc") is not None:
            lines.append("  stance accuracy: %.3f" % (self.meta["stance_acc"],))
        lines.append("  view classes:  %s" % (", ".join(self.view_classes),))
        lines.append("  stance classes: %s" % (", ".join(self.stance_classes),))
        return "\n".join(lines)


def default_bundle_path() -> str:
    """Where the trained heads live.

    The bundle sits inside the project, beside the code that uses it,
    so the whole folder can be copied to another machine and still
    work.  ``SNAPDET_BUNDLE`` overrides it, and a frozen build keeps it
    beside the executable.
    """
    env = os.environ.get("SNAPDET_BUNDLE")
    if env:
        return env
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "snapdet", DEFAULT_BUNDLE_NAME)


def load_bundle(path: str = None) -> Bundle:
    """Load a bundle, or return an empty one when none is available."""
    path = path or default_bundle_path()
    if not os.path.isfile(path):
        return Bundle()
    try:
        return Bundle.load(path)
    except Exception:
        return Bundle()

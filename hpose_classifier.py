
from __future__ import annotations

import numpy as np


class LinearHead(object):

    kind = "linear"

    def __init__(self, w, b, mean=None, std=None, classes=None):
        self.w = np.asarray(w, dtype=np.float32)
        self.b = np.asarray(b, dtype=np.float32)
        dim = self.w.shape[0]
        self.mean = (np.zeros(dim, dtype=np.float32)
                     if mean is None else np.asarray(mean, dtype=np.float32))
        self.std = (np.ones(dim, dtype=np.float32)
                    if std is None else np.asarray(std, dtype=np.float32))
        self.classes = list(classes or [])

    def _z(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32)
        if x.ndim == 1:
            x = x[None, :]
        x = (x - self.mean) / np.maximum(self.std, 1e-6)
        return x @ self.w + self.b

    def proba(self, x: np.ndarray) -> np.ndarray:
        z = self._z(x)
        z = z - z.max(axis=1, keepdims=True)
        e = np.exp(z)
        return e / np.maximum(e.sum(axis=1, keepdims=True), 1e-12)

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.proba(x).argmax(axis=1)

    def to_arrays(self):
        return {"head_w": self.w, "head_b": self.b,
                "feat_mean": self.mean, "feat_std": self.std}


class MLPHead(object):

    kind = "mlp"

    def __init__(self, w1, b1, w2, b2, mean=None, std=None, classes=None):
        self.w1 = np.asarray(w1, dtype=np.float32)
        self.b1 = np.asarray(b1, dtype=np.float32)
        self.w2 = np.asarray(w2, dtype=np.float32)
        self.b2 = np.asarray(b2, dtype=np.float32)
        dim = self.w1.shape[0]
        self.mean = (np.zeros(dim, dtype=np.float32)
                     if mean is None else np.asarray(mean, dtype=np.float32))
        self.std = (np.ones(dim, dtype=np.float32)
                    if std is None else np.asarray(std, dtype=np.float32))
        self.classes = list(classes or [])

    def _h(self, x):
        x = np.asarray(x, dtype=np.float32)
        if x.ndim == 1:
            x = x[None, :]
        x = (x - self.mean) / np.maximum(self.std, 1e-6)
        return np.maximum(x @ self.w1 + self.b1, 0.0)

    def proba(self, x: np.ndarray) -> np.ndarray:
        h = self._h(x)
        z = h @ self.w2 + self.b2
        z = z - z.max(axis=1, keepdims=True)
        e = np.exp(z)
        return e / np.maximum(e.sum(axis=1, keepdims=True), 1e-12)

    def predict(self, x: np.ndarray) -> np.ndarray:
        return self.proba(x).argmax(axis=1)

    def to_arrays(self):
        return {"head_w1": self.w1, "head_b1": self.b1,
                "head_w2": self.w2, "head_b2": self.b2,
                "feat_mean": self.mean, "feat_std": self.std}


def head_from_arrays(arrays: dict, classes=None):
    if "head_w" in arrays:
        return LinearHead(arrays["head_w"], arrays["head_b"],
                          arrays.get("feat_mean"), arrays.get("feat_std"),
                          classes)
    return MLPHead(arrays["head_w1"], arrays["head_b1"],
                   arrays["head_w2"], arrays["head_b2"],
                   arrays.get("feat_mean"), arrays.get("feat_std"),
                   classes)




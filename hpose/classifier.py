"""Tiny classifiers with numpy-only inference.

Two heads are enough for this problem and both are cheap:

* :class:`LinearHead` - multinomial logistic regression.
* :class:`MLPHead` - one hidden layer with ReLU.

Both are *trained* with torch (which is optional) and *exported* to a
flat ``.npz`` of float32 arrays, so inference never needs torch.
Standardisation statistics travel with the weights.
"""

from __future__ import annotations

import os

import numpy as np


class LinearHead(object):
    """Multinomial logistic regression over ``D`` features."""

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
    """One hidden layer, ReLU, softmax output."""

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
    """Rebuild a head from a bundle's arrays."""
    if "head_w" in arrays:
        return LinearHead(arrays["head_w"], arrays["head_b"],
                          arrays.get("feat_mean"), arrays.get("feat_std"),
                          classes)
    return MLPHead(arrays["head_w1"], arrays["head_b1"],
                   arrays["head_w2"], arrays["head_b2"],
                   arrays.get("feat_mean"), arrays.get("feat_std"),
                   classes)


def train_head(x: np.ndarray, y: np.ndarray, n_classes: int,
               hidden: int = 96, epochs: int = 60, batch: int = 256,
               lr: float = 3e-3, weight_decay: float = 1e-4,
               seed: int = 7, verbose: bool = False):
    """Train a head with torch and export it to numpy arrays.

    Returns ``(head, history)``.  Falls back to a closed-form
    multinomial logistic regression when torch is unavailable.
    """
    x = np.asarray(x, dtype=np.float32)
    y = np.asarray(y, dtype=np.int64)
    mean = x.mean(axis=0)
    std = x.std(axis=0)
    std[std < 1e-6] = 1.0
    xs = (x - mean) / std

    try:
        import torch
    except ImportError:
        head = _train_logreg(xs, y, n_classes)
        head.mean = mean
        head.std = std
        return head, {}

    torch.manual_seed(seed)
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    xt = torch.from_numpy(xs)
    yt = torch.from_numpy(y)
    n = xt.shape[0]
    model = torch.nn.Sequential(
        torch.nn.Linear(xt.shape[1], hidden),
        torch.nn.ReLU(),
        torch.nn.Linear(hidden, n_classes),
    )
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    lossf = torch.nn.CrossEntropyLoss(label_smoothing=0.02)
    g = torch.Generator().manual_seed(seed)
    history = {}
    for ep in range(epochs):
        perm = torch.randperm(n, generator=g)
        total = 0.0
        for i in range(0, n, batch):
            idx = perm[i:i + batch]
            opt.zero_grad()
            loss = lossf(model(xt[idx]), yt[idx])
            loss.backward()
            opt.step()
            total += float(loss.detach()) * len(idx)
        history["epoch"] = ep + 1
        history["loss"] = total / max(1, n)
        if verbose and (ep + 1) % 10 == 0:
            print("  epoch %3d  loss %.4f" % (ep + 1, history["loss"]))
    model.eval()
    with torch.no_grad():
        out = model(xt)
        pred = out.argmax(dim=1).numpy()
    acc = float((pred == y).mean())
    history["train_acc"] = acc
    w1 = model[0].weight.detach().numpy().T.astype(np.float32)
    b1 = model[0].bias.detach().numpy().astype(np.float32)
    w2 = model[2].weight.detach().numpy().T.astype(np.float32)
    b2 = model[2].bias.detach().numpy().astype(np.float32)
    head = MLPHead(w1, b1, w2, b2, mean, std)
    return head, history


def _train_logreg(xs: np.ndarray, y: np.ndarray, n_classes: int,
                  iters: int = 400, lr: float = 0.5, l2: float = 1e-3):
    """Fallback optimiser: full-batch gradient descent on softmax."""
    n, d = xs.shape
    xs = np.concatenate([xs, np.ones((n, 1), dtype=np.float32)], axis=1)
    w = np.zeros((d + 1, n_classes), dtype=np.float32)
    yy = np.zeros((n, n_classes), dtype=np.float32)
    yy[np.arange(n), y] = 1.0
    for _ in range(iters):
        z = xs @ w
        z -= z.max(axis=1, keepdims=True)
        p = np.exp(z)
        p /= np.maximum(p.sum(axis=1, keepdims=True), 1e-12)
        g = xs.T @ (p - yy) / n
        g[:-1] += l2 * w[:-1]
        w -= lr * g
    return LinearHead(w[:-1], w[-1])

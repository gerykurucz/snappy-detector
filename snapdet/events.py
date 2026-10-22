"""Operator selection events and the robot-facing event bus.

snapdet detects; the OPERATOR decides.  Nothing here moves anything -
the module turns "the user clicked / is holding near a detected person"
into small, timestamped events that a consumer (robot controller,
teleoperation UI, analytics logger) acts on:

    select   one authorised click; carries the assist point (the
             selected track's centre), the raw click, and the track
    hold     emitted every frame while LMB is held and a track is
             under the cursor magnet - the assisted point follows the
             target as it moves
    release  the hold ended

Publication is a UDP JSON datagram per event (fire-and-forget, fits
real-time control loops) plus in-process callbacks.  Example consumer:
``tools/robot_consumer.py``.

Why there is no OS cursor control in this repository: a consumer that
owns its window handles clicks natively; a robot wants events; the
only remaining case for injecting cursor movement toward
software-picked humans is a third-party window (a game), where it is
an aim assist.  If your own UI wants a magnetised cursor, call
SetCursorPos / warp_pointer yourself from these events.
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass, asdict


@dataclass
class SelectEvent(object):
    kind: str                 # "select" | "hold" | "release"
    t: float                  # engine clock, seconds
    x: int                    # assist point (selected track centre)
    y: int
    click_x: int = 0          # raw operator cursor position
    click_y: int = 0
    track_id: int = -1        # -1 = clicked empty space
    view: str = ""
    stance: str = ""
    height: float = 0.0
    source: str = ""
    tracked: bool = True

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, blob: str) -> "SelectEvent":
        return cls(**json.loads(blob))


def pick_track(tracks, x: float, y: float, radius: float):
    """The track the operator meant, or None.

    Inside a box counts as a hit at distance 0; outside, the distance
    to the box edge must be within ``radius`` - the click magnet.
    Nearest wins.
    """
    best, bd = None, 1e18
    for tr in tracks:
        x1, y1, x2, y2 = tr.box
        dx = max(x1 - x, 0.0, x - x2)
        dy = max(y1 - y, 0.0, y - y2)
        d = (dx * dx + dy * dy) ** 0.5
        if d <= radius and d < bd:
            best, bd = tr, d
    return best


class EventBus(object):
    """Callbacks + optional UDP JSON publication."""

    def __init__(self, publish: str = None):
        self._subs = []
        self._sock = None
        self._addr = None
        if publish:
            self.publish_to(publish)

    def subscribe(self, fn) -> None:
        self._subs.append(fn)

    def publish_to(self, spec: str) -> None:
        """``udp://host:port``."""
        if not spec.startswith("udp://"):
            raise RuntimeError("only udp:// endpoints are supported: %r"
                               % (spec,))
        host, port = spec[len("udp://"):].rsplit(":", 1)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._addr = (host, int(port))

    def emit(self, ev: SelectEvent) -> None:
        for fn in self._subs:
            try:
                fn(ev)
            except Exception:
                pass                      # a bad subscriber must not kill
        if self._sock is not None:        # the detection loop
            try:
                self._sock.sendto(ev.to_json().encode("utf-8"), self._addr)
            except OSError:
                pass

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

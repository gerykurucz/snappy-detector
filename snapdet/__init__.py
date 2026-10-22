"""snapdet - finding every person in the frame, down to a few pixels.

    python -m snapdet video --source game.mp4 --out debug.mp4
    python -m snapdet image --source shot.jpg --out shot_debug.jpg
    python -m snapdet bench

See ``README.md`` for the architecture and the honest performance
table; ``python -m snapdet.smoketest`` runs the acceptance tests.
"""

__version__ = "0.1.0"

from .config import Config
from .engine import Brain, SnapEngine
from .debug import draw_debug
from .capture import open_source
from .live import BudgetController, RealtimeLoop

__all__ = ["Config", "SnapEngine", "Brain", "draw_debug", "open_source",
           "RealtimeLoop", "BudgetController", "__version__"]

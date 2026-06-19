"""PyGlass — a physically-grounded glass UI experiment built on PyQt6.

Step 1: a frosted-glass popup that performs a real Gaussian blur of the
scene behind it and composites a tinted glass surface on top.
"""

__version__ = "0.1.0"

from .glass import GlassPopup

__all__ = ["GlassPopup", "__version__"]

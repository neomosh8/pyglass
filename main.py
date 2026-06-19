"""PyGlass entry point.

    python main.py            # demo window with the painted scene
    python main.py --desktop  # floating glass pane over your real desktop
"""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication


def main() -> int:
    app = QApplication(sys.argv)

    if "--desktop" in sys.argv[1:]:
        from pyglass.desktop import DesktopGlass

        window = DesktopGlass()
    else:
        from pyglass.demo import DemoBackground

        window = DemoBackground()

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

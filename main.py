"""PyGlass entry point — launches the demo window with the frosted-glass popup."""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from pyglass.demo import DemoBackground


def main() -> int:
    app = QApplication(sys.argv)
    window = DemoBackground()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

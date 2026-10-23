"""Launch the desktop application."""

from pathlib import Path
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    from vasl_ai_bridge.launcher import main
    raise SystemExit(main())

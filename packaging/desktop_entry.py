"""Launch the desktop app, or explicit offline verification when requested."""

from pathlib import Path
import sys

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        from vasl_ai_bridge.packaged_self_test import main
    else:
        from vasl_ai_bridge.launcher import main
    raise SystemExit(main())

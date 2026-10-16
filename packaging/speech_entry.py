"""Console helper for bounded native speech IPC and external-program isolation."""

from __future__ import annotations

import os
import sys

if not getattr(sys, "frozen", False):
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vasl_ai_bridge import frozen_runtime


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    try:
        if arguments and arguments[0] == "--external":
            return frozen_runtime.run_external(arguments[1:])
        if arguments == ["--backend", "whisper_cpp_native"]:
            from vasl_ai_bridge import whisper_cpp_native_worker as worker
        elif arguments == ["--backend", "whisper_desktop"]:
            from vasl_ai_bridge import whisper_desktop_worker as worker
        else:
            if sys.stderr is not None:
                sys.stderr.write("Unsupported background helper command.\n")
            return 2
        # The stdlib worker imports are loaded before resetting helper lookup.
        frozen_runtime.reset_helper_library_search()
        sanitized = frozen_runtime.external_environment()
        for key in list(os.environ):
            if key not in sanitized:
                del os.environ[key]
        os.environ.update(sanitized)
        return worker.main()
    except (OSError, RuntimeError, ValueError):
        # Never copy user command lines, paths, or raw errors into diagnostics.
        if sys.stderr is not None:
            sys.stderr.write("The background helper could not start its local worker.\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

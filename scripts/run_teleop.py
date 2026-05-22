#!/usr/bin/env python3
"""Entry-point script: `uv run python scripts/run_teleop.py [-c conf/...]`."""

from vr_teleop.runtime.loop import main

if __name__ == "__main__":
    main()

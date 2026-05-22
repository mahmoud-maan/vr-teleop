#!/usr/bin/env python3
"""Sanity-check that the configured arm is reachable.

Connects to the arm, reads its current observation once, prints it, and
disconnects. No Quest, no IK, no pipeline — useful as a first-thing-to-run
when bringing up new hardware.
"""

import argparse
from pathlib import Path

from vr_teleop.settings import load_settings

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "conf" / "arm.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description="Read and print current joint states.")
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()

    robot, _settings = load_settings(args.config)
    robot.connect()
    try:
        obs = robot.get_observation()
        print("Current observation:")
        for k, v in obs.items():
            print(f"  {k}: {v}")
    finally:
        robot.disconnect()


if __name__ == "__main__":
    main()

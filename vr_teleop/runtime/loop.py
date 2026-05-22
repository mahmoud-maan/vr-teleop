"""The main teleop loop — poll the Quest socket and drive the arm.

This is the `vr-teleop` console-script entry-point (see pyproject.toml).

* one WebSocket server (port 8080) receives ``FramePacket``s from the Godot client,
* one HTTP MJPEG server (port 8765) streams camera frames back to the headset,
* on every tick we poll the latest controller frame, dispatch the corresponding
  action (``none`` / ``reset`` / ``start_episode`` / ``stop_episode`` /
  ``save_dataset``), and optionally record to a LeRobotDataset.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from lerobot.utils.robot_utils import precise_sleep
from lerobot.utils.visualization_utils import init_rerun, log_rerun_data

from vr_teleop.dataset import (
    delete_episodes_from_dataset,
    end_active_episode,
    finalize_dataset,
    record_step,
    setup_dataset,
)
from vr_teleop.logs import get_logger, log, maybe_log_loop_timing
from vr_teleop.runtime.session import build_session
from vr_teleop.server import setup_camera_server
from vr_teleop.settings import load_settings
from vr_teleop.transport.quest_socket import open_quest_socket

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "conf" / "arm.yaml"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="vr_teleop — bridge a Godot/Meta Quest XR app to an SO-101 arm.",
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Start the VR teleop loop")
    run_parser.add_argument(
        "-c",
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"Path to YAML config (default: {DEFAULT_CONFIG}).",
    )

    del_parser = subparsers.add_parser(
        "delete-episodes",
        help="Delete episodes from an existing LeRobot dataset.",
    )
    del_parser.add_argument(
        "--repo-id", required=True,
        help="Repo id of the dataset (e.g. 'user/dataset-name').",
    )
    del_parser.add_argument(
        "--episodes", required=True,
        help='Episode indices to delete as a JSON list, e.g. "[0, 2, 5]".',
    )
    del_parser.add_argument(
        "--root", default=None,
        help="Optional root directory override.",
    )
    del_parser.add_argument(
        "--push-to-hub", action="store_true", default=False,
        help="Push the updated dataset to the Hugging Face Hub.",
    )

    # Allow `vr-teleop -c ...` without an explicit subcommand.
    parser.add_argument(
        "-c",
        "--config",
        default=str(DEFAULT_CONFIG),
        help=argparse.SUPPRESS,
    )
    return parser


def _delete_episodes(args: argparse.Namespace, logger) -> None:
    episode_indices = json.loads(args.episodes)
    if not isinstance(episode_indices, list) or not all(
        isinstance(i, int) for i in episode_indices
    ):
        raise SystemExit("--episodes must be a JSON list of integers, e.g. '[0, 2, 5]'")
    delete_episodes_from_dataset(
        repo_id=args.repo_id,
        episode_indices=episode_indices,
        root=args.root,
        push_to_hub=args.push_to_hub,
        logger=logger,
    )


def _run(args: argparse.Namespace, logger) -> None:
    config_path = getattr(args, "config", str(DEFAULT_CONFIG)) or str(DEFAULT_CONFIG)
    robot, settings = load_settings(config_path)

    # ── transports ──────────────────────────────────────────────────────────
    quest = open_quest_socket(
        host=settings.transport.host, port=settings.transport.port
    )
    camera_server = setup_camera_server(robot, settings, logger)

    # ── pipelines / dataset ─────────────────────────────────────────────────
    session = build_session(robot, settings)
    dataset = setup_dataset(robot, settings, logger)

    robot.connect()
    if not robot.is_connected or not quest.is_connected:
        raise RuntimeError("Robot arm or Quest socket failed to connect.")

    if settings.use_rerun:
        init_rerun(session_name="vr_teleop")

    session.capture_initial_observations()

    log(
        "✅ Ready. Connect from the Godot XR app and squeeze the grip "
        "to move the arm. Press Ctrl+C to stop.",
        logger,
    )

    push_to_hub = settings.dataset.push_to_hub if settings.dataset else False
    recording = False
    finalized_dataset = False

    loop_count = 0
    period = 1.0 / settings.fps

    try:
        while True:
            t0 = time.perf_counter()
            frame = quest.last_frame
            t_control: float | None = None
            camera_frames: dict = {}

            if session.is_resetting():
                # One ramp step per tick; camera streaming below still runs.
                session.tick_reset()
            elif frame is not None:
                action_str = frame.get("action", "none")

                if action_str == "reset" and not session.has_initial_position:
                    session.reset()
                elif action_str == "start_episode" and not recording:
                    log(
                        f"🔴 Recording episode "
                        f"{dataset.num_episodes if dataset else '?'}...",
                        logger,
                    )
                    recording = True
                    finalized_dataset = False
                elif action_str == "stop_episode" and recording:
                    session.reset()
                    end_active_episode(dataset, logger)
                    recording = False
                elif action_str == "save_dataset" and not finalized_dataset:
                    finalize_dataset(dataset, push_to_hub, logger)
                    recording = False
                    finalized_dataset = True
                else:
                    result = session.handle_frame(frame)
                    t_control = time.perf_counter()
                    if result is not None:
                        obs, action = result
                        # Pluck camera frames out of obs so we don't double-read.
                        for cam_name in getattr(robot, "cameras", {}) or {}:
                            if cam_name in obs:
                                camera_frames[cam_name] = obs[cam_name]
                        if settings.use_rerun:
                            log_rerun_data(observation=obs, action=action)
                        if recording:
                            record_step(dataset, settings, obs, action)

            # Always stream cameras (fall back to a direct async_read).
            for cam_name, cam in (getattr(robot, "cameras", {}) or {}).items():
                cam_frame = camera_frames.get(cam_name)
                if cam_frame is None:
                    try:
                        cam_frame = cam.async_read()
                    except Exception:
                        cam_frame = None
                camera_server.update_camera_frame(cam_name, cam_frame)

            loop_count += 1
            maybe_log_loop_timing(logger, loop_count, t0, t_control)
            precise_sleep(max(period - (time.perf_counter() - t0), 0.0))

    except KeyboardInterrupt:
        log("\nStopping teleop.", logger)
    finally:
        finalize_dataset(dataset, push_to_hub, logger)
        try:
            robot.disconnect()
        except Exception:
            pass


def main() -> None:
    logger = get_logger()
    parser = _build_parser()
    args = parser.parse_args()
    if args.command == "delete-episodes":
        _delete_episodes(args, logger)
        return
    _run(args, logger)


if __name__ == "__main__":
    main()

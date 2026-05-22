"""Episode recording to a LeRobot dataset, plus delete-episodes utility.

Supports the VR dataset panel actions: Record Episode, Save Episode, Save Dataset.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from lerobot.datasets.dataset_tools import delete_episodes
from lerobot.datasets.image_writer import safe_stop_image_writer
from lerobot.datasets.lerobot_dataset import HF_LEROBOT_HOME, LeRobotDataset
from lerobot.datasets.pipeline_features import (
    aggregate_pipeline_dataset_features,
    create_initial_features,
)
from lerobot.datasets.utils import (
    ACTION,
    OBS_STR,
    build_dataset_frame,
    combine_feature_dicts,
)
from lerobot.processor import make_default_processors

from vr_teleop.logs import log


def setup_dataset(robot, settings, logger) -> LeRobotDataset | None:
    """Create a LeRobotDataset for episode recording, or None if not configured."""
    if settings.dataset is None:
        return None

    teleop_action_processor, _robot_action_processor, robot_observation_processor = (
        make_default_processors()
    )

    dataset_features = combine_feature_dicts(
        aggregate_pipeline_dataset_features(
            pipeline=teleop_action_processor,
            initial_features=create_initial_features(action=robot.action_features),
            use_videos=True,
        ),
        aggregate_pipeline_dataset_features(
            pipeline=robot_observation_processor,
            initial_features=create_initial_features(observation=robot.observation_features),
            use_videos=True,
        ),
    )

    # Streaming encoding gives near-instant save_episode(). The codec is
    # configurable from YAML (`dataset.vcodec`). Defaults to ``h264`` (software
    # libx264 under the hood) since ``auto`` would pick ``h264_nvenc`` on any
    # box with an NVIDIA driver, which then crashes the encoder thread when
    # there's no CUDA device.
    VCODEC = settings.dataset.vcodec

    dataset_root = (
        Path(settings.dataset.root)
        if settings.dataset.root is not None
        else HF_LEROBOT_HOME / settings.dataset.repo_id
    )
    info_path = dataset_root / "meta" / "info.json"
    dataset_exists = info_path.exists()

    # An initialised-but-empty dataset dir confuses LeRobotDataset.__init__.
    if dataset_exists:
        with open(info_path) as f:
            info = json.load(f)
        if info.get("total_frames", 0) == 0:
            shutil.rmtree(dataset_root)
            dataset_exists = False

    # LeRobotDataset.create() calls ``root.mkdir(parents=True, exist_ok=False)``,
    # so any pre-existing directory (even an empty one left over from a crashed
    # run, manual ``mkdir`` etc.) makes it raise FileExistsError. Wipe a stale
    # directory if it's clearly not a dataset.
    if not dataset_exists and dataset_root.exists():
        shutil.rmtree(dataset_root)

    if dataset_exists:
        dataset = LeRobotDataset(
            repo_id=settings.dataset.repo_id,
            root=settings.dataset.root,
            streaming_encoding=True,
        )
        log(
            f"📂 Resuming existing dataset: {settings.dataset.repo_id} "
            f"({dataset.num_episodes} episodes so far)",
            logger,
        )
    else:
        dataset = LeRobotDataset.create(
            repo_id=settings.dataset.repo_id,
            fps=settings.fps,
            root=settings.dataset.root,
            robot_type=robot.name,
            features=dataset_features,
            use_videos=True,
            vcodec=VCODEC,
            streaming_encoding=True,
        )
        log(f"📁 Dataset recording enabled: {settings.dataset.repo_id}", logger)
    log(f"🎬 Streaming video encoding active (vcodec={VCODEC})", logger)
    return dataset


def end_active_episode(dataset: LeRobotDataset | None, logger) -> None:
    """Save and close the current episode if any frames were recorded."""
    if dataset is None:
        return
    episode_buffer = getattr(dataset, "episode_buffer", None)
    frame_indices = (
        episode_buffer.get("frame_index", []) if isinstance(episode_buffer, dict) else []
    )
    if not frame_indices:
        log("⚠️ No frames recorded, skipping empty episode.", logger)
        return
    dataset.save_episode()
    log(
        f"✅ Episode {dataset.num_episodes - 1} saved "
        f"({dataset.num_frames} total frames)",
        logger,
    )


def record_step(
    dataset: LeRobotDataset | None,
    settings,
    obs: dict[str, Any],
    action: dict[str, Any],
) -> None:
    """Record a single observation/action step into the dataset."""
    if dataset is None or settings.dataset is None:
        return

    observation_frame = build_dataset_frame(dataset.features, obs, prefix=OBS_STR)
    action_frame = build_dataset_frame(dataset.features, action, prefix=ACTION)
    frame = {
        **observation_frame,
        **action_frame,
        "task": settings.dataset.single_task,
    }
    dataset.add_frame(frame)


def finalize_dataset(
    dataset: LeRobotDataset | None,
    push_to_hub: bool,
    logger,
) -> None:
    """Flush buffers, finalize parquet files, optionally push to Hugging Face Hub."""
    if dataset is None:
        return

    streaming_encoder = getattr(dataset, "_streaming_encoder", None)
    if streaming_encoder is None:
        safe_stop_image_writer(dataset)
    else:
        streaming_encoder.close()
    dataset.finalize()
    log(f"📊 Total episodes recorded: {dataset.num_episodes}", logger)

    if push_to_hub:
        if dataset.num_episodes == 0:
            log("⚠️ No episodes recorded — skipping push to Hub.", logger)
            return
        try:
            log(f"🚀 Pushing dataset '{dataset.repo_id}' to Hugging Face Hub...", logger)
            dataset.push_to_hub(tags=["vr_teleop"])
            log(f"✅ Dataset '{dataset.repo_id}' pushed successfully.", logger)
        except Exception as e:
            log(f"❌ Failed to push dataset to Hub: {e}", logger)
            log(
                "   Make sure you are logged in (`huggingface-cli login`) "
                "or set HF_TOKEN.",
                logger,
            )


def delete_episodes_from_dataset(
    repo_id: str,
    episode_indices: list[int],
    root: str | None = None,
    push_to_hub: bool = False,
    logger=None,
) -> None:
    """Remove episodes from a dataset on disk (and optionally push the result)."""
    dataset_root = Path(root) if root is not None else HF_LEROBOT_HOME / repo_id

    if not (dataset_root / "meta" / "info.json").exists():
        raise FileNotFoundError(f"Dataset not found at {dataset_root}")

    dataset = LeRobotDataset(repo_id=repo_id, root=root)
    log(
        f"📂 Loaded dataset: {repo_id} "
        f"({dataset.num_episodes} episodes, {dataset.num_frames} frames)",
        logger,
    )
    log(f"🗑️  Deleting episodes: {episode_indices}", logger)

    tmp_repo_id = f"{repo_id}_tmp_delete"
    tmp_root = dataset_root.parent / f"{dataset_root.name}_tmp_delete"

    try:
        new_dataset = delete_episodes(
            dataset=dataset,
            episode_indices=episode_indices,
            output_dir=tmp_root,
            repo_id=tmp_repo_id,
        )
        log(
            f"✅ New dataset has {new_dataset.num_episodes} episodes, "
            f"{new_dataset.num_frames} frames",
            logger,
        )

        shutil.rmtree(dataset_root)
        tmp_root.rename(dataset_root)
        log(f"✅ Dataset at {dataset_root} updated in-place.", logger)

        if push_to_hub:
            updated_dataset = LeRobotDataset(repo_id=repo_id, root=root)
            log(f"🚀 Pushing updated dataset '{repo_id}' to Hub...", logger)
            updated_dataset.push_to_hub(tags=["vr_teleop"])
            log(f"✅ Dataset '{repo_id}' pushed successfully.", logger)
    except Exception:
        if tmp_root.exists():
            shutil.rmtree(tmp_root)
        raise

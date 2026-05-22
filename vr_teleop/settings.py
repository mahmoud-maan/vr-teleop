"""Load and validate teleop settings from a YAML file."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from lerobot.cameras.opencv.configuration_opencv import OpenCVCameraConfig
from lerobot.robots.robot import Robot
from lerobot.robots.so_follower import SOFollower
from lerobot.robots.so_follower.config_so_follower import (
    SOFollowerConfig,
    SOFollowerRobotConfig,
)
from lerobot.robots.bi_so_follower.bi_so_follower import BiSOFollower
from lerobot.robots.bi_so_follower.config_bi_so_follower import BiSOFollowerConfig


@dataclass
class CameraSettings:
    """Configuration for a single OpenCV-backed camera."""

    index: int
    width: int = 640
    height: int = 480
    fps: int = 30


@dataclass
class ArmSettings:
    """Per-arm configuration."""

    type: str
    port: str
    use_degrees: bool = True
    regularization: float = 1e-3
    end_effector_step_sizes: dict[str, float] = field(
        default_factory=lambda: {"x": 0.5, "y": 0.5, "z": 0.5}
    )
    end_effector_bounds: dict[str, list[float]] = field(
        default_factory=lambda: {"min": [-1.0, -1.0, -1.0], "max": [1.0, 1.0, 1.0]}
    )
    max_ee_step_m: float = 0.20
    gripper_speed_factor: float = 20.0
    cameras: list[str] = field(default_factory=list)


@dataclass
class TransportSettings:
    """Quest WebSocket server configuration."""

    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class CameraServerSettings:
    """HTTP MJPEG camera-streaming server configuration."""

    host: str = "0.0.0.0"
    port: int = 8765
    jpeg_quality: int = 80
    fps: int = 30


@dataclass
class DatasetSettings:
	"""Configuration for recording episodes to a LeRobot dataset."""

	repo_id: str
	single_task: str
	root: str | None = None
	push_to_hub: bool = False
	# Video codec used by LeRobot's streaming encoder for camera streams.
	# Must be one of the values LeRobot whitelists in ``VALID_VIDEO_CODECS``:
	#   "h264"               software H.264 via libx264 (CPU, portable default)
	#   "hevc"               software HEVC via libx265 (better compression, slower)
	#   "libsvtav1"          software AV1 via SVT-AV1
	#   "h264_nvenc" / "hevc_nvenc"          NVIDIA HW
	#   "h264_qsv"                           Intel QuickSync
	#   "h264_vaapi"                         Intel/AMD VAAPI
	#   "h264_videotoolbox" / "hevc_videotoolbox"  macOS
	#   "auto"               let FFmpeg pick (will use NVENC if any NVIDIA driver
	#                        is installed, even without a CUDA device — crashes).
	vcodec: str = "h264"


@dataclass
class Settings:
    """Top-level teleop settings."""

    id: str
    fps: int
    arms: dict[str, ArmSettings]
    cameras: dict[str, CameraSettings]
    transport: TransportSettings
    camera_server: CameraServerSettings
    dataset: DatasetSettings | None = None
    use_rerun: bool = False
    # Seconds to ramp the arm(s) back to the captured home pose on Reset.
    # 0 disables the ramp (single send_action at full servo speed).
    reset_duration_s: float = 1.5


def load_settings(path: str | Path) -> tuple[Robot, Settings]:
    """Load a YAML settings file and build a ready-to-use robot from it.

    Returns:
        A tuple of `(robot, settings)`. The robot is either a `SOFollower`
        (one arm defined) or a `BiSOFollower` (both `left` and `right` defined).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Settings file not found: {path}\n"
            "Pick a template from conf/ and adjust to match your hardware."
        )

    with open(path) as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    # Parse cameras
    cameras: dict[str, CameraSettings] = {}
    for name, cam in (raw.get("cameras") or {}).items():
        cameras[name] = CameraSettings(
            index=cam["index"],
            width=cam.get("width", 640),
            height=cam.get("height", 480),
            fps=cam.get("fps", 30),
        )

    # Parse arms (and validate camera references)
    arms: dict[str, ArmSettings] = {}
    for name, arm in (raw.get("arms") or {}).items():
        arm_cameras = arm.get("cameras", [])
        for cam_name in arm_cameras:
            if cam_name not in cameras:
                raise ValueError(
                    f"Arm '{name}' references camera '{cam_name}' which is not "
                    "defined in the cameras section."
                )
        arms[name] = ArmSettings(
            type=arm["type"],
            port=arm["port"],
            use_degrees=arm.get("use_degrees", True),
            regularization=arm.get("regularization", 1e-3),
            end_effector_step_sizes=arm.get(
                "end_effector_step_sizes", {"x": 0.5, "y": 0.5, "z": 0.5}
            ),
            end_effector_bounds=arm.get(
                "end_effector_bounds",
                {"min": [-1.0, -1.0, -1.0], "max": [1.0, 1.0, 1.0]},
            ),
            max_ee_step_m=arm.get("max_ee_step_m", 0.20),
            gripper_speed_factor=arm.get("gripper_speed_factor", 20.0),
            cameras=arm_cameras,
        )

    if not arms:
        raise ValueError("Settings file must define at least one arm under 'arms:'.")

    robot_section = raw.get("robot", {}) or {}
    transport_section = raw.get("transport", {}) or {}
    camera_server_section = raw.get("camera_server", {}) or {}

    dataset_section = raw.get("dataset")
    dataset_cfg: DatasetSettings | None = None
    if dataset_section is not None:
        repo_id = dataset_section.get("repo_id")
        single_task = dataset_section.get("single_task")
        if not repo_id or not single_task:
            raise ValueError("dataset section requires both 'repo_id' and 'single_task'.")
        dataset_cfg = DatasetSettings(
            repo_id=repo_id,
            single_task=single_task,
            root=dataset_section.get("root"),
            push_to_hub=dataset_section.get("push_to_hub", False),
            vcodec=dataset_section.get("vcodec", "h264"),
        )

    settings = Settings(
        id=robot_section.get("id", "vr_teleop"),
        fps=robot_section.get("fps", 30),
        arms=arms,
        cameras=cameras,
        transport=TransportSettings(
            host=transport_section.get("host", "0.0.0.0"),
            port=transport_section.get("port", 8080),
        ),
        camera_server=CameraServerSettings(
            host=camera_server_section.get("host", "0.0.0.0"),
            port=camera_server_section.get("port", 8765),
            jpeg_quality=camera_server_section.get("jpeg_quality", 80),
            fps=camera_server_section.get("fps", 30),
        ),
        dataset=dataset_cfg,
        use_rerun=robot_section.get("use_rerun", False),
        reset_duration_s=float(robot_section.get("reset_duration_s", 1.5)),
    )

    robot = _build_robot(settings)
    return robot, settings


def _build_robot(settings: Settings) -> Robot:
    # Materialise OpenCVCameraConfig for each declared camera.
    camera_configs = {
        name: OpenCVCameraConfig(
            index_or_path=cam.index,
            width=cam.width,
            height=cam.height,
            fps=cam.fps,
        )
        for name, cam in settings.cameras.items()
    }

    arm_configs = {
        name: SOFollowerConfig(
            port=arm.port,
            use_degrees=arm.use_degrees,
            cameras={cam_name: camera_configs[cam_name] for cam_name in arm.cameras},
        )
        for name, arm in settings.arms.items()
    }

    if len(arm_configs) == 1:
        name, arm_config = next(iter(arm_configs.items()))
        return SOFollower(
            SOFollowerRobotConfig(
                port=arm_config.port,
                use_degrees=arm_config.use_degrees,
                cameras=arm_config.cameras,
                id=f"{settings.id}_{name}",
            )
        )

    if {"left", "right"} <= arm_configs.keys():
        return BiSOFollower(
            BiSOFollowerConfig(
                left_arm_config=arm_configs["left"],
                right_arm_config=arm_configs["right"],
                id=settings.id,
            )
        )

    raise ValueError(
        f"Unsupported arm combination: {list(arm_configs.keys())}. "
        "Use either one arm, or exactly 'left' and 'right'."
    )

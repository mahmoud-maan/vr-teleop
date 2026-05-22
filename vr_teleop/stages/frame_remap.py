"""Stage 1 — Map raw Quest controller pose into the robot's target action.

  - Quaternion → rotation vector.
  - Axis remap from WebXR / Quest (Y-up, right-handed) into the SO-arm base
    frame: target_x = -pos_z, target_y = -pos_x, target_z = pos_y, with the
    matching inversions on the rotation vector components.
  - Joystick X → gripper velocity.

When `enabled` is false, all target deltas are zeroed so the arm holds its
last commanded pose; the gripper command is still passed through so the
operator can open / close the gripper with the controller down.
"""

from dataclasses import dataclass, field

from lerobot.configs.types import FeatureType, PipelineFeatureType, PolicyFeature
from lerobot.processor import (
    ProcessorStepRegistry,
    RobotAction,
    RobotActionProcessorStep,
)
from lerobot.utils.rotation import Rotation


@ProcessorStepRegistry.register("vr_teleop_frame_remap")
@dataclass
class FrameRemap(RobotActionProcessorStep):
    """Remap one Quest controller's pose into the robot's action dict."""

    _enabled_prev: bool = field(default=False, init=False, repr=False)

    def action(self, action: RobotAction) -> RobotAction:
        enabled = bool(action.pop("enabled"))
        joystick_x = action.pop("joystickX")
        pos = action.pop("pos")
        rot = action.pop("rot")

        if pos is None or rot is None:
            raise ValueError("Quest frame is missing 'pos' or 'rot'.")

        rotvec = Rotation.from_quat(rot).as_rotvec()

        action["enabled"] = enabled
        action["target_x"] = -pos[2] if enabled else 0.0
        action["target_y"] = -pos[0] if enabled else 0.0
        action["target_z"] = pos[1] if enabled else 0.0
        action["target_wx"] = -rotvec[1] if enabled else 0.0
        action["target_wy"] = -rotvec[0] if enabled else 0.0
        action["target_wz"] = -rotvec[2] if enabled else 0.0
        action["gripper_vel"] = joystick_x
        return action

    def transform_features(
        self, features: dict[PipelineFeatureType, dict[str, PolicyFeature]]
    ) -> dict[PipelineFeatureType, dict[str, PolicyFeature]]:
        for feat in ("enabled", "pos", "rot"):
            features[PipelineFeatureType.ACTION].pop(feat, None)
        for feat in (
            "enabled",
            "target_x",
            "target_y",
            "target_z",
            "target_wx",
            "target_wy",
            "target_wz",
            "gripper_vel",
        ):
            features[PipelineFeatureType.ACTION][feat] = PolicyFeature(
                type=FeatureType.ACTION, shape=(1,)
            )
        return features

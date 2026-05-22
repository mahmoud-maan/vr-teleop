"""LeRobot pipeline stages re-exported under a stable vr_teleop namespace.

Wrapping these gives vr_teleop a single seam to update if LeRobot moves or
renames these classes, rather than chasing the import across multiple files.

  Stage 2 — EEReferenceAndDelta  : latch a reference EE pose, emit deltas
  Stage 3 — EEBoundsAndSafety    : clamp target into workspace box, limit step
  Stage 4 — GripperVelocityToJoint: integrate gripper velocity → joint position
  Stage 5 — InverseKinematicsEEToJoints: solve IK, emit joint targets
"""

from lerobot.robots.so_follower.robot_kinematic_processor import (
    EEBoundsAndSafety,
    EEReferenceAndDelta,
    GripperVelocityToJoint,
    InverseKinematicsEEToJoints,
)

__all__ = [
    "EEReferenceAndDelta",
    "EEBoundsAndSafety",
    "GripperVelocityToJoint",
    "InverseKinematicsEEToJoints",
]

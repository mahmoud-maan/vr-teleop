"""Arm hardware abstractions and URDF-backed IK solver for SO-101 arms.

Hardware boundary: the runtime imports SOFollower / BiSOFollower from here so
that swapping to a different motor brand or a simulation backend only requires
touching this file.
"""

import os

from lerobot.model.kinematics import RobotKinematics
from lerobot.robots.bi_so_follower.bi_so_follower import BiSOFollower
from lerobot.robots.so_follower.so_follower import SOFollower

from vr_teleop import ASSETS_DIR

__all__ = [
    "BiSOFollower",
    "SOFollower",
    "is_dual_arm",
    "is_single_arm",
    "SO101Solver",
    "build_solver",
]


def is_dual_arm(robot) -> bool:
    return isinstance(robot, BiSOFollower)


def is_single_arm(robot) -> bool:
    return isinstance(robot, SOFollower)


class SO101Solver(RobotKinematics):
    """Placo-based IK solver for SO-ARM101 arms.

    The L2 regularization term prevents singularity-induced oscillation when
    the arm is at full extension by keeping the QP well-conditioned, so the
    solver returns a stable, unique solution near workspace boundaries.

    URDF and meshes live outside the Python package, under `assets/so101/`.
    """

    URDF_PATH = ASSETS_DIR / "so101" / "so101_new_calib.urdf"

    def __init__(self, motor_names: list[str], regularization: float = 1e-3):
        # placo's C++ layer writes self-collision warnings directly to fd 1/2,
        # bypassing Python's sys.stdout/stderr. Silence at the OS fd level so
        # the import is quiet.
        with open(os.devnull, "w") as devnull:
            saved_stdout = os.dup(1)
            saved_stderr = os.dup(2)
            try:
                os.dup2(devnull.fileno(), 1)
                os.dup2(devnull.fileno(), 2)
                super().__init__(
                    urdf_path=str(self.URDF_PATH),
                    target_frame_name="gripper_frame_link",
                    joint_names=motor_names,
                )
            finally:
                os.dup2(saved_stdout, 1)
                os.dup2(saved_stderr, 2)
                os.close(saved_stdout)
                os.close(saved_stderr)
        self.solver.add_regularization_task(regularization)


_SOLVERS: dict[str, type[RobotKinematics]] = {"so100": SO101Solver}


def build_solver(
    arm_type: str,
    motor_names: list[str],
    regularization: float = 1e-3,
) -> RobotKinematics:
    """Construct an IK solver for the given arm type."""
    cls = _SOLVERS.get(arm_type)
    if cls is None:
        raise ValueError(
            f"Unknown arm type '{arm_type}'. Supported types: {', '.join(_SOLVERS)}"
        )
    return cls(motor_names, regularization=regularization)

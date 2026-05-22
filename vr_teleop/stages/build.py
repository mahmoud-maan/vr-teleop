"""Assemble the five stages into the full Quest → joint-target pipeline."""

from lerobot.model.kinematics import RobotKinematics
from lerobot.processor import RobotAction, RobotObservation, RobotProcessorPipeline
from lerobot.processor.converters import (
    robot_action_observation_to_transition,
    transition_to_robot_action,
)

from vr_teleop.stages.frame_remap import FrameRemap
from vr_teleop.stages.lerobot_stages import (
    EEBoundsAndSafety,
    EEReferenceAndDelta,
    GripperVelocityToJoint,
    InverseKinematicsEEToJoints,
)


def build_pipeline(
    motor_names: list[str],
    kinematics_solver: RobotKinematics,
    end_effector_step_sizes: dict[str, float],
    end_effector_bounds: dict[str, list[float]],
    max_ee_step_m: float,
    gripper_speed_factor: float,
) -> RobotProcessorPipeline[tuple[RobotAction, RobotObservation], RobotAction]:
    """Return a pipeline that turns one Quest frame into a joint action.

    The pipeline composes, in order:
        FrameRemap → EEReferenceAndDelta → EEBoundsAndSafety
                   → GripperVelocityToJoint → InverseKinematicsEEToJoints
    """
    return RobotProcessorPipeline[tuple[RobotAction, RobotObservation], RobotAction](
        steps=[
            FrameRemap(),
            EEReferenceAndDelta(
                kinematics=kinematics_solver,
                end_effector_step_sizes=end_effector_step_sizes,
                motor_names=motor_names,
                use_latched_reference=True,
            ),
            EEBoundsAndSafety(
                end_effector_bounds=end_effector_bounds,
                max_ee_step_m=max_ee_step_m,
            ),
            GripperVelocityToJoint(speed_factor=gripper_speed_factor),
            InverseKinematicsEEToJoints(
                kinematics=kinematics_solver,
                motor_names=motor_names,
                initial_guess_current_joints=True,
            ),
        ],
        to_transition=robot_action_observation_to_transition,
        to_output=transition_to_robot_action,
    )

"""Per-frame dispatch from the Quest socket to the IK pipeline(s)."""

import copy
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from lerobot.processor import RobotAction, RobotObservation
from lerobot.robots.robot import Robot

from vr_teleop.arm.solver import BiSOFollower, SOFollower, build_solver
from vr_teleop.settings import ArmSettings, Settings
from vr_teleop.stages.build import build_pipeline


def _scalar_action(d: dict[str, Any]) -> dict[str, float]:
    """Filter a (potentially image-laden) observation dict down to the numeric,
    scalar entries that ``send_action`` actually consumes (motor positions)."""
    return {k: float(v) for k, v in d.items() if isinstance(v, (int, float))}


def _smoothstep(t: float) -> float:
    """Cubic ease-in-out — gentle accelerate / decelerate at the endpoints."""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return t * t * (3.0 - 2.0 * t)


@dataclass
class _ResetRampState:
    """One step of a reset ramp is sent per main-loop tick (non-blocking)."""

    plans: list[tuple[Any, dict[str, float], dict[str, float]]]
    steps: int
    step_index: int = 0  # how many setpoints have been sent so far


def _build_reset_plans(
    arms: list[tuple[Any, dict[str, Any]]],
) -> list[tuple[Any, dict[str, float], dict[str, float]]]:
    plans: list[tuple[Any, dict[str, float], dict[str, float]]] = []
    for arm, target in arms:
        current = _scalar_action(arm.get_observation())
        end = _scalar_action(target)
        keys = current.keys() & end.keys()
        start_vals = {k: current[k] for k in keys}
        end_vals = {k: end[k] for k in keys}
        plans.append((arm, start_vals, end_vals))
    return plans


def _send_reset_step(
    plans: list[tuple[Any, dict[str, float], dict[str, float]]],
    step_index: int,
    steps: int,
) -> None:
    alpha = _smoothstep(step_index / steps)
    for arm, start_vals, end_vals in plans:
        action = {
            k: start_vals[k] + (end_vals[k] - start_vals[k]) * alpha
            for k in start_vals
        }
        arm.send_action(action)


class TeleopSession(ABC):
    """Owns the IK pipeline(s) and dispatches per-frame work to one or both arms."""

    def __init__(self, robot: Robot, settings: Settings):
        self.robot = robot
        self.settings = settings
        self.has_initial_position = True
        self._reset_state: _ResetRampState | None = None

    def is_resetting(self) -> bool:
        return self._reset_state is not None

    def _begin_reset_ramp(self, arms: list[tuple[Any, dict[str, Any]]]) -> None:
        """Start a non-blocking ramp to home. Advance with ``tick_reset()``."""
        if self.is_resetting():
            return

        duration_s = self.settings.reset_duration_s
        fps = max(int(self.settings.fps), 1)

        if duration_s <= 0.0:
            for arm, target in arms:
                arm.send_action(_scalar_action(target))
            self._finish_reset()
            return

        steps = max(int(duration_s * fps), 1)
        print(f"Resetting to initial position over {duration_s:.2f}s...")
        self._reset_state = _ResetRampState(
            plans=_build_reset_plans(arms),
            steps=steps,
        )

    def tick_reset(self) -> bool:
        """Advance the reset ramp by one setpoint. Returns True when idle/done."""
        if self._reset_state is None:
            return True

        state = self._reset_state
        state.step_index += 1
        _send_reset_step(state.plans, state.step_index, state.steps)

        if state.step_index >= state.steps:
            self._finish_reset()
            return True
        return False

    def _finish_reset(self) -> None:
        self._reset_state = None
        self._build_pipelines()
        self.has_initial_position = True

    def _build_pipeline_for(self, motor_names: list[str], arm: ArmSettings):
        solver = build_solver(
            arm_type=arm.type,
            motor_names=motor_names,
            regularization=arm.regularization,
        )
        return build_pipeline(
            motor_names=motor_names,
            kinematics_solver=solver,
            end_effector_step_sizes=arm.end_effector_step_sizes,
            end_effector_bounds=arm.end_effector_bounds,
            max_ee_step_m=arm.max_ee_step_m,
            gripper_speed_factor=arm.gripper_speed_factor,
        )

    @abstractmethod
    def _build_pipelines(self) -> None: ...

    @abstractmethod
    def capture_initial_observations(self) -> None: ...

    @abstractmethod
    def reset(self) -> None: ...

    @abstractmethod
    def handle_frame(
        self, frame: dict
    ) -> tuple[RobotObservation, RobotAction] | None: ...


class SingleArmSession(TeleopSession):
    """Drives one SOFollower arm from one Quest controller."""

    def __init__(self, robot: SOFollower, settings: Settings):
        super().__init__(robot, settings)
        self.arm_name = next(iter(settings.arms))  # "left" or "right"
        self._build_pipelines()

    def _build_pipelines(self) -> None:
        self.pipeline = self._build_pipeline_for(
            list(self.robot.bus.motors.keys()),
            arm=self.settings.arms[self.arm_name],
        )

    def capture_initial_observations(self) -> None:
        self.initial_obs = self.robot.get_observation()

    def reset(self) -> None:
        self._begin_reset_ramp([(self.robot, self.initial_obs)])

    def handle_frame(
        self, frame: dict
    ) -> tuple[RobotObservation, RobotAction] | None:
        controller = copy.deepcopy(frame[self.arm_name])
        if controller["enabled"]:
            self.has_initial_position = False

        obs = self.robot.get_observation()
        action = self.pipeline((controller, obs))
        self.robot.send_action(action)
        return obs, action


class DualArmSession(TeleopSession):
    """Drives a BiSOFollower from both Quest controllers."""

    def __init__(self, robot: BiSOFollower, settings: Settings):
        super().__init__(robot, settings)
        self._build_pipelines()

    def _build_pipelines(self) -> None:
        self.pipelines = {
            "left": self._build_pipeline_for(
                list(self.robot.left_arm.bus.motors.keys()),
                arm=self.settings.arms["left"],
            ),
            "right": self._build_pipeline_for(
                list(self.robot.right_arm.bus.motors.keys()),
                arm=self.settings.arms["right"],
            ),
        }

    def capture_initial_observations(self) -> None:
        self.initial_left_obs = self.robot.left_arm.get_observation()
        self.initial_right_obs = self.robot.right_arm.get_observation()

    def reset(self) -> None:
        self._begin_reset_ramp(
            [
                (self.robot.right_arm, self.initial_right_obs),
                (self.robot.left_arm, self.initial_left_obs),
            ]
        )

    def handle_frame(
        self, frame: dict
    ) -> tuple[RobotObservation, RobotAction] | None:
        combined_obs: RobotObservation = {}
        combined_action: RobotAction = {}

        for side in ("right", "left"):
            arm = getattr(self.robot, f"{side}_arm")
            controller = copy.deepcopy(frame[side])
            if controller["enabled"]:
                self.has_initial_position = False

            obs = arm.get_observation()
            action = self.pipelines[side]((controller, obs))
            arm.send_action(action)

            combined_obs.update({f"{side}_{k}": v for k, v in obs.items()})
            combined_action.update({f"{side}_{k}": v for k, v in action.items()})

        # Surface top-level camera observations (which live on the parent robot,
        # not on the per-arm followers) so the loop can stream / record them.
        try:
            full_obs = self.robot.get_observation()
            for key, value in full_obs.items():
                if key not in combined_obs:
                    combined_obs[key] = value
        except Exception:
            pass

        return combined_obs, combined_action


def build_session(robot: Robot, settings: Settings) -> TeleopSession:
    """Pick the right session subclass for the configured robot."""
    if isinstance(robot, SOFollower):
        return SingleArmSession(robot, settings)
    if isinstance(robot, BiSOFollower):
        return DualArmSession(robot, settings)
    raise TypeError(f"Unsupported robot type: {type(robot).__name__}")

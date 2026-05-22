"""Lightweight logging helpers shared across the runtime."""

import logging
import time

TIMING_INTERVAL = 100


def get_logger(name: str = "vr_teleop") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log(message: str, logger: logging.Logger | None = None) -> None:
    (logger or get_logger()).info(message)


def maybe_log_loop_timing(
    logger: logging.Logger,
    loop_count: int,
    t_start: float,
    t_control: float | None = None,
) -> None:
    if loop_count % TIMING_INTERVAL != 0:
        return
    total = time.perf_counter() - t_start
    parts = [f"total: {total * 1000:.1f}ms", f"effective fps: {1 / total:.1f}"]
    if t_control is not None:
        parts.insert(0, f"control+IK: {(t_control - t_start) * 1000:.1f}ms")
    logger.info(f"⏱ Loop {loop_count} | " + " | ".join(parts))

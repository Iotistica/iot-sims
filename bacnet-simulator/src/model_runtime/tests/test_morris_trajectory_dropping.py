"""A Morris trajectory (num_vars+1 consecutive rows in the sample matrix:
a base point plus one one-at-a-time step per tuner) used to abort the
entire sensitivity run if even a single evaluation in it failed -- e.g. a
tuner corner that makes the FMU replay genuinely non-convergent, such as
an aggressive SAT-loop gain interacting with a real setpoint/fan-schedule
step in the RTU historical data (see the real CI failure this guards:
CVode's mxstep exhausted at the same historical timestamp across several
different sampled tuner corners).

evaluate_morris_trajectories() now drops (and records) only the failing
trajectory and keeps going, since the elementary-effect calculation needs
complete trajectories -- a partial one can't contribute at all. These
tests exercise that control flow directly via a fake `evaluate` callable,
with no FMU involved.
"""
from __future__ import annotations

import numpy as np
import pytest

from shared.run_sensitivity import SensitivityError, evaluate_morris_trajectories

PARAMETER_NAMES = ["a", "b"]
NUM_VARS = len(PARAMETER_NAMES)
TRAJECTORY_SIZE = NUM_VARS + 1


def _samples(num_trajectories: int) -> np.ndarray:
    """A distinctly-valued sample matrix -- row i is filled with the
    float i, so tests can pin exactly which row a fake `evaluate` saw."""
    total_rows = num_trajectories * TRAJECTORY_SIZE
    return np.arange(total_rows * NUM_VARS, dtype=float).reshape(total_rows, NUM_VARS)


def test_all_trajectories_succeed():
    samples = _samples(2)

    result = evaluate_morris_trajectories(
        samples=samples,
        num_vars=NUM_VARS,
        parameter_names=PARAMETER_NAMES,
        evaluate=lambda sample: float(sample.sum()),
        metric="cv_rmse",
        log=lambda msg: None,
    )

    assert result.num_trajectories == 2
    assert result.completed_trajectories == 2
    assert result.dropped_count == 0
    assert result.dropped_trajectories == []
    assert result.reliable is True
    assert result.evaluations_completed == 2 * TRAJECTORY_SIZE
    assert result.samples_kept.shape == samples.shape
    assert np.array_equal(result.samples_kept, samples)
    assert result.scores_kept.tolist() == [
        float(row.sum()) for row in samples
    ]


def test_single_failing_trajectory_is_dropped_entirely():
    samples = _samples(3)
    # Row index 4 is trajectory 1's 2nd row (trajectory_size=3): 0,1,2 =
    # trajectory 0; 3,4,5 = trajectory 1; 6,7,8 = trajectory 2.
    failing_row = 4

    def evaluate(sample: np.ndarray) -> float:
        row_index = int(sample[0] // NUM_VARS)
        if row_index == failing_row:
            raise RuntimeError("mxstep steps taken before reaching tout")
        return float(sample.sum())

    result = evaluate_morris_trajectories(
        samples=samples,
        num_vars=NUM_VARS,
        parameter_names=PARAMETER_NAMES,
        evaluate=evaluate,
        metric="cv_rmse",
        log=lambda msg: None,
    )

    assert result.num_trajectories == 3
    assert result.completed_trajectories == 2
    assert result.dropped_count == 1
    assert result.dropped_rate == pytest.approx(1 / 3)
    assert result.reliable is False  # 1/3 dropped is well above the 10% threshold

    dropped = result.dropped_trajectories[0]
    assert dropped["trajectory_index"] == 1
    assert dropped["failed_row_in_trajectory"] == 1
    assert dropped["parameters"] == {"a": samples[failing_row][0], "b": samples[failing_row][1]}
    assert "mxstep" in dropped["error"]

    # Only trajectories 0 and 2's rows survive, not trajectory 1's.
    assert result.samples_kept.shape[0] == 2 * TRAJECTORY_SIZE
    kept_row_sums = {row.sum() for row in result.samples_kept}
    dropped_trajectory_rows = samples[TRAJECTORY_SIZE:2 * TRAJECTORY_SIZE]
    for row in dropped_trajectory_rows:
        assert row.sum() not in kept_row_sums


def test_reliable_flag_true_at_exactly_10_percent_dropped():
    samples = _samples(10)
    failing_row = 0  # trajectory 0 only

    def evaluate(sample: np.ndarray) -> float:
        row_index = int(sample[0] // NUM_VARS)
        if row_index == failing_row:
            raise RuntimeError("boom")
        return 1.0

    result = evaluate_morris_trajectories(
        samples=samples,
        num_vars=NUM_VARS,
        parameter_names=PARAMETER_NAMES,
        evaluate=evaluate,
        metric="cv_rmse",
        log=lambda msg: None,
    )

    assert result.dropped_count == 1
    assert result.dropped_rate == pytest.approx(0.10)
    assert result.reliable is True


def test_reliable_flag_false_above_10_percent_dropped():
    samples = _samples(10)
    failing_rows = {0, TRAJECTORY_SIZE}  # trajectories 0 and 1 -> 20%

    def evaluate(sample: np.ndarray) -> float:
        row_index = int(sample[0] // NUM_VARS)
        if row_index in failing_rows:
            raise RuntimeError("boom")
        return 1.0

    result = evaluate_morris_trajectories(
        samples=samples,
        num_vars=NUM_VARS,
        parameter_names=PARAMETER_NAMES,
        evaluate=evaluate,
        metric="cv_rmse",
        log=lambda msg: None,
    )

    assert result.dropped_count == 2
    assert result.dropped_rate == pytest.approx(0.20)
    assert result.reliable is False


def test_raises_when_every_trajectory_fails():
    samples = _samples(2)

    def evaluate(sample: np.ndarray) -> float:
        raise RuntimeError("always fails")

    with pytest.raises(SensitivityError, match="All 2 Morris trajectories failed"):
        evaluate_morris_trajectories(
            samples=samples,
            num_vars=NUM_VARS,
            parameter_names=PARAMETER_NAMES,
            evaluate=evaluate,
            metric="cv_rmse",
            log=lambda msg: None,
        )


def test_progress_logs_by_evaluation_count_not_trajectory_count():
    """Progress used to fire every N *trajectories* -- a real regression
    (RTU has 8 tuners = 9 evaluations per trajectory, so the gap between
    progress lines silently grew 9x versus the old per-sample loop),
    which combined with FMPy's per-evaluation warning noise made a long
    run look hung. It must fire every N *evaluations* instead."""
    samples = _samples(5)  # 5 trajectories * 3 rows/trajectory = 15 evaluations
    messages: list[str] = []

    evaluate_morris_trajectories(
        samples=samples,
        num_vars=NUM_VARS,
        parameter_names=PARAMETER_NAMES,
        evaluate=lambda sample: 1.0,
        metric="cv_rmse",
        progress_every=2,
        log=messages.append,
    )

    progress_messages = [m for m in messages if m.strip().startswith("completed")]
    assert len(progress_messages) == 15 // 2
    for msg in progress_messages:
        assert "evaluations" in msg

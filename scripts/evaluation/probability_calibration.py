from __future__ import annotations

import math
from typing import Any

from scripts.core.models import parse_ts


def build_expanding_walk_forward_splits(
    records: list[dict[str, Any]],
    *,
    horizon: str,
    evaluation_time: str,
    minimum_train_size: int,
) -> list[dict[str, Any]]:
    """Build test folds using only labels mature at each decision cutoff."""
    evaluation_cutoff = parse_ts(evaluation_time)
    same_horizon = sorted(
        (record for record in records if record.get("horizon") == horizon),
        key=lambda record: parse_ts(str(record["decision_time"])),
    )
    folds: list[dict[str, Any]] = []
    for test_record in same_horizon:
        test_decision = parse_ts(str(test_record["decision_time"]))
        test_maturity = parse_ts(str(test_record["label_matured_at"]))
        if test_maturity > evaluation_cutoff:
            continue
        training = [
            record
            for record in same_horizon
            if parse_ts(str(record["decision_time"])) < test_decision
            and parse_ts(str(record["label_matured_at"])) <= test_decision
        ]
        if len(training) < minimum_train_size:
            continue
        folds.append(
            {
                "horizon": horizon,
                "training_cutoff_time": test_decision.isoformat(),
                "training_sample_size": len(training),
                "training_records": training,
                "test_record": test_record,
            }
        )
    return folds


def multiclass_brier_score(
    probabilities: dict[str, float],
    actual_bucket: str,
) -> float:
    _validate_distribution(probabilities, actual_bucket)
    return sum(
        (float(probability) - (1.0 if bucket == actual_bucket else 0.0)) ** 2
        for bucket, probability in probabilities.items()
    )


def multiclass_log_loss(
    probabilities: dict[str, float],
    actual_bucket: str,
    *,
    epsilon: float = 1e-15,
) -> float:
    _validate_distribution(probabilities, actual_bucket)
    probability = min(1.0, max(epsilon, float(probabilities[actual_bucket])))
    return -math.log(probability)


def calibration_diagnostics(
    records: list[dict[str, Any]],
    *,
    bins: int = 10,
) -> dict[str, Any]:
    """Return ECE and a reliability curve as diagnostics, never a sole gate."""
    if bins <= 0:
        raise ValueError("bins must be positive")
    grouped: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for record in records:
        probabilities = {
            str(key): float(value)
            for key, value in record["probabilities"].items()
        }
        actual = str(record["actual_bucket"])
        _validate_distribution(probabilities, actual)
        predicted = max(probabilities, key=probabilities.get)
        confidence = probabilities[predicted]
        index = min(bins - 1, int(confidence * bins))
        grouped[index].append((confidence, 1.0 if predicted == actual else 0.0))
    total = sum(len(group) for group in grouped)
    curve: list[dict[str, Any]] = []
    ece = 0.0
    for index, group in enumerate(grouped):
        if not group:
            continue
        confidence = sum(item[0] for item in group) / len(group)
        accuracy = sum(item[1] for item in group) / len(group)
        if total:
            ece += len(group) / total * abs(accuracy - confidence)
        curve.append(
            {
                "bin_lower": index / bins,
                "bin_upper": (index + 1) / bins,
                "sample_size": len(group),
                "mean_confidence": confidence,
                "observed_accuracy": accuracy,
            }
        )
    return {"ece": ece, "reliability_curve": curve, "sample_size": total}


def uncalibrated_metadata(horizon: str) -> dict[str, Any]:
    return {
        "calibration_status": "uncalibrated",
        "calibration_version": "none",
        "calibration_training_cutoff_time": None,
        "calibration_sample_size": 0,
        "calibration_horizon": horizon,
    }


def _validate_distribution(
    probabilities: dict[str, float],
    actual_bucket: str,
) -> None:
    if actual_bucket not in probabilities:
        raise ValueError("actual bucket is absent from probability distribution")
    values = [float(value) for value in probabilities.values()]
    if any(value < 0 or value > 1 for value in values):
        raise ValueError("probabilities must be inside [0, 1]")
    if abs(sum(values) - 1.0) > 1e-6:
        raise ValueError("probabilities must sum to 1")

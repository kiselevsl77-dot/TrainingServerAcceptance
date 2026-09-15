"""Валидаторы диапазонов форм (NFR-5).

Диапазоны взяты из спецификации `SOM1.json` (поля с `minimum`/`maximum`):
    batch_size 1–256, epochs 1–1000, learning_rate >0…1, threshold 0–1,
    аугментация 0.001–0.05 и 1–10.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RangeSpec:
    """Границы допустимого значения."""

    low: float
    high: float
    low_inclusive: bool = True
    high_inclusive: bool = True


BATCH_SIZE = RangeSpec(1, 256)
EPOCHS = RangeSpec(1, 1000)
LEARNING_RATE = RangeSpec(0.0, 1.0, low_inclusive=False, high_inclusive=True)
THRESHOLD = RangeSpec(0.0, 1.0)
AUGMENTATION_NOISE = RangeSpec(0.001, 0.05)
AUGMENTATION_SAMPLES = RangeSpec(1, 10)


def validate_range(value: float, spec: RangeSpec) -> list[str]:
    """Возвращает список сообщений об ошибках (пусто, если значение допустимо)."""
    errors: list[str] = []

    if spec.low_inclusive:
        if value < spec.low:
            errors.append(f"Значение должно быть ≥ {spec.low}")
    elif value <= spec.low:
        errors.append(f"Значение должно быть > {spec.low}")

    if spec.high_inclusive:
        if value > spec.high:
            errors.append(f"Значение должно быть ≤ {spec.high}")
    elif value >= spec.high:
        errors.append(f"Значение должно быть < {spec.high}")

    return errors

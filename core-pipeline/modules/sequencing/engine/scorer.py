def clamp_confidence(value: float | None) -> float:
    """Normalize sequencing confidence to [0, 1]. Values above 1 are invalid → 0."""
    if value is None:
        return 0.0
    v = float(value)
    if v > 1.0 or v < 0.0:
        return 0.0
    return v

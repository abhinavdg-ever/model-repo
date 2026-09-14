from . import gliner_large_v2_1, gliner_low, gliner_medium_v2_1

DOWNLOADERS = (
    gliner_large_v2_1,
    gliner_medium_v2_1,
    gliner_low,
)

__all__ = [
    "DOWNLOADERS",
    "gliner_large_v2_1",
    "gliner_low",
    "gliner_medium_v2_1",
]

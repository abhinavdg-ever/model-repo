"""Document continuity: which document each page belongs to."""
from .engine import PageInput, assign, judge, load_rules

__all__ = ["PageInput", "assign", "judge", "load_rules"]

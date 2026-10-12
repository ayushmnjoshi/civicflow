"""Application-wide constants.

The database column ``applications.priority`` is an ``Integer``.
This enum provides the canonical label ↔ integer mapping.
"""

from __future__ import annotations

import enum


class Priority(int, enum.Enum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2

    @classmethod
    def from_label(cls, label: str) -> "Priority":
        """Parse a label (case-insensitive).  Raises ValueError for unknowns."""
        try:
            return cls[label.upper()]
        except KeyError:
            valid = ", ".join(p.name for p in cls)
            raise ValueError(f"Invalid priority '{label}'. Valid values: {valid}.")

    @property
    def label(self) -> str:
        return self.name

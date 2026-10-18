"""Shared, serializable wizard mechanics; specialized rules stay in domain services."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Step:
    key: str
    prompt: str
    optional: bool = False
    max_length: int = 4000


def advance(draft: dict[str, Any], steps: tuple[Step, ...], value: str | None) -> dict[str, Any]:
    position = int(draft["step"])
    if not 0 <= position < len(steps):
        raise ValueError("Wizard already completed")
    step = steps[position]
    if value is None and not step.optional:
        raise ValueError("Required field")
    if value is not None and (not value.strip() or len(value) > step.max_length):
        raise ValueError("Invalid field length")
    result = dict(draft)
    result["values"] = {**draft.get("values", {}), step.key: value}
    result["step"], result["revision"] = position + 1, int(draft["revision"]) + 1
    return result


def back(draft: dict[str, Any]) -> dict[str, Any]:
    return {**draft, "step": max(0, int(draft["step"]) - 1), "revision": int(draft["revision"]) + 1}

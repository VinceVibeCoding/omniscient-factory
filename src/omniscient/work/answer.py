"""The researcher's JSON answer and the one-line reasons recorded for failures."""

from __future__ import annotations

from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    field_validator,
)

from omniscient.work.tickets import GIST_MAX_CHARS

REASON_MAX_CHARS = 300


class Answer(BaseModel):
    """The researcher's JSON answer. Control lines are neutralised by `resolve()`."""

    model_config = ConfigDict(extra="ignore")
    answer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    gist: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

    @field_validator("gist")
    @classmethod
    def _shorten_gist(cls, value: str) -> str:
        """A gist is a summary line: shorten it at a word boundary, never reject it."""
        flat = " ".join(value.split())
        if len(flat) <= GIST_MAX_CHARS:
            return flat
        return flat[: GIST_MAX_CHARS - 1].rsplit(" ", 1)[0] + "…"


def failure_reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        fields = "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()
        )
        reason = f"invalid answer: {fields}"
    else:
        lines = str(exc).splitlines()
        reason = f"{type(exc).__name__}: {lines[0] if lines else ''}"
    return reason[:REASON_MAX_CHARS]

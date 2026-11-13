"""Operational records in the vault: the append-only cost log."""

from __future__ import annotations

from dataclasses import astuple, dataclass
from datetime import date, datetime
from pathlib import Path

COST_LOG = Path("ops") / "costs.md"
CYCLE_LOG = Path("ops") / "cycles.md"
CYCLE_HEADER = "| started |\n|---|\n"
COST_HEADER = (
    "| date | task | role | model | effort | usd |\n|---|---|---|---|---|---|\n"
)


COST_STAMP_FORMAT = (
    "%d-%m-%Y-%H-%M-%S"  # owner's format: DD-MM-YYYY-HH-MM-SS, local time
)


def cost_stamp(at: datetime) -> str:
    """Format a timestamp for the cost log in the machine's local time zone."""
    return at.astimezone().strftime(COST_STAMP_FORMAT)


def record_cycle(vault: Path, at: datetime) -> None:
    """Append one cycle start to vault/ops/cycles.md."""
    log = vault / CYCLE_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    if not log.exists():
        log.write_text(CYCLE_HEADER, encoding="utf-8")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"| {cost_stamp(at)} |\n")


def cycles_on(vault: Path, day: date) -> int:
    """How many cycles started on one local calendar day."""
    log = vault / CYCLE_LOG
    if not log.is_file():
        return 0
    count = 0
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            at = datetime.strptime(line.strip().strip("|").strip(), COST_STAMP_FORMAT)
        except ValueError:
            continue
        count += at.date() == day
    return count


def spent_on(vault: Path, day: date) -> float:
    """Total USD logged in the cost log for one local calendar day."""
    log = vault / COST_LOG
    if not log.is_file():
        return 0.0
    total = 0.0
    for line in log.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        try:
            at = datetime.strptime(cells[0], COST_STAMP_FORMAT)
            usd = float(cells[-1])
        except ValueError:
            continue  # header, separator, or a malformed row
        if at.date() == day:
            total += usd
    return total


@dataclass(frozen=True)
class CostRow:
    at: datetime
    task: str
    role: str
    model: str
    effort: str
    usd: float


def append_cost(vault: Path, row: CostRow) -> None:
    """Append one row to vault/ops/costs.md, creating the table if needed."""
    for value in astuple(row):
        if "|" in str(value):
            raise ValueError(f"cost row field contains '|': {value!r}")
    log = vault / COST_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    if not log.exists():
        log.write_text(COST_HEADER, encoding="utf-8")
    stamp = cost_stamp(row.at)
    line = f"| {stamp} | {row.task} | {row.role} | {row.model} | {row.effort} | {row.usd:.4f} |\n"
    with log.open("a", encoding="utf-8") as handle:
        handle.write(line)

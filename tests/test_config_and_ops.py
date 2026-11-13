import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniscient.config import ConfigError, load_settings
from omniscient.home import packaged
from omniscient.ops import CostRow, append_cost, cost_stamp

REPO = Path(__file__).resolve().parents[1]


def test_load_settings_reads_real_config() -> None:
    settings = load_settings(packaged("templates") / "omniscient.toml")

    assert settings.budget.per_cycle_usd > 0
    assert settings.sources.news_lookback_days >= 1
    assert abs(sum(settings.scoring_weights.values()) - 1.0) < 1e-6


def test_load_settings_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.toml")


def test_load_settings_rejects_invalid_values(tmp_path: Path) -> None:
    bad = (
        (packaged("templates") / "omniscient.toml")
        .read_text()
        .replace("daily_usd = 5.00", "daily_usd = -1")
    )
    path = tmp_path / "o.toml"
    path.write_text(bad)

    with pytest.raises(ConfigError, match="budget.daily_usd"):
        load_settings(path)


def test_append_cost_adds_a_row_to_the_cost_log(tmp_path: Path) -> None:
    log = tmp_path / "ops" / "costs.md"
    log.parent.mkdir()
    log.write_text(
        "| date | task | role | model | effort | usd |\n|---|---|---|---|---|---|\n"
    )
    row = CostRow(
        at=datetime(2026, 11, 12, 20, 0, tzinfo=UTC),
        task="ingest run-1",
        role="ingest_tagger",
        model="anthropic/claude-haiku-5-5",
        effort="low",
        usd=0.0139,
    )

    append_cost(tmp_path, row)

    last = log.read_text().strip().splitlines()[-1]
    stamp = cost_stamp(row.at)
    assert re.fullmatch(
        r"\d{2}-\d{2}-\d{4}-\d{2}-\d{2}-\d{2}", stamp
    )  # DD-MM-YYYY-HH-MM-SS
    assert last == (
        f"| {stamp} | ingest run-1 | ingest_tagger | "
        "anthropic/claude-haiku-5-5 | low | 0.0139 |"
    )


def test_cost_stamp_uses_day_month_year_hour_minute_second() -> None:
    at = datetime(2026, 11, 12, 15, 4, 9).astimezone()  # local time, as logged

    assert cost_stamp(at) == "12-11-2026-15-04-09"


def test_append_cost_rejects_pipe_characters_that_would_break_the_table(
    tmp_path: Path,
) -> None:
    row = CostRow(
        at=datetime(2026, 11, 12, tzinfo=UTC),
        task="a|b",
        role="r",
        model="m",
        effort="low",
        usd=0.0,
    )

    with pytest.raises(ValueError, match="'|'"):
        append_cost(tmp_path, row)


def test_spent_on_sums_only_the_given_local_day(tmp_path: Path) -> None:
    from datetime import date

    from omniscient.ops import spent_on

    log = tmp_path / "ops" / "costs.md"
    log.parent.mkdir()
    log.write_text(
        "| date | task | role | model | effort | usd |\n|---|---|---|---|---|---|\n"
        "| 12-11-2026-15-11-00 | a | r | m | low | 0.0205 |\n"
        "| 12-11-2026-23-59-59 | b | r | m | low | 1.5000 |\n"
        "| 11-11-2026-10-00-00 | c | r | m | low | 9.0000 |\n"
        "| garbage row | x | x | x | x | nope |\n"
    )

    assert spent_on(tmp_path, date(2026, 11, 12)) == pytest.approx(1.5205)
    assert spent_on(tmp_path, date(2026, 11, 13)) == 0.0


def test_spent_on_is_zero_without_a_log(tmp_path: Path) -> None:
    from datetime import date

    from omniscient.ops import spent_on

    assert spent_on(tmp_path, date(2026, 11, 12)) == 0.0


def test_cycle_log_counts_cycles_per_local_day(tmp_path: Path) -> None:
    from datetime import date

    from omniscient.ops import cycles_on, record_cycle

    record_cycle(tmp_path, datetime(2026, 11, 12, 7, 0).astimezone())
    record_cycle(tmp_path, datetime(2026, 11, 12, 19, 0).astimezone())
    record_cycle(tmp_path, datetime(2026, 11, 13, 7, 0).astimezone())

    assert cycles_on(tmp_path, date(2026, 11, 12)) == 2
    assert cycles_on(tmp_path, date(2026, 11, 13)) == 1
    assert cycles_on(tmp_path, date(2026, 11, 14)) == 0


def test_load_settings_reads_max_cycles_per_day() -> None:
    settings = load_settings(packaged("templates") / "omniscient.toml")

    assert settings.schedule.max_cycles_per_day == 1

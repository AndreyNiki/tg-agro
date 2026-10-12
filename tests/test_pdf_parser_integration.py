from pathlib import Path
import json
import pytest
from schedule_bot.pdf_parser import (
    parse_schedule,
    parse_schedules_for_groups,
    schedule_to_dict,
    schedule_from_dict,
)

FIXTURE_PATH = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")


@pytest.fixture(scope="module")
def schedule():
    return parse_schedule(FIXTURE_PATH, "Б-Мв-101")


def test_monday_and_tuesday_have_no_lessons(schedule):
    assert schedule.days["Понедельник"].lessons == []
    assert schedule.days["Вторник"].lessons == []


def test_wednesday_lessons(schedule):
    lessons = {l.time: (l.upper, l.lower) for l in schedule.days["Среда"].lessons}
    assert set(lessons) == {"13.40-15.10", "15.20-16.50", "17.00-18.30"}
    upper, lower = lessons["15.20-16.50"]
    assert "СПЕЦ. ПЕДАГОГИКА" in upper
    assert "Специальная педагогика" in lower


def test_thursday_has_asymmetric_split_at_10_10(schedule):
    lessons = {l.time: (l.upper, l.lower) for l in schedule.days["Четверг"].lessons}
    upper, lower = lessons["10.10-11.40"]
    assert upper is None
    assert "МАТЕМАТИКА" in lower


def test_saturday_has_merged_cell_at_12_00(schedule):
    lessons = {l.time: (l.upper, l.lower) for l in schedule.days["Суббота"].lessons}
    upper, lower = lessons["12.00-13.30"]
    assert upper is None
    assert "ОСНОВЫ РОССИЙСКОЙ ГОСУДАРСТВЕННОСТИ" in lower


def test_saturday_8_30_slot_is_fully_empty_and_omitted(schedule):
    lessons = {l.time for l in schedule.days["Суббота"].lessons}
    assert "8.30-10.00" not in lessons


def test_parse_schedule_raises_for_unknown_group():
    with pytest.raises(ValueError):
        parse_schedule(FIXTURE_PATH, "NO-SUCH-GROUP")


def test_schedule_round_trips_through_json(schedule):
    payload = json.dumps(schedule_to_dict(schedule))
    restored = schedule_from_dict(json.loads(payload))
    assert restored.table_x0 == schedule.table_x0
    assert restored.table_x1 == schedule.table_x1
    assert restored.days["Среда"].lessons == schedule.days["Среда"].lessons


def test_parse_schedule_includes_header_y_bounds():
    schedule = parse_schedule(FIXTURE_PATH, "Б-Мв-101")
    assert schedule.header_y0 is not None
    assert schedule.header_y1 is not None
    assert schedule.header_y1 > schedule.header_y0


def test_schedule_round_trip_preserves_header_bounds():
    schedule = parse_schedule(FIXTURE_PATH, "Б-Мв-101")
    restored = schedule_from_dict(schedule_to_dict(schedule))
    assert restored.header_y0 == schedule.header_y0
    assert restored.header_y1 == schedule.header_y1
    restored_day = restored.days["Среда"]
    original_day = schedule.days["Среда"]
    assert restored_day.time_col_x0 == original_day.time_col_x0
    assert restored_day.group_col_x1 == original_day.group_col_x1


def test_parse_schedules_for_groups_extracts_raw_rows_exactly_once(monkeypatch):
    import schedule_bot.pdf_parser as pdf_parser_module

    call_count = 0
    real_extract = pdf_parser_module.extract_raw_rows

    def counting_extract(source):
        nonlocal call_count
        call_count += 1
        return real_extract(source)

    monkeypatch.setattr(pdf_parser_module, "extract_raw_rows", counting_extract)

    results = parse_schedules_for_groups(FIXTURE_PATH, ["Б-Мв-101", "Б-Эв-101"])

    assert call_count == 1
    assert results["Б-Мв-101"] is not None
    assert isinstance(results["Б-Мв-101"], pdf_parser_module.Schedule)


def test_parse_schedules_for_groups_returns_none_for_unknown_group():
    results = parse_schedules_for_groups(FIXTURE_PATH, ["Б-Мв-101", "НЕТ-ТАКОЙ-ГРУППЫ"])
    assert results["Б-Мв-101"] is not None
    assert results["НЕТ-ТАКОЙ-ГРУППЫ"] is None

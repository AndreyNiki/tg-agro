from pathlib import Path
import json
import pytest
from schedule_bot.pdf_parser import parse_schedule, schedule_to_dict, schedule_from_dict

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

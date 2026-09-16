from pathlib import Path
import pytest
from schedule_bot.pdf_parser import extract_raw_rows, find_group_column_index

FIXTURE_PATH = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")


def test_extract_raw_rows_reads_local_path():
    rows, table_bbox = extract_raw_rows(FIXTURE_PATH)
    assert len(rows) == 41
    assert table_bbox[2] == pytest.approx(822.5956357142857, abs=0.5)


def test_extract_raw_rows_header_row_has_group_columns():
    rows, _ = extract_raw_rows(FIXTURE_PATH)
    header = rows[1]
    header_texts = [cell.text if cell else None for cell in header.cells]
    assert "Б-Эв-101" in header_texts
    assert "Б-Мв-101" in header_texts


def test_extract_raw_rows_day_marker_rows_match_known_positions():
    rows, _ = extract_raw_rows(FIXTURE_PATH)
    day_marker_indexes = [i for i, row in enumerate(rows) if row.day_marker]
    # Title row (0), header row (1, group names act as a "marker" too because
    # column 0 is non-empty there), the 6 real day-start rows, and the footer
    # row (40) all have a non-empty first column.
    assert day_marker_indexes == [0, 1, 2, 4, 6, 14, 23, 32, 40]


def test_extract_raw_rows_time_label_forward_fill_source_is_none_between_rows():
    rows, _ = extract_raw_rows(FIXTURE_PATH)
    assert rows[2].time_label == "8.30-10.00"
    assert rows[3].time_label == "10.10-11.40"
    # row 11 is a continuation row (no time label of its own)
    assert rows[11].time_label is None


def test_find_group_column_index_locates_target_group():
    rows, _ = extract_raw_rows(FIXTURE_PATH)
    header_idx, col_idx = find_group_column_index(rows, "Б-Мв-101")
    assert header_idx == 1
    assert col_idx == 4


def test_find_group_column_index_raises_for_unknown_group():
    rows, _ = extract_raw_rows(FIXTURE_PATH)
    with pytest.raises(ValueError):
        find_group_column_index(rows, "NO-SUCH-GROUP")

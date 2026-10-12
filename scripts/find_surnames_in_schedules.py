"""Standalone script: find surnames across schedule PDFs linked from given pages.

Usage:
    python3 scripts/find_surnames_in_schedules.py \
        --pages "https://example.com/page1,https://example.com/page2" \
        --surnames "Иванов,Петров" \
        --output report.csv

For each page URL, collects every <a href> link pointing to a .pdf file,
downloads each PDF, and searches every table cell for each surname
(case-insensitive substring match by default). For every match it records
the day label, the time label (second non-empty cell in the row) and the
full text of the matched cell.

Append "^" to a surname for an exact-word match instead of substring —
e.g. "Уколов^" matches "Уколов" but not "Уколова".

The day label is not read literally from the first cell's text: in these
schedule PDFs that column is typeset vertically (one character per text
line, e.g. "л\nнеде\nПо" for "Понедельник"), so a non-empty first cell is
instead treated as "day marker reached" and advances through the known
Mon-Sun order, same convention used by schedule_bot/pdf_parser.py.

The final CSV has one row per surname, one column per day. Each cell
contains every match found for that surname/day, formatted as
"{time} / {cell text}", with a blank line between entries.

Pages or PDFs that failed to download or parse are not silently skipped —
they are listed in a final report printed to stderr.
"""

import argparse
import csv
import io
import re
import sys
from urllib.parse import urljoin

import pdfplumber
import requests

DAY_ORDER = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
TIME_RE = re.compile(r"\d{1,2}[.:]\d{2}\s*-\s*\d{1,2}[.:]\d{2}")


def find_pdf_links(page_url: str, failures: list[tuple[str, str]]) -> list[str]:
    try:
        response = requests.get(page_url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        failures.append((page_url, f"failed to fetch page: {exc}"))
        return []

    links = []
    for href in HREF_RE.findall(response.text):
        if href.lower().endswith(".pdf"):
            links.append(urljoin(page_url, href))
    return links


def download_pdf(pdf_url: str, failures: list[tuple[str, str]]) -> bytes | None:
    try:
        response = requests.get(pdf_url, timeout=60)
        response.raise_for_status()
        return response.content
    except requests.RequestException as exc:
        failures.append((pdf_url, f"failed to download pdf: {exc}"))
        return None


def parse_surname_arg(raw: str) -> tuple[str, re.Pattern]:
    """Return (display_name, compiled matcher). A trailing "^" requests an
    exact-word match instead of substring, e.g. "Уколов^" won't match
    "Уколова"."""
    if raw.endswith("^"):
        display = raw[:-1].strip()
        pattern = re.compile(r"\b" + re.escape(display) + r"\b", re.IGNORECASE)
    else:
        display = raw.strip()
        pattern = re.compile(re.escape(display), re.IGNORECASE)
    return display, pattern


def search_pdf(
    pdf_bytes: bytes,
    pdf_url: str,
    surnames: list[tuple[str, re.Pattern]],
    failures: list[tuple[str, str]],
) -> dict[str, dict[str, list[str]]]:
    # display name -> day -> list of "{time} / {cell text}"
    matches: dict[str, dict[str, list[str]]] = {display: {} for display, _ in surnames}

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                for table in page.find_tables():
                    rows = table.extract()
                    day_iter = iter(DAY_ORDER)
                    current_day: str | None = None
                    current_time: str | None = None
                    for row in rows:
                        time_cell = row[1].strip() if len(row) > 1 and row[1] else None
                        if row and row[0] and time_cell and TIME_RE.search(time_cell):
                            current_day = next(day_iter, current_day)
                        if time_cell:
                            current_time = time_cell

                        for cell_text in row:
                            if not cell_text:
                                continue
                            for display, pattern in surnames:
                                if pattern.search(cell_text):
                                    day = current_day or "?"
                                    time_label = current_time or "?"
                                    entry = f"{time_label} / {cell_text.strip()}"
                                    matches[display].setdefault(day, []).append(entry)
    except Exception as exc:  # pdfplumber can raise various parsing errors
        failures.append((pdf_url, f"failed to parse pdf: {exc}"))

    return matches


def merge_matches(
    total: dict[str, dict[str, list[str]]],
    partial: dict[str, dict[str, list[str]]],
) -> None:
    for surname, days in partial.items():
        for day, entries in days.items():
            total.setdefault(surname, {}).setdefault(day, []).extend(entries)


def day_sort_key(day: str) -> tuple[int, str]:
    if day in DAY_ORDER:
        return (DAY_ORDER.index(day), day)
    return (len(DAY_ORDER), day)


def _entry_time_label(entry: str) -> str:
    return entry.split(" / ", 1)[0]


def _time_sort_key(time_label: str) -> tuple[int, int]:
    match = TIME_RE.search(time_label)
    if not match:
        return (99, 99)
    start = re.split(r"[.:]", match.group().split("-")[0])
    return (int(start[0]), int(start[1]))


def _whitespace_key(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _dedup_key(entry: str, surname: str) -> str:
    """Two entries are duplicates if they share the same time and, from
    the surname's first occurrence onward, the same text (ignoring
    whitespace) — this absorbs differing prefixes like "лек." vs "пр.з."
    before the same "Фамилия И.О. каб." tail."""
    time_label, _, text = entry.partition(" / ")
    idx = text.lower().find(surname.lower())
    tail = text[idx:] if idx != -1 else text
    return _whitespace_key(time_label) + "|" + _whitespace_key(tail)


def sort_and_flag_conflicts(entries: list[str], surname: str) -> list[str]:
    """Sort entries by start time; collapse duplicates (same time, same
    text from the surname onward, ignoring whitespace); mark remaining
    same-time entries with "?" (likely two different people sharing the
    same surname, or a genuine clash)."""
    deduped: dict[str, str] = {}
    for entry in entries:
        deduped.setdefault(_dedup_key(entry, surname), entry)
    ordered = sorted(deduped.values(), key=lambda entry: _time_sort_key(_entry_time_label(entry)))

    counts: dict[str, int] = {}
    for entry in ordered:
        label = _entry_time_label(entry)
        counts[label] = counts.get(label, 0) + 1

    return [
        entry + " ???" if counts[_entry_time_label(entry)] > 1 else entry
        for entry in ordered
    ]


def write_report(output_path: str, surnames: list[str], total: dict[str, dict[str, list[str]]]) -> None:
    all_days = sorted(
        {day for days in total.values() for day in days},
        key=day_sort_key,
    )

    with open(output_path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Фамилия", *all_days])
        for surname in surnames:
            days = total.get(surname, {})
            row = [surname]
            for day in all_days:
                entries = sort_and_flag_conflicts(days.get(day, []), surname)
                row.append("\n\n".join(entries))
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", required=True, help="Comma-separated list of page URLs")
    parser.add_argument("--surnames", required=True, help="Comma-separated list of surnames")
    parser.add_argument("--output", default="report.csv", help="Output CSV path")
    args = parser.parse_args()

    pages = [p.strip() for p in args.pages.split(",") if p.strip()]
    surnames = [parse_surname_arg(s) for s in args.surnames.split(",") if s.strip()]
    surname_display_order = [display for display, _ in surnames]

    failures: list[tuple[str, str]] = []

    pdf_urls: list[str] = []
    seen = set()
    for page_url in pages:
        for pdf_url in find_pdf_links(page_url, failures):
            if pdf_url not in seen:
                seen.add(pdf_url)
                pdf_urls.append(pdf_url)

    total: dict[str, dict[str, list[str]]] = {}
    for pdf_url in pdf_urls:
        pdf_bytes = download_pdf(pdf_url, failures)
        if pdf_bytes is None:
            continue
        partial = search_pdf(pdf_bytes, pdf_url, surnames, failures)
        merge_matches(total, partial)

    write_report(args.output, surname_display_order, total)

    print(f"Report written to {args.output}")
    print(f"PDFs processed: {len(pdf_urls) - len(failures)} / {len(pdf_urls)}")

    if failures:
        print("\n=== Ссылки/PDF, которые не удалось открыть или спарсить ===", file=sys.stderr)
        for url, reason in failures:
            print(f"{url}: {reason}", file=sys.stderr)
    else:
        print("Все ссылки и PDF успешно обработаны.")


if __name__ == "__main__":
    main()

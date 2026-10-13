import pytest

from schedule_bot.group_links import (
    GroupLinkNotFoundError,
    SourcePageUnreachableError,
    expand_group_names,
    list_pdf_links,
    resolve_group_pdf_source,
    resolve_pdf_link,
)

_SAMPLE_HTML = """
<html><body>
<a href="/files/1_kurs_BE_BM.pdf">1 курс БЭ, БМ оз.</a>
<a href="/files/2_kurs_BE.pdf">2 курс БЭ оз.</a>
</body></html>
"""


def test_resolve_pdf_link_matches_by_visible_text_substring():
    link = resolve_pdf_link(_SAMPLE_HTML, "БЭ", "https://example.com/schedules")
    assert link == "https://example.com/files/1_kurs_BE_BM.pdf"


def test_resolve_pdf_link_matches_second_group_sharing_same_link():
    link = resolve_pdf_link(_SAMPLE_HTML, "БМ", "https://example.com/schedules")
    assert link == "https://example.com/files/1_kurs_BE_BM.pdf"


def test_resolve_pdf_link_is_case_insensitive():
    link = resolve_pdf_link(_SAMPLE_HTML, "бэ", "https://example.com/schedules")
    assert link is not None


def test_resolve_pdf_link_returns_none_when_no_match():
    assert resolve_pdf_link(_SAMPLE_HTML, "НЕТ-ТАКОЙ-ГРУППЫ", "https://example.com/schedules") is None


def test_resolve_pdf_link_resolves_relative_href_against_base_url():
    link = resolve_pdf_link(_SAMPLE_HTML, "БЭ", "https://example.com/ucheba/schedules.html")
    assert link.startswith("https://example.com/files/")


def test_resolve_group_pdf_source_raises_when_page_unreachable(monkeypatch):
    import schedule_bot.group_links as group_links_module

    def raise_error(url, timeout):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(group_links_module, "fetch_url", raise_error)

    with pytest.raises(SourcePageUnreachableError):
        resolve_group_pdf_source("https://example.com/schedules", "БЭ")


def test_resolve_group_pdf_source_raises_when_no_matching_link(monkeypatch):
    import schedule_bot.group_links as group_links_module

    class FakeResponse:
        text = _SAMPLE_HTML

        def raise_for_status(self):
            pass

    monkeypatch.setattr(group_links_module, "fetch_url", lambda url, timeout: FakeResponse())

    with pytest.raises(GroupLinkNotFoundError):
        resolve_group_pdf_source("https://example.com/schedules", "НЕТ-ТАКОЙ-ГРУППЫ")


def test_resolve_group_pdf_source_returns_resolved_link_on_success(monkeypatch):
    import schedule_bot.group_links as group_links_module

    class FakeResponse:
        text = _SAMPLE_HTML

        def raise_for_status(self):
            pass

    monkeypatch.setattr(group_links_module, "fetch_url", lambda url, timeout: FakeResponse())

    link = resolve_group_pdf_source("https://example.com/schedules", "БЭ")
    assert link == "https://example.com/files/1_kurs_BE_BM.pdf"


def test_list_pdf_links_returns_every_pdf_link_with_normalized_label():
    html = """
    <a href="/files/a.pdf">  Б-А-301,302,303  </a>
    <a href="/files/b.pdf">Б-Эв-101, Б-Мв-101</a>
    """
    links = list_pdf_links(html, "https://example.com/catalog")
    assert links == [
        ("https://example.com/files/a.pdf", "Б-А-301,302,303"),
        ("https://example.com/files/b.pdf", "Б-Эв-101, Б-Мв-101"),
    ]


def test_list_pdf_links_collapses_internal_whitespace_but_keeps_case():
    html = '<a href="/x.pdf">Б-А-301,\xa0 302</a>'
    links = list_pdf_links(html, "https://example.com/catalog")
    assert links == [("https://example.com/x.pdf", "Б-А-301, 302")]


def test_expand_group_names_fills_prefix_for_trailing_digit_fragments():
    assert expand_group_names("Б-А-301,302,303") == ["Б-А-301", "Б-А-302", "Б-А-303"]


def test_expand_group_names_keeps_full_names_as_is():
    assert expand_group_names("Б-Эв-101, Б-Мв-101") == ["Б-Эв-101", "Б-Мв-101"]


def test_expand_group_names_mixes_prefix_fill_and_full_names():
    assert expand_group_names("Б-А-301,302, Б-Мв-101") == ["Б-А-301", "Б-А-302", "Б-Мв-101"]


def test_expand_group_names_returns_empty_for_leading_number_with_no_prefix():
    assert expand_group_names("301,302") == []


def test_expand_group_names_ignores_surrounding_whitespace_around_commas():
    assert expand_group_names(" Б-А-301 , 302 ") == ["Б-А-301", "Б-А-302"]


from schedule_bot.group_links import list_form_of_study_links

_FORM_NAV_HTML = """
<html><body>
<a href="novosti">Новости</a>
<a href="ochnaya-forma-obucheniya">Очная форма обучения</a>
<a href="ochno-zaochnaya-forma-obucheniya">Очно-заочная форма обучения</a>
<a href="zaochnaya-forma-obucheniya">Заочная форма обучения</a>
</body></html>
"""


def test_list_form_of_study_links_finds_all_three_and_ignores_other_links():
    links = list_form_of_study_links(_FORM_NAV_HTML, "https://example.com/institut")
    assert links == [
        ("https://example.com/ochnaya-forma-obucheniya", "Очная форма обучения"),
        ("https://example.com/ochno-zaochnaya-forma-obucheniya", "Очно-заочная форма обучения"),
        ("https://example.com/zaochnaya-forma-obucheniya", "Заочная форма обучения"),
    ]


def test_list_form_of_study_links_returns_empty_when_none_present():
    html = '<a href="novosti">Новости</a>'
    assert list_form_of_study_links(html, "https://example.com/institut") == []


def test_list_form_of_study_links_is_case_insensitive_on_label():
    html = '<a href="x">ОЧНАЯ ФОРМА ОБУЧЕНИЯ</a>'
    assert list_form_of_study_links(html, "https://example.com/institut") == [
        ("https://example.com/x", "ОЧНАЯ ФОРМА ОБУЧЕНИЯ")
    ]


def test_list_form_of_study_links_resolves_relative_href_against_base_url():
    html = '<a href="/abs/path">Заочная форма обучения</a>'
    links = list_form_of_study_links(html, "https://example.com/deep/institut")
    assert links == [("https://example.com/abs/path", "Заочная форма обучения")]


def test_list_form_of_study_links_handles_subset_of_the_three_forms():
    html = '<a href="x">Очная форма обучения</a>'
    assert list_form_of_study_links(html, "https://example.com/institut") == [
        ("https://example.com/x", "Очная форма обучения")
    ]


def test_list_form_of_study_links_ignores_pdf_hrefs_even_with_matching_label():
    """A .pdf href whose label happens to mention "форма обучения"
    (e.g. a PDF file itself named with the phrase) is not a navigation
    link to a form-of-study subpage — matching it would make
    refresh_institute_catalog fetch a PDF and regex it as HTML, wasting
    a request for zero benefit."""
    html = '<a href="/zaochnaya-1kurs.pdf">Заочная форма обучения, 1 курс</a>'
    assert list_form_of_study_links(html, "https://example.com/institut") == []

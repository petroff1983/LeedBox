import pytest

from leedbox.domain.brief import BRIEF_FIELDS, missing_fields, validate_answer


def test_brief_requires_real_answers_and_never_enriches_facts():
    brief = {"niche": "  Яндекс Директ  ", "product": "", "audience": None}
    assert missing_fields(brief, ("niche", "product", "audience")) == ("product", "audience")
    assert validate_answer("evidence", " Нет отзывов ") == "Нет отзывов"
    assert len(BRIEF_FIELDS) == 10


@pytest.mark.parametrize(
    "field,value",
    [
        ("unknown", "x"),
        ("niche", " "),
        ("niche", "x" * 101),
        ("evidence", "Доход\x00"),
        ("links", "javascript:alert(1)"),
        ("links", "https://example.org\n" * 11),
    ],
)
def test_brief_rejects_unsafe_or_invalid_answers(field, value):
    with pytest.raises(ValueError):
        validate_answer(field, value)


def test_brief_links_are_http_only_without_fetching():
    assert validate_answer("links", "https://example.org\n http://example.com ") == (
        "https://example.org\nhttp://example.com"
    )
    with pytest.raises(ValueError):
        missing_fields({}, ("not-a-field",))

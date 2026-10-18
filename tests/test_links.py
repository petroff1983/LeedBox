import csv
import io
from urllib.parse import parse_qs, urlsplit

import pytest

from leedbox.application.links import utm_batch
from leedbox.domain.links import ReplacementRequired, csv_export, deep_link, utm_url

TAGS = {"source": "yandex", "medium": "cpc", "campaign": "Моя кампания", "term": "{keyword}"}


def test_utm_preserves_query_anchor_and_encodes_unicode():
    value = utm_url("https://пример.рф/страница?a=1&a=2#мой-якорь", TAGS)
    parsed = urlsplit(value)
    assert parsed.netloc == "xn--e1afmkfd.xn--p1ai"
    assert "%D1%81" in parsed.path and "%D0%BC" in parsed.fragment
    query = parse_qs(parsed.query)
    assert query["a"] == ["1", "2"]
    assert query["utm_campaign"] == ["Моя_кампания"]
    assert query["utm_term"] == ["{keyword}"]
    assert "{keyword}" in value
    assert (
        urlsplit(utm_url("https://пример.рф:8080/a", TAGS)).netloc == "xn--e1afmkfd.xn--p1ai:8080"
    )
    assert urlsplit(utm_url("http://[2001:db8::1]:8000/a", TAGS)).netloc == "[2001:db8::1]:8000"


def test_utm_requires_explicit_replacement_and_removes_duplicates():
    url = "https://example.com/?utm_source=old&UTM_SOURCE=again&utm_custom=x&x=1#top"
    with pytest.raises(ReplacementRequired):
        utm_url(url, TAGS)
    value = utm_url(url, TAGS, replace=True)
    query = parse_qs(urlsplit(value).query)
    assert query["utm_source"] == ["yandex"]
    assert "UTM_SOURCE" not in query and "utm_custom" not in query
    assert query["x"] == ["1"] and value.endswith("#top")


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "ftp://example.com",
        "https://user:pw@example.com",
        "https://@example.com",
        "https://example.com/ bad",
        "https://example.com\\evil",
    ],
)
def test_utm_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        utm_url(url, TAGS)


def test_utm_validates_required_fields_lengths_and_batches():
    with pytest.raises(ValueError):
        utm_url("https://example.com", {"source": "vk"})
    with pytest.raises(ValueError):
        utm_url("https://example.com", {**TAGS, "campaign": "я" * 201})
    with pytest.raises(ValueError):
        utm_batch("\n".join(["https://example.com"] * 21), TAGS)
    assert len(utm_batch("https://example.com/a\nhttps://example.com/b", TAGS)) == 2


def test_csv_formula_injection_bom_quotes_and_newlines():
    raw = csv_export([["=1+1", " +1", "-1", "@SUM(A1)", "\tformula", "обычный,текст", "строка\n2"]])
    assert raw.startswith(b"\xef\xbb\xbf")
    values = next(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert all(value.startswith("'") for value in values[:5])
    assert values[5:] == ["обычный,текст", "строка\n2"]


def test_deep_link_rejects_payload_overflow_and_invalid_characters():
    assert deep_link("leedbox_test", "ex_123456abcdef").endswith("start=ex_123456abcdef")
    assert deep_link("leedbox_test", "a" * 64)
    for payload in ("a" * 65, "метки", "a b", "a&b", ""):
        with pytest.raises(ValueError):
            deep_link("leedbox_test", payload)

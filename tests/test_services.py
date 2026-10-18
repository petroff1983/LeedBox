from dataclasses import replace

import pytest

from leedbox.domain.services import ServiceInput, owner_contact_url, request_contact, service_text


@pytest.mark.parametrize("value", ["@test_user", "test@example.com", "+7 (900) 123-45-67"])
def test_request_contacts(value):
    assert request_contact(f" {value} ") == value


@pytest.mark.parametrize("value", ["", "http://evil.example", "@x", "text", "123", "x\x00@y.ru"])
def test_invalid_request_contacts(value):
    with pytest.raises(ValueError):
        request_contact(value)


def test_service_data_is_factual_and_bounded():
    values = ServiceInput(
        "Тест",
        "Эксперты",
        "Состав\nработ",
        "Согласовать",
        "По договорённости",
        "Тестовый исполнитель",
        "Тестовые условия",
    )
    assert values.validated()["description"] == "Состав\nработ"
    assert not values.validated()["enabled"]
    with pytest.raises(ValueError):
        replace(values, title=" ").validated()
    with pytest.raises(ValueError):
        replace(values, sort_order=True).validated()
    assert service_text("", 1000, False) == ""
    with pytest.raises(ValueError):
        service_text("\x00", 1000)


def test_owner_contact_cannot_be_an_arbitrary_url():
    assert owner_contact_url("") is None
    assert owner_contact_url("@test_owner") == "https://t.me/test_owner"
    with pytest.raises(ValueError):
        owner_contact_url("https://evil.example")

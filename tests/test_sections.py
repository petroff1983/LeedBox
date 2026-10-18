from datetime import UTC, datetime

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from leedbox.application.sections import SECTIONS, VISIBILITY_DEFAULTS
from leedbox.infrastructure.config import Settings
from leedbox.telegram.handlers import menu
from leedbox.telegram.material_ui import HOME
from leedbox.telegram.sections import event_section


@pytest.mark.parametrize("hidden", list(SECTIONS))
def test_each_menu_section_can_be_hidden_and_returned(hidden):
    config = Settings.model_construct()
    shown = set(SECTIONS) - {hidden}
    markup = menu(config, 1, visible=shown)
    codes = {button.callback_data for row in markup.inline_keyboard for button in row}
    assert codes == {"section:" + code for code in shown if code != "youtube"}
    assert all(len(row) <= 2 for row in markup.inline_keyboard)
    restored = menu(config, 1, visible=set(SECTIONS))
    if hidden != "youtube":
        assert "section:" + hidden in str(restored)
    assert "section:youtube" not in str(restored)
    assert len(VISIBILITY_DEFAULTS) == len(SECTIONS)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("section:links", "links"),
        ("ut:new:vk", "links"),
        ("sl:new:short", "links"),
        ("ln:history:0", "links"),
        ("ct:resume", "texts"),
        ("ob:open:1", "texts"),
        ("cs:from:1", "texts"),
        ("m:library", "materials"),
        ("aud:new:1", "materials"),
        ("cw:1:back", "calc"),
        ("section:audience", "audience"),
        ("yt:limit:5", "youtube"),
        ("project:contact", "profile"),
        ("store:products", "services"),
        ("store:contact", "consultations"),
        ("sn:list:0", "services"),
        ("get:token", None),
        ("consent:token", None),
        ("notify:ads:off", None),
        ("purchase:order:1", None),
        ("access:renew", None),
        ("profile:delete", None),
        ("delete:token", None),
        ("sw:confirm:1", None),
        ("cw:1:cancel", None),
        ("menu", None),
    ],
)
def test_callback_visibility_classification_and_safety_exceptions(code, expected):
    event = CallbackQuery(
        id="fixture",
        chat_instance="fixture",
        data=code,
        from_user=User(id=1, is_bot=False, first_name="Expert"),
    )
    assert event_section(event, {"active_tool": "texts", "paused": False}) == expected


@pytest.mark.parametrize(
    "text,active,paused,expected",
    [
        ("/utm@fixture_bot", None, True, "links"),
        ("/calc", None, True, "calc"),
        ("/utp", None, True, "texts"),
        ("/youtube", None, True, "youtube"),
        ("/menu", "texts", False, None),
        ("/start lm_token", "material", False, None),
        (HOME, "material", False, None),
        ("answer", "utm", False, "links"),
        ("answer", "utm", True, None),
    ],
)
def test_commands_inputs_and_home_remain_correctly_scoped(text, active, paused, expected):
    event = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=1, type="private"), text=text
    )
    assert event_section(event, {"active_tool": active, "paused": paused}) == expected

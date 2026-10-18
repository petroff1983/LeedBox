# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.
import asyncio
import json
import re
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Document, Message, Update, User
from sqlalchemy.orm import Session
from test_dzen_agent import ai_fixture  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import agents, ai_tokens
from leedbox.application.cabinet_access import issue_ticket
from leedbox.application.settings import set_setting
from leedbox.domain.image_prompts import SUFFIX, headings, validate_result
from leedbox.telegram.agents import build_agent_router
from leedbox.telegram.storage import PostgresStorage
from leedbox.web.app import create_app


def article(count=5):
    return "\n\n".join(
        f"## Раздел {index}\n\nПодробный текст статьи о домашних растениях и уходе за ними. "
        "Рассказываем о доступных решениях и практических советах для начинающих."
        for index in range(1, count + 1)
    )


def result(count=5):
    objects = ["a window", "a table", "a shelf", "a chair", "a curtain"]
    return {
        "images": [
            {
                "heading": f"Раздел {index}",
                "prompt": f"A ceramic plant pot beside {objects[index - 1]}, "
                "fresh green leaves reflecting soft morning light in a peaceful home, "
                "warm colors, realistic photo, " + SUFFIX,
            }
            for index in range(1, count + 1)
        ]
    }


def payload(value):
    if isinstance(value, dict) and isinstance(value.get("images"), list):
        value = {
            "images": [
                {"section_id": f"s{i}", "prompt": item["prompt"]}
                if isinstance(item, dict) and "heading" in item
                else item
                for i, item in enumerate(value["images"], 1)
            ]
        }
    return {
        "usage": dict(prompt_tokens=700, completion_tokens=500, total_tokens=1200),
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}],
    }


@pytest.mark.parametrize("count", [1, 3, 5, 7])
def test_image_headings_and_result(count):
    selected = headings(article(count))
    assert selected == [f"Раздел {i}" for i in range(1, min(count, 5) + 1)]
    assert validate_result(result(min(count, 5)), selected) == result(min(count, 5))


def test_image_plain_bold_numbered_and_lists():
    assert headings("Название статьи\n\n" + "Связный текст с точкой. " * 6)[0] == "Название статьи"
    assert headings("**Первый раздел**\n\n" + "Связный текст. " * 8)[0] == "Первый раздел"
    assert headings("1. Первый раздел\n\n" + "Связный текст. " * 8)[0] == "1. Первый раздел"
    assert headings("# Тема\n\n- Пункт списка\n- Второй пункт\n\n" + "Абзац. " * 20) == ["Тема"]
    with pytest.raises(ValueError, match="заголовков"):
        headings("Сплошной текст без заголовков. " * 10)
    with pytest.raises(ValueError):
        headings("Привет")
    with pytest.raises(ValueError):
        headings("# Тема\n" + "x" * 20000)


def test_image_telegram_single_newlines_and_question_headings():
    titles = [
        "Секрет, о котором молчат на упаковке",
        "Тогда зачем вообще люди её прокаливают дома?",
        "А в чём тогда подвох",
        "Когда точно не нужно жарить",
        "Что решила для себя я",
    ]
    paragraph = "Подробный абзац о приготовлении гречки и личном опыте автора. " * 4
    # Same structure as the reported Telegram paste: no ## and no blank lines.
    text = paragraph + "\n" + "\n".join(title + "\n" + paragraph for title in titles)
    assert headings(text) == titles
    assert headings(text.replace("\n", "\n\n")) == titles
    # Body paragraphs alone must not become headings.
    with pytest.raises(ValueError, match="заголовков"):
        headings("\n".join([paragraph, "Небольшой абзац с точкой.", paragraph]))


@pytest.mark.parametrize("case", ["count", "heading", "language", "short", "duplicate"])
def test_image_result_rejects_invalid(case):
    value = result()
    if case == "count":
        value["images"].pop()
    elif case == "heading":
        value["images"][0]["heading"] = "Придуманный заголовок"
    elif case == "language":
        value["images"][0]["prompt"] = "Русский текст " + value["images"][0]["prompt"]
    elif case == "short":
        value["images"][0]["prompt"] = "A plant " + SUFFIX
    else:
        value["images"][1]["prompt"] = value["images"][0]["prompt"]
    with pytest.raises(ValueError):
        validate_result(value, headings(article()))


def test_image_short_prompts_are_bounded_and_parameters_added_by_code():
    scenes = [
        "Photorealistic view of a vivid deep blue gas giant very close to its bright star, "
        "violent horizontal storms with glittering glass-like particles streaking through "
        "the upper atmosphere, dramatic cinematic lighting, ultra-detailed, 8k, no text, "
        "no letters, no watermark.",
        "Photorealistic rocky super-Earth with a glowing ocean of molten lava on its "
        "star-facing side, cracked dark crust, huge white-hot star filling much of the "
        "sky, heat haze, volcanic plumes, cinematic space photography, ultra-detailed, "
        "no text, no letters, no watermark.",
        "Photorealistic matte black gas giant that reflects almost no light, only a faint "
        "dull red glow along its edges, tiny bright star in the distance, dense starfield, "
        "deep space atmosphere, moody and mysterious, high detail, no text, no letters, "
        "no watermark.",
    ]
    value = {"images": [{"heading": str(i), "prompt": scene} for i, scene in enumerate(scenes)]}
    parsed = validate_result(value, ["0", "1", "2"])
    for row in parsed["images"]:
        assert len(row["prompt"].split()) <= 40
        assert row["prompt"].endswith(SUFFIX)
        assert row["prompt"].count("no text") == 1
    # A short but useful scene is not rejected for failing an arbitrary minimum.
    tiny = validate_result(
        {"images": [{"heading": "Scene", "prompt": "A cat sleeping peacefully"}]}, ["Scene"]
    )
    assert tiny["images"][0]["prompt"] == "A cat sleeping peacefully, " + SUFFIX
    assert value["images"][0]["prompt"] == scenes[0]  # Input is immutable.


def test_image_section_ids_preserve_original_headings_and_order():
    expected = ["Тогда зачем вообще люди её прокаливают дома?", "Что решила для себя я"]
    prompts = [row["prompt"] for row in result(2)["images"]]
    wire = {
        "images": [
            {"section_id": "s2", "prompt": prompts[1]},
            {"section_id": "s1", "prompt": prompts[0]},
        ]
    }
    parsed = validate_result(wire, expected)
    assert [row["heading"] for row in parsed["images"]] == expected
    assert [row["prompt"] for row in parsed["images"]] == prompts
    # A redundant model-authored heading is never used for display or matching.
    extra = {"images": [{**item, "heading": "Изменённое название"} for item in wire["images"]]}
    assert validate_result(extra, expected) == parsed
    for invalid in [
        [{"section_id": "s1", "prompt": prompts[0]}] * 2,
        [{"section_id": "s3", "prompt": prompts[0]}, wire["images"][0]],
        [{"section_id": [], "prompt": prompts[0]}, wire["images"][0]],
        [{"heading": expected[0], "prompt": prompts[0]}, wire["images"][0]],
    ]:
        with pytest.raises(ValueError, match="идентификаторы"):
            validate_result({"images": invalid}, expected)


@pytest.mark.integration
def test_image_generation_quota_idempotency_and_independent_switch(ai_fixture):
    engine, config, actors, owners, bot = ai_fixture
    with Session(engine) as db, db.begin():
        set_setting(db, config, actors[0], "ai_dzen_enabled", False)
        offer = agents.quote_images(db, article())
        before = ai_tokens.available(db, owners[1], bot, True)
    with patch(
        "leedbox.application.agents.deepseek_generate", return_value=payload(result())
    ) as api:
        number = agents.generate(engine, config, owners[1], bot, "image-valid", offer)
        assert agents.generate(engine, config, owners[1], bot, "image-valid", offer) == number
        api.assert_called_once()
    with Session(engine) as db, db.begin():
        assert ai_tokens.available(db, owners[1], bot, True) == before - 1200
        assert agents.operation(db, owners[1], bot, True, number).result == result()
        set_setting(db, config, actors[0], "ai_images_enabled", False)
    try:
        with (
            patch("leedbox.application.agents.deepseek_generate") as api,
            pytest.raises(ValueError, match="выключен"),
        ):
            agents.generate(engine, config, owners[1], bot, "image-disabled", offer)
        api.assert_not_called()
    finally:
        with Session(engine) as db, db.begin():
            set_setting(db, config, actors[0], "ai_images_enabled", True)


@pytest.mark.integration
def test_image_invalid_refund_and_prompt_edit_invalidates_quote(ai_fixture):
    engine, config, actors, owners, bot = ai_fixture
    with Session(engine) as db:
        offer = agents.quote_images(db, article())
        before = ai_tokens.available(db, owners[1], bot, True)
    with (
        patch("leedbox.application.agents.deepseek_generate", return_value=payload({"images": []})),
        pytest.raises(ValueError, match="Количество промптов"),
    ):
        agents.generate(engine, config, owners[1], bot, "image-invalid", offer)
    with Session(engine) as db:
        assert ai_tokens.available(db, owners[1], bot, True) == before
    changed = {**offer, "inputs": {**offer["inputs"], "headings": ["Подмена"]}}
    with (
        patch("leedbox.application.agents.deepseek_generate") as api,
        pytest.raises(ValueError, match="изменились"),
    ):
        agents.generate(engine, config, owners[1], bot, "image-tampered", changed)
    api.assert_not_called()


@pytest.mark.integration
def test_image_error_is_visible_even_when_callback_expired(ai_fixture):
    engine, config, actors, owners, _ = ai_fixture
    sent = []

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        with Session(engine) as db, db.begin():
            ai_tokens.grant(db, owners[1], bot.id, True, "image-error-fixture", 50000)
            offer = agents.quote_images(db, article())
            agents.payment_preview(db, config, owners[1], bot.id, offer)
            initial_balance = ai_tokens.available(db, owners[1], bot.id, True)
        dp = Dispatcher(storage=PostgresStorage(engine))
        dp.include_router(build_agent_router(engine, config))
        actor = User(id=actors[1], is_bot=False, first_name="Fixture")
        chat = Chat(id=actor.id, type="private")
        context = dp.fsm.get_context(bot=bot, chat_id=actor.id, user_id=actor.id)
        original = dict(phase="confirm", token="error-fixture", offer=offer)
        await context.set_data(
            dict(mode="expert", active_tool="agents", paused=False, image_draft=original)
        )

        async def fake_request(bot, method, timeout=None):
            if isinstance(method, AnswerCallbackQuery):
                raise TelegramBadRequest(method=method, message="query is too old")
            if isinstance(method, SendMessage):
                sent.append(method)
            return True

        async def click(code):
            await dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    callback_query=CallbackQuery(
                        id="expired",
                        from_user=actor,
                        chat_instance="1",
                        data=code,
                        message=Message(
                            message_id=1,
                            date=datetime.now(UTC),
                            chat=chat,
                            from_user=actor,
                            text="test",
                        ),
                    ),
                ),
            )

        with (
            patch.object(bot.session, "make_request", side_effect=fake_request),
            patch(
                "leedbox.application.agents.deepseek_generate",
                side_effect=[payload({"images": []}), payload(result())],
            ) as api,
        ):
            await click("ip:generate:error-fixture")
            api.assert_called_once()
            assert any("⏳ Создаю" in method.text for method in sent)
            assert "Количество промптов" in sent[-1].text
            assert "Пользовательские токены не списаны" in sent[-1].text
            assert sent[-1].text.startswith("⚠️ ")
            draft = (await context.get_data())["image_draft"]
            assert draft["phase"] == "confirm" and draft["token"] != original["token"]
            with Session(engine) as db:
                row = ai_tokens.previous(db, owners[1], bot.id, True, "images:error-fixture")
                assert row.result["failure"] == "image_count"
                assert ai_tokens.available(db, owners[1], bot.id, True) == initial_balance
            await click("ip:resume")
            assert api.call_count == 1
            await click("ip:generate:" + draft["token"])
            assert api.call_count == 2
            assert (await context.get_data())["image_draft"]["phase"] == "ready"
            assert "Списано: 1 кредитов" in sent[-1].text
            assert "токен" not in sent[-1].text.lower()
            await context.clear()
        await bot.session.close()
        await dp.storage.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_image_router_text_confirmation_history_no_second_paid_call(ai_fixture):
    engine, config, actors, owners, _ = ai_fixture
    bot_id = int(config.bot_token.get_secret_value().split(":")[0])
    with Session(engine) as db, db.begin():
        ai_tokens.grant(db, owners[1], bot_id, True, "image-router-fixture", 50000)
    sent = []

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        dp = Dispatcher(storage=PostgresStorage(engine))
        dp.include_router(build_agent_router(engine, config))
        actor = User(id=actors[1], is_bot=False, first_name="Fixture")
        chat = Chat(id=actor.id, type="private")
        context = dp.fsm.get_context(bot=bot, chat_id=actor.id, user_id=actor.id)
        await context.set_data(dict(mode="expert", active_tool="agents", paused=False))
        update = 0

        def message(text):
            return Message(
                message_id=1, date=datetime.now(UTC), chat=chat, from_user=actor, text=text
            )

        async def click(code):
            nonlocal update
            update += 1
            await dp.feed_update(
                bot,
                Update(
                    update_id=update,
                    callback_query=CallbackQuery(
                        id=str(update),
                        from_user=actor,
                        chat_instance="1",
                        data=code,
                        message=message("test"),
                    ),
                ),
            )

        async def fake_request(bot, method, timeout=None):
            if isinstance(method, SendMessage):
                sent.append(method)
            return True

        with (
            patch.object(bot.session, "make_request", side_effect=fake_request),
            patch(
                "leedbox.application.agents.deepseek_generate", return_value=payload(result())
            ) as api,
        ):
            await click("section:agents")
            assert any(
                b.callback_data == "ip:begin"
                for row in sent[-1].reply_markup.inline_keyboard
                for b in row
            )
            await click("ip:begin")
            await dp.feed_update(bot, Update(update_id=100, message=message(article())))
            draft = (await context.get_data())["image_draft"]
            assert draft["phase"] == "article"
            api.assert_not_called()
            await click(f"ip:finish:{draft['token']}")
            draft = (await context.get_data())["image_draft"]
            assert draft["phase"] == "confirm"
            assert "токен" not in sent[-1].text.lower()
            api.assert_not_called()
            await click("section:agents")
            await click("ip:resume")
            await click(f"ip:generate:{draft['token']}")
            api.assert_called_once()
            number = (await context.get_data())["image_draft"]["operation_id"]
            assert sum("🖼 Разместите картинку:" in s.text for s in sent) == 5
            assert sent[-1].text == "Готово."
            assert "токен" not in sent[-1].text.lower()
            await click(f"ait:result:{number}")
            assert "Новых списаний нет" in sent[-1].text
            api.assert_called_once()
            await context.clear()
        await bot.session.close()
        await dp.storage.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_image_multipart_collects_all_sections_and_survives_return(ai_fixture):
    engine, config, actors, owners, _ = ai_fixture
    sent = []

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        dp = Dispatcher(storage=PostgresStorage(engine))
        dp.include_router(build_agent_router(engine, config))
        actor = User(id=actors[1], is_bot=False, first_name="Fixture")
        chat = Chat(id=actor.id, type="private")
        context = dp.fsm.get_context(bot=bot, chat_id=actor.id, user_id=actor.id)
        await context.set_data(dict(mode="expert", active_tool="agents", paused=False))

        def message(text, number):
            return Message(
                message_id=number, date=datetime.now(UTC), chat=chat, from_user=actor, text=text
            )

        async def click(code):
            await dp.feed_update(
                bot,
                Update(
                    update_id=200,
                    callback_query=CallbackQuery(
                        id="fixture",
                        from_user=actor,
                        chat_instance="1",
                        data=code,
                        message=message("test", 200),
                    ),
                ),
            )

        async def send(text, number):
            await dp.feed_update(bot, Update(update_id=number, message=message(text, number)))

        async def fake_request(bot, method, timeout=None):
            if isinstance(method, SendMessage):
                sent.append(method)
            return True

        with (
            patch.object(bot.session, "make_request", side_effect=fake_request),
            patch("leedbox.application.agents.deepseek_generate") as api,
        ):
            await click("ip:begin")
            token = (await context.get_data())["image_draft"]["token"]
            await click(f"ip:finish:{token}")  # Empty finish does not leave input mode.
            assert await context.get_state() == "agent_image_article"
            chunks = [f"## Раздел {i}\n\n" + "Подробный текст статьи. " * 60 for i in range(1, 6)]
            await send(chunks[0], 101)
            await send(chunks[0], 101)  # Duplicate delivery is not a second part.
            assert len((await context.get_data())["image_draft"]["parts"]) == 1
            await click("section:agents")
            assert any(
                b.callback_data == "ip:resume"
                for row in sent[-1].reply_markup.inline_keyboard
                for b in row
            )
            # A fresh storage instance can read the persisted upload.
            stored = await PostgresStorage(engine).get_data(context.key)
            assert stored["image_draft"]["parts"][0]["text"] == chunks[0]
            await click("ip:resume")
            for i in [4, 2, 3, 1]:  # Order follows Telegram message IDs, not arrival.
                await send(chunks[i], 101 + i)
            await send("x" * 20000, 110)
            assert "не добавлена" in sent[-1].text
            assert len((await context.get_data())["image_draft"]["parts"]) == 5
            assert await context.get_state() == "agent_image_article"
            api.assert_not_called()
            await click(f"ip:finish:{token}")
            draft = (await context.get_data())["image_draft"]
            assert draft["phase"] == "confirm"
            assert draft["offer"]["inputs"]["headings"] == [f"Раздел {i}" for i in range(1, 6)]
            assert draft["offer"]["inputs"]["article"] == "\n\n".join(chunks).strip()
            # Repeated finish cannot reparse or invoke a provider.
            await click(f"ip:finish:{token}")
            assert (await context.get_data())["image_draft"] == draft
            await click(f"ip:edit:{draft['token']}")
            assert await context.get_state() == "agent_image_article"
            restored = (await context.get_data())["image_draft"]
            assert len(restored["parts"]) == 5
            # The previous confirmation cannot charge after returning to edit.
            await click(f"ip:generate:{draft['token']}")
            assert (await context.get_data())["image_draft"] == restored
            await send("Дополнительный абзац статьи.", 115)
            await click(f"ip:finish:{restored['token']}")
            changed = (await context.get_data())["image_draft"]
            assert changed["offer"]["inputs"]["article"].endswith("Дополнительный абзац статьи.")
            assert len(changed["parts"]) == 6
            assert changed["token"] != draft["token"]
            await click("ip:begin")
            await send("Текст без заголовков. " * 10, 120)
            latest = (await context.get_data())["image_draft"]
            await click(f"ip:finish:{latest['token']}")
            assert await context.get_state() == "agent_image_article"
            assert (await context.get_data())["image_draft"]["parts"] == latest["parts"]
            await click("ip:begin")
            assert (await context.get_data())["image_draft"]["parts"] == []
            api.assert_not_called()
            await context.clear()
        await bot.session.close()
        await dp.storage.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_image_web_copy_download_and_owner_scope(ai_fixture):
    engine, config, actors, owners, _ = ai_fixture
    bot = int(config.bot_token.get_secret_value().split(":")[0])
    with Session(engine) as db, db.begin():
        ai_tokens.grant(db, owners[1], bot, True, "image-web-fixture", 50000)
        offer = agents.quote_images(db, article())
    with patch("leedbox.application.agents.deepseek_generate", return_value=payload(result())):
        number = agents.generate(engine, config, owners[1], bot, "image-web", offer)
    with Session(engine) as db, db.begin():
        ticket = urlsplit(issue_ticket(db, config, actors[1])).fragment.removeprefix("token=")
        pending = ai_tokens.reserve(
            db, owners[1], bot, True, "image-pending", offer["maximum"], offer
        )
        pending.status = "uncertain"
        pending_number = pending.id
    client = create_app(config, engine).test_client()
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', client.get("/cabinet/login").text)[1]
    client.post("/cabinet/login", data=dict(csrf=csrf, token=ticket, next="agents"))
    with patch("leedbox.application.agents.deepseek_generate") as api:
        page = client.get(f"/cabinet/agents/{number}")
        assert page.status_code == 200 and "Промпты для картинок" in page.text
        assert page.text.count("data-copy-target") == 5
        assert "--ar 4:3" in client.get(f"/cabinet/agents/{number}/prompt").text
        assert "Промпты для картинок" in client.get("/cabinet/agents").text
        empty = client.get(f"/cabinet/agents/{pending_number}").text
        assert "Промпты для картинок ещё не готовы" in empty
        assert "Готовых заголовков пока нет" not in empty
        api.assert_not_called()
    with Session(engine) as db, db.begin():
        other_ticket = urlsplit(issue_ticket(db, config, actors[2])).fragment.removeprefix("token=")
    other = create_app(config, engine).test_client()
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', other.get("/cabinet/login").text)[1]
    other.post("/cabinet/login", data=dict(csrf=csrf, token=other_ticket))
    assert other.get(f"/cabinet/agents/{number}").status_code == 404
    assert other.get(f"/cabinet/agents/{number}/prompt").status_code == 404


@pytest.mark.integration
def test_image_file_and_insufficient_tokens_never_call_provider(ai_fixture):
    engine, config, actors, owners, _ = ai_fixture
    sent = []

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        dp = Dispatcher(storage=PostgresStorage(engine))
        dp.include_router(build_agent_router(engine, config))
        actor = User(id=actors[2], is_bot=False, first_name="Fixture")
        chat = Chat(id=actor.id, type="private")
        context = dp.fsm.get_context(bot=bot, chat_id=actor.id, user_id=actor.id)
        await context.set_data(dict(mode="expert", active_tool="agents", paused=False))
        await context.set_state("agent_image_article")

        async def fake_request(bot, method, timeout=None):
            if isinstance(method, SendMessage):
                sent.append(method)
            return True

        async def download(file, destination=None, **kwargs):
            destination.write(article().encode("utf-8-sig"))
            return destination

        doc = Document(
            file_id="synthetic", file_unique_id="fixture", file_name="article.md", file_size=2000
        )
        message = Message(
            message_id=1, date=datetime.now(UTC), chat=chat, from_user=actor, document=doc
        )
        with (
            patch.object(bot.session, "make_request", side_effect=fake_request),
            patch.object(bot, "download", side_effect=download),
            patch("leedbox.application.agents.deepseek_generate") as api,
        ):
            await dp.feed_update(bot, Update(update_id=1, message=message))
            draft = (await context.get_data())["image_draft"]
            assert draft["phase"] == "confirm"
            assert len(draft["offer"]["inputs"]["headings"]) == 5
            assert "Не хватает токенов" in sent[-1].text
            buttons = [b for row in sent[-1].reply_markup.inline_keyboard for b in row]
            assert any(b.callback_data == "ait:packages" for b in buttons)
            assert not any((b.callback_data or "").startswith("ip:generate:") for b in buttons)
            forged = CallbackQuery(
                id="fake",
                from_user=actor,
                chat_instance="1",
                data=f"ip:generate:{draft['token']}",
                message=message,
            )
            await dp.feed_update(bot, Update(update_id=2, callback_query=forged))
            api.assert_not_called()
            await context.clear()
        await bot.session.close()
        await dp.storage.close()

    asyncio.run(scenario())

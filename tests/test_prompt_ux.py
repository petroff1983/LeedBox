import asyncio
from datetime import UTC, datetime
from html import unescape
from unittest.mock import patch

import pytest
import test_content_postgres
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendDocument, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application.settings import set_setting
from leedbox.domain import ai_prompts
from leedbox.infrastructure.models import Objection, Project, Setting, ToolResult
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage

content_fixture = test_content_postgres.content_fixture


@pytest.mark.integration
def test_prompts_questionnaire_recovery_history_export_and_ownership(content_fixture):
    engine, config, actors, owners, *_ = content_fixture
    with Session(engine) as db:
        row = db.get(Setting, "prompt_template_offer")
        previous_override = row.value if row else None

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        bot_user = User(id=bot.id, first_name="Synthetic", is_bot=True)
        storage = PostgresStorage(engine)
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(build_router(engine, config))
        key = StorageKey(bot.id, actors[1], actors[1])
        calls, sequence, screen = [], 0, 1000

        async def api(_bot, method, timeout=None):
            nonlocal screen
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, AnswerCallbackQuery):
                return True
            if isinstance(method, SendMessage):
                screen += 1
            return Message(
                message_id=getattr(method, "message_id", None) or screen,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=bot_user,
            )

        async def send(value, *, callback=False, actor=actors[1], message_id=None, query_id=None):
            nonlocal sequence
            sequence += 1
            user = User(id=actor, first_name="Synthetic", is_bot=False)
            message = Message(
                message_id=message_id or (screen if callback else 5000 + sequence),
                date=datetime.now(UTC),
                chat=Chat(id=actor, type="private"),
                from_user=bot_user if callback else user,
                text=None if callback else value,
            )
            update = (
                Update(
                    update_id=sequence,
                    callback_query=CallbackQuery(
                        id=query_id or str(sequence),
                        chat_instance="synthetic",
                        data=value,
                        message=message,
                        from_user=user,
                    ),
                )
                if callback
                else Update(update_id=sequence, message=message)
            )
            await dispatcher.feed_update(bot, update)

        async def action(name):
            data = await storage.get_data(key)
            await send(
                f"tw:{data['text_draft']['revision']}:{name}",
                callback=True,
                message_id=data["screen_message_id"],
            )

        def latest():
            return [call for call in calls if isinstance(call, SendMessage)][-1]

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await send("/start")
                await send("section:texts", callback=True)
                assert "Промпты и ответы" in latest().text
                assert "Коммерческое предложение" not in str(latest().reply_markup)
                assert "Оффер и УТП" not in str(calls[-2:])
                await send("ct:new:offer", callback=True)
                assert "Что вы продаёте" in latest().text
                before = await storage.get_data(key)
                with Session(engine) as db, db.begin():
                    set_setting(
                        db,
                        config,
                        actors[0],
                        "prompt_template_offer",
                        "UPDATED\n" + before["text_draft"]["prompt_template"],
                    )
                stale_code = f"tw:{before['text_draft']['revision']}:skip"
                await action("skip")
                assert calls[-1].show_alert
                assert (await storage.get_data(key))["text_draft"]["step"] == 0
                start_of_flow = len(calls)
                await send("Курс Excel")
                await send("все")
                assert "Уточните" in latest().text
                await send("Бухгалтеры малого бизнеса")
                await send(stale_code, callback=True, message_id=before["screen_message_id"])
                assert calls[-1].show_alert
                fresh = PostgresStorage(engine)
                assert (await fresh.get_data(key))["text_draft"]["values"][
                    "product"
                ] == "Курс Excel"
                await fresh.close()
                await send("menu", callback=True)
                await send("ct:new:offer", callback=True)
                assert "главную проблему" in latest().text
                await send("Долго собирают отчёты")
                await send("Отчёт за 30 минут")
                await action("skip")
                await send("Консультация; Оставить заявку; дружелюбный тон")
                draft = (await storage.get_data(key))["text_draft"]
                first_id = draft["result_id"]
                assert "Промпт «УТП» готов" in latest().text
                assert "Продолжить" not in str(latest().reply_markup)
                assert not any(isinstance(call, EditMessageText) for call in calls[start_of_flow:])
                documents = [call for call in calls if isinstance(call, SendDocument)]
                with Session(engine) as db:
                    result = db.get(ToolResult, first_id)
                    original = result.result
                    assert not original.startswith("UPDATED")
                    assert result.owner_user_id == owners[1]
                    assert result.template_version.startswith("prompt-")
                    assert result.inputs["values"]["proof_difference"] == "нет"
                    assert documents[-1].document.data.decode() == original
                    assert db.get(Project, result.project_id).brief == {}
                # A repeat rendering must reuse the immutable result, not create a duplicate.
                await send("ct:resume", callback=True)
                assert (await storage.get_data(key))["text_draft"]["result_id"] == first_id
                chunks = []
                for page in range((len(original) + 1599) // 1600):
                    await send(f"ap:{first_id}:{page}", callback=True)
                    chunks.append(unescape(latest().text.split("<pre>")[1].split("</pre>")[0]))
                    assert len(latest().text) < 4096
                assert "".join(chunks) == original
                await send(f"hr:edit:{first_id}", callback=True)
                pending = (await storage.get_data(key))["history_pending"]
                await send("hr:confirm:" + pending["token"], callback=True)
                await send("ct:new:offer", callback=True)
                restored = (await storage.get_data(key))["text_draft"]
                assert restored["fields"] == list(ai_prompts.FIELDS["offer"])
                await send("Новый курс Excel")
                for _ in range(5):
                    await action("keep")
                changed_id = (await storage.get_data(key))["text_draft"]["result_id"]
                assert changed_id != first_id
                with Session(engine) as db:
                    assert db.get(ToolResult, first_id).result == original
                    assert "Новый курс Excel" in db.get(ToolResult, changed_id).result
                await send("ct:restart:offer", callback=True)
                assert (await storage.get_data(key))["text_draft"]["prompt_template"].startswith(
                    "UPDATED"
                )
                await send("ct:restart:landing", callback=True)
                for field in ai_prompts.FIELDS["landing"]:
                    if field == "evidence":
                        await action("skip")
                    else:
                        await send("Реальный ответ: " + field)
                landing_id = (await storage.get_data(key))["text_draft"]["result_id"]
                with Session(engine) as db:
                    assert "Карта лендинга" in db.get(ToolResult, landing_id).result
                await send("ct:new:case", callback=True)
                await send("Магазин одежды; заявок было мало")
                unfinished = (await storage.get_data(key))["text_draft"]
                await send("menu", callback=True)
                await send("ct:new:ca", callback=True)
                assert "Структура кейса" in latest().text
                assert "опрос будет заменён" in latest().text
                assert "ct:resume" in str(latest().reply_markup)
                assert "ct:restart:ca" in str(latest().reply_markup)
                assert (await storage.get_data(key))["text_draft"] == unfinished
                # Text on the choice screen must not be consumed as an answer.
                await send("Не ответ на опрос")
                assert (await storage.get_data(key))["text_draft"] == unfinished
                await send("ct:resume", callback=True)
                resumed = await storage.get_data(key)
                assert resumed["text_draft"]["values"] == unfinished["values"]
                assert resumed["text_draft"]["step"] == unfinished["step"]
                assert resumed["active_tool"] == "texts" and not resumed["paused"]
                assert "Что вы сделали" in latest().text
                unfinished = resumed["text_draft"]
                await send("ct:new:sequence", callback=True, query_id="sequence-retry")
                assert (await storage.get_data(key))["text_draft"] == unfinished
                assert "В боте отвечать на вопросы не нужно" in latest().text
                with Session(engine) as db:
                    sequence_result = db.scalar(
                        select(ToolResult)
                        .where(ToolResult.owner_user_id == owners[1], ToolResult.tool == "sequence")
                        .order_by(ToolResult.id.desc())
                    )
                    sequence_id = sequence_result.id
                    assert sequence_result.result == ai_prompts.build_prompt("sequence", {})
                await send("ct:new:sequence", callback=True, query_id="sequence-retry")
                with Session(engine) as db:
                    assert (
                        len(
                            list(
                                db.scalars(
                                    select(ToolResult).where(
                                        ToolResult.owner_user_id == owners[1],
                                        ToolResult.tool == "sequence",
                                    )
                                )
                            )
                        )
                        == 1
                    )
                await send(f"hr:open:{sequence_id}", callback=True)
                assert f"hr:edit:{sequence_id}" not in str(calls[-2:])
                await send(f"hr:edit:{sequence_id}", callback=True)
                assert calls[-1].show_alert
                await send("ct:new:case", callback=True)
                assert "Что вы сделали" in latest().text
                await send("Настроили рекламу и улучшили карточки")
                await send("До: 10 заявок; после: 30; за месяц. Отзыва нет.")
                await action("skip")
                case_id = (await storage.get_data(key))["text_draft"]["result_id"]
                with Session(engine) as db:
                    assert "все 10 блоков" in db.get(ToolResult, case_id).result
                    assert "До: 10 заявок" in db.get(ToolResult, case_id).result
                    objection = db.scalar(select(Objection).where(Objection.enabled.is_(True)))
                    objection_id = objection.id
                    expected_answer = objection.payload["answers"][0]
                await send(f"ob:open:{objection_id}", callback=True)
                copies = [
                    button.copy_text.text
                    for row in latest().reply_markup.inline_keyboard
                    for button in row
                    if button.copy_text
                ]
                assert expected_answer in copies
                await send(f"ob:write:{objection_id}", callback=True)
                personal = "Мой длинный ответ. " * 30
                await send(personal)
                pending = (await storage.get_data(key))["objection_draft"]
                await send("ob:save:" + pending["token"], callback=True)
                await send(f"ob:open:{objection_id}", callback=True)
                copy_data = (await storage.get_data(key))["objection_copy"]
                index = len(copy_data["answers"]) - 1
                chunks = []
                for page in range((len(personal) + 255) // 256):
                    await send(f"obc:{copy_data['token']}:{index}:{page}", callback=True)
                    chunks.append(latest().reply_markup.inline_keyboard[0][0].copy_text.text)
                assert "".join(chunks) == personal.strip()
                await send("/start", actor=actors[0])
                await send(f"ap:{first_id}:0", callback=True, actor=actors[0])
                assert calls[-1].show_alert
                await send(f"ap:{case_id}:0", callback=True, actor=actors[0])
                assert calls[-1].show_alert
                await send("/start lm_missing", actor=actors[2])
                await send(f"ap:{landing_id}:0", callback=True, actor=actors[2])
                assert calls[-1].show_alert
        finally:
            await bot.session.close()
            await storage.close()
            with Session(engine) as db, db.begin():
                db.execute(delete(Setting).where(Setting.key == "prompt_template_offer"))
                if previous_override is not None:
                    db.add(Setting(key="prompt_template_offer", value=previous_override))

    asyncio.run(scenario())

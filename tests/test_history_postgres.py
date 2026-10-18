import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendDocument,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application.entitlements import require_access
from leedbox.application.links import save_utm
from leedbox.application.results import delete_result, get_result, history_page, save_result
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import AccessGrant, AccessPolicy, ToolResult
from leedbox.telegram.calculators import SCHEMAS, VERSION, calculate
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage


@pytest.mark.integration
def test_history_scoped_pagination_deletion_and_expired_access(request):
    engine, _, _, owners, projects, _, _ = request.getfixturevalue("service_fixture")
    with Session(engine) as session, session.begin():
        ids = [
            save_result(
                session,
                owners[1],
                projects[1],
                123456,
                "test",
                {},
                "=2+3",
                f"history:{owners[1]}:{i}",
            ).id
            for i in range(7)
        ]
        assert len(history_page(session, owners[1], 123456)) == 6
        assert len(history_page(session, owners[1], 123456, page=1)) == 2
        assert history_page(session, owners[1], 123456, projects[1])[0].id == ids[-1]
        assert history_page(session, owners[1], 999) == []
        for foreign in (owners[0], owners[2]):
            assert history_page(session, foreign, 123456) == []
            with pytest.raises(ObjectUnavailable):
                delete_result(session, foreign, 123456, ids[0])
            with pytest.raises(ObjectUnavailable):
                history_page(session, foreign, 123456, projects[1])
        with pytest.raises(ObjectUnavailable):
            delete_result(session, owners[1], 999, ids[0])
        with pytest.raises(ValueError):
            history_page(session, owners[1], 123456, page=-1)
        delete_result(session, owners[1], 123456, ids[0])
        session.flush()
        assert session.get(ToolResult, ids[0]) is None

    # Roll back this temporary policy: tests must not alter the global access configuration.
    with Session(engine) as session:
        config = session.get(AccessPolicy, 1)
        config.mode, config.grace_days = "paid", 0
        session.execute(delete(AccessGrant).where(AccessGrant.user_id == owners[1]))
        session.flush()
        with pytest.raises(ValueError, match="Доступ закончился"):
            require_access(session, owners[1])
        assert get_result(session, owners[1], 123456, ids[1]).result == "=2+3"
        assert len(history_page(session, owners[1], 123456)) == 6
        delete_result(session, owners[1], 123456, ids[1])
        session.flush()
        assert session.get(ToolResult, ids[1]) is None
        session.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("saved_version", ["profit-planning-2", VERSION])
def test_history_replays_export_and_confirmations_without_real_telegram(request, saved_version):
    engine, config, actors, owners, projects, _, _ = request.getfixturevalue("service_fixture")
    values = {
        "target": "30000",
        "margin": "1000",
        "sale_rate": "33",
        "lead_rate": "10",
        "click_cost": "15",
        "platform": "yandex",
    }
    factors = []
    with Session(engine) as session, session.begin():
        calculation = save_result(
            session,
            owners[1],
            projects[1],
            123456,
            "ads",
            {"values": values, "factors": factors},
            ("Старый сохранённый отчёт\n" if saved_version != VERSION else "")
            + calculate("ads", values),
            f"history:calc:{owners[1]}",
            saved_version,
        )
        id, original = calculation.id, calculation.result
        utm_id = save_utm(
            session,
            owners[1],
            projects[1],
            123456,
            {
                "name": "История UTM",
                "platform": "site",
                "urls": "https://example.org",
                "source": "=formula",
                "medium": "email",
                "campaign": "campaign",
                "content": None,
                "term": None,
            },
            f"history:utm:{owners[1]}",
        ).id

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        storage = PostgresStorage(engine)
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(build_router(engine, config))
        bot_user = User(id=bot.id, is_bot=True, first_name="Test")
        sequence, screen_id, calls = 0, 1, []
        key = StorageKey(bot.id, actors[1], actors[1])

        async def api(_bot, method, timeout=None):
            nonlocal screen_id
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, AnswerCallbackQuery):
                return True
            screen_id = getattr(method, "message_id", None) or screen_id + 1
            return Message(
                message_id=screen_id,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=bot_user,
            )

        async def message(text, actor=actors[1]):
            nonlocal sequence
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    message=Message(
                        message_id=100 + sequence,
                        date=datetime.now(UTC),
                        chat=Chat(id=actor, type="private"),
                        from_user=User(id=actor, first_name="Test", is_bot=False),
                        text=text,
                    ),
                ),
            )

        async def callback(value, actor=actors[1]):
            nonlocal sequence
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    callback_query=CallbackQuery(
                        id=str(sequence),
                        data=value,
                        chat_instance="test",
                        from_user=User(id=actor, first_name="Test", is_bot=False),
                        message=Message(
                            message_id=screen_id,
                            date=datetime.now(UTC),
                            chat=Chat(id=actor, type="private"),
                            from_user=bot_user,
                        ),
                    ),
                ),
            )

        async def confirm():
            pending = (await storage.get_data(key))["history_pending"]
            await callback("hr:confirm:" + pending["token"])

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await message("/start")
                await callback("hr:list:0:0")
                assert "breakeven" not in calls[-2].text  # Tool labels are human readable.
                await callback("hr:list:1:1")
                await callback(f"hr:open:{id}")
                assert f"hr:md:{id}" in str(calls[-2].reply_markup)
                assert "hr:list:1:1" in str(calls[-2].reply_markup)
                await callback(f"hr:md:{id}")
                assert calls[-2].document.data.decode() == original
                await callback(f"hr:csv:{utm_id}")
                assert "'=formula" in calls[-2].document.data.decode("utf-8-sig")
                before_edit = len(calls)
                await callback(f"hr:edit:{id}")
                pending = (await storage.get_data(key))["history_pending"]
                assert pending["message_id"] == screen_id
                await confirm()
                await callback("calc:resume")
                assert not any(isinstance(call, EditMessageText) for call in calls[before_edit:])
                assert any(isinstance(call, EditMessageReplyMarkup) for call in calls[before_edit:])
                for _ in SCHEMAS["ads"]:
                    draft = (await storage.get_data(key))["calc_draft"]
                    await callback(f"cw:{draft['revision']}:keep")
                with Session(engine) as session:
                    copies = list(
                        session.scalars(
                            select(ToolResult).where(
                                ToolResult.owner_user_id == owners[1],
                                ToolResult.tool == "ads",
                            )
                        )
                    )
                    assert len(copies) == 2 and copies[0].id != copies[1].id
                    saved = session.get(ToolResult, id)
                    assert saved.result == original and saved.template_version == saved_version
                    rebuilt = next(row for row in copies if row.id != id)
                    assert rebuilt.result == calculate("ads", values)
                    assert rebuilt.template_version == VERSION
                await callback(f"hr:edit:{utm_id}")
                assert calls[-1].show_alert
                with Session(engine) as session:
                    assert (
                        session.scalar(
                            select(func.count())
                            .select_from(ToolResult)
                            .where(ToolResult.owner_user_id == owners[1], ToolResult.tool == "utm")
                        )
                        == 1
                    )
                await callback(f"hr:delete:{id}")
                pending = (await storage.get_data(key))["history_pending"]
                await callback("hr:list:0:0")
                await callback("hr:confirm:" + pending["token"])
                assert calls[-1].show_alert
                await callback(f"hr:delete:{id}")
                pending = (await storage.get_data(key))["history_pending"]
                await callback("menu")
                await callback("hr:confirm:" + pending["token"])
                assert calls[-1].show_alert
                with Session(engine) as session:
                    assert session.get(ToolResult, id) is not None
                await callback(f"hr:delete:{id}")
                pending = (await storage.get_data(key))["history_pending"]
                await confirm()
                await callback("hr:confirm:" + pending["token"])
                assert calls[-1].show_alert
                with Session(engine) as session:
                    assert session.get(ToolResult, id) is None
                    assert session.get(ToolResult, utm_id) is not None
                await message("/start", actors[0])
                for command in ("open", "csv", "delete", "edit"):
                    await callback(f"hr:{command}:{utm_id}", actors[0])
                    assert calls[-1].show_alert
                await message("/start lm_missing", actors[2])
                await callback("hr:list:0:0", actors[2])
                assert calls[-1].show_alert
                assert sum(isinstance(call, SendDocument) for call in calls) == 2
        finally:
            await bot.session.close()
            await storage.close()

    asyncio.run(scenario())

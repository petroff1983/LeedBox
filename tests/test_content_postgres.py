import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, GetMe
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_admin_postgres import admin_fixture  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application.admin_content import current_fingerprint, restore_version
from leedbox.application.content import CATALOGS, save_catalog, snapshot, tool_catalog
from leedbox.application.profile import delete_my_data
from leedbox.application.versions import fingerprint
from leedbox.infrastructure.content_seed import seed
from leedbox.infrastructure.models import (
    AdminMutation,
    ContentVersion,
    Niche,
    NicheSuggestion,
    ProductEvent,
    Project,
    ToolResult,
)
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage


@pytest.fixture
def content_fixture(request):
    context = request.getfixturevalue("service_fixture")
    engine = context[0]
    with Session(engine) as db, db.begin():
        before = {
            kind: [snapshot(row) for row in db.scalars(select(model))]
            for kind, model in CATALOGS.items()
        }
        versions = list(db.scalars(select(ContentVersion.id)))
        receipts = list(db.scalars(select(AdminMutation.key)))
        seed(db)
    try:
        yield context
    finally:
        with Session(engine) as db, db.begin():
            for kind, model in CATALOGS.items():
                db.execute(
                    delete(model).where(model.id.not_in([row["id"] for row in before[kind]]))
                )
                for saved in before[kind]:
                    row = db.get(model, saved["id"])
                    for key, value in saved.items():
                        setattr(row, key, value)
            db.execute(
                delete(ContentVersion).where(
                    ContentVersion.entity.in_(CATALOGS), ContentVersion.id.not_in(versions)
                )
            )
            db.execute(
                delete(AdminMutation).where(
                    AdminMutation.entity.in_(CATALOGS), AdminMutation.key.not_in(receipts)
                )
            )


@pytest.mark.integration
def test_catalog_seed_versions_conflicts_and_owner_permission(content_fixture):
    engine, config, actors, *_ = content_fixture
    with Session(engine) as db, db.begin():
        assert seed(db) == 0
        niches = list(db.scalars(select(Niche)))
        assert len(niches) == 12
        ideas = tool_catalog(db, "ideas")
        assert len(ideas) == 140
        for slug in ("marketer", "targetologist", "psychologist", "marketplace"):
            assert len([row for row in ideas if row["payload"]["niche"] == slug]) == 10
        assert all(row["payload"]["niche"] not in {"photographer", "consultant"} for row in ideas)
        row = niches[0]
        original = snapshot(row)
        values = {key: original[key] for key in ("slug", "title", "payload", "enabled")}
        with pytest.raises(PermissionError):
            save_catalog(db, config, actors[1], "niche", values, row.id, row.revision)
        changed = deepcopy(values)
        changed["title"] = "Новое название"
        save_catalog(db, config, actors[0], "niche", changed, row.id, row.revision)
        assert original["title"] != row.title and seed(db) == 0 and row.title == changed["title"]
        versions = list(
            db.scalars(
                select(ContentVersion)
                .where(ContentVersion.entity == "niche", ContentVersion.object_key == str(row.id))
                .order_by(ContentVersion.number)
            )
        )
        assert versions[0].data == values and versions[-1].data == changed
        restore_version(db, config, actors[0], versions[0].id, str(row.revision))
        assert row.title == values["title"] and row.revision == 3
        with pytest.raises(ValueError, match="изменилась"):
            save_catalog(db, config, actors[0], "niche", values, row.id, 1)
        # This temporary edit is rolled back; existing catalogs are not reset by seeding.
        db.rollback()
    with Session(engine) as db, db.begin():
        values = {
            "slug": "test-new",
            "title": "Тестовая ниша",
            "payload": {"audiences": ["Эксперты"]},
            "enabled": False,
        }
        row = save_catalog(db, config, actors[0], "niche", values, action_key="a" * 32)
        assert (
            save_catalog(db, config, actors[0], "niche", values, action_key="a" * 32).id == row.id
        )


@pytest.mark.integration
def test_content_admin_preview_csv_and_new_niches(request, content_fixture):
    client, csrf, _, context, _ = request.getfixturevalue("admin_fixture")
    engine = context[0]
    for kind in CATALOGS:
        response = client.get(f"/owner/content-{kind}/")
        assert response.status_code == 200
        assert 'name="published"' in response.text
        assert client.get(f"/owner/content-{kind}/csv").status_code == 200
    with Session(engine) as db:
        niche = db.scalar(select(Niche).where(Niche.slug == "psychologist"))
        id, revision = niche.id, niche.revision
    data = {
        "slug": "psychologist",
        "title": "Психолог",
        "audiences": "Клиенты",
        "enabled": "1",
        "action": "preview",
        "expected": str(revision),
        "csrf_token": csrf,
    }
    preview = client.post(f"/owner/content-niche/edit?id={id}", data=data)
    assert preview.status_code == 200 and "Клиенты" in preview.text
    with Session(engine) as db:
        assert db.get(Niche, id).revision == revision
    exported = client.get("/owner/content-niche/csv").data.decode("utf-8-sig")
    assert "Маркетолог" in exported and "Специалист по маркетплейсам" in exported
    assert "Фотограф" not in exported and "Бизнес-консультант" not in exported
    assert client.post("/owner/content-niche/import", data={"csv": exported}).status_code == 403
    invalid = exported.replace("psychologist", "INVALID!", 1)
    response = client.post(
        "/owner/content-niche/import",
        data={"csv": invalid, "confirmed": "1", "csrf_token": csrf, "action_key": "b" * 32},
    )
    assert response.status_code == 200 and "Код:" in response.text
    with Session(engine) as db:
        # The whole CSV import rolls back, including valid earlier rows.
        assert db.get(Niche, id).revision == revision


@pytest.mark.integration
def test_ideas_wizard_persistence_material_confirmation_and_isolation(content_fixture):
    engine, config, actors, owners, *_ = content_fixture

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        storage = PostgresStorage(engine)
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(build_router(engine, config))
        bot_user = User(id=bot.id, is_bot=True, first_name="Test")
        key = StorageKey(bot.id, actors[1], actors[1])
        sequence, screen, calls = 0, 1, []

        async def api(_bot, method, timeout=None):
            nonlocal screen
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, AnswerCallbackQuery):
                return True
            screen = getattr(method, "message_id", None) or screen + 1
            return Message(
                message_id=screen,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=bot_user,
            )

        async def send(value, callback=True, actor=actors[1]):
            nonlocal sequence
            sequence += 1
            user = User(id=actor, is_bot=False, first_name="Test")
            message = Message(
                message_id=screen if callback else 100 + sequence,
                date=datetime.now(UTC),
                chat=Chat(id=actor, type="private"),
                from_user=bot_user if callback else user,
                text=None if callback else value,
            )
            update = (
                Update(
                    update_id=sequence,
                    callback_query=CallbackQuery(
                        id=str(sequence),
                        data=value,
                        chat_instance="test",
                        from_user=user,
                        message=message,
                    ),
                )
                if callback
                else Update(update_id=sequence, message=message)
            )
            await dispatcher.feed_update(bot, update)

        async def action(command):
            draft = (await storage.get_data(key))["text_draft"]
            await send(f"tw:{draft['revision']}:{command}")

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await send("/start", False)
                await send("ct:new:ideas")
                with Session(engine) as db:
                    niche_id = db.scalar(select(Niche.id).where(Niche.slug == "marketer"))
                await action("choose:" + str(niche_id))
                assert (await storage.get_data(key))["text_draft"]["brief_pending"][
                    "answer"
                ] == "Маркетолог"
                await send("/start", False)
                assert "ct:resume" not in str(calls[-1].reply_markup)
                await send("ct:new:ideas")
                await action("brief:skip")
                await send("Малый бизнес", False)
                await action("brief:skip")
                await send("menu")
                # A newly constructed storage restores all fields without in-memory FSM.
                fresh = PostgresStorage(engine)
                assert (await fresh.get_data(key))["text_draft"]["values"]["niche"] == "marketer"
                await fresh.close()
                await send("ct:resume")
                await action("choose:requests")
                await action("skip")
                draft = (await storage.get_data(key))["text_draft"]
                result_id = draft["result_id"]
                with Session(engine) as db:
                    result = db.get(ToolResult, result_id)
                    original = result.result
                    assert db.get(Project, result.project_id).brief == {}
                    assert result.owner_user_id == owners[1] and "Нишевая идея" in original
                    assert (
                        result.template_version
                        == "content-" + fingerprint(result.inputs["catalog"])[:32]
                    )
                await action("more")
                with Session(engine) as db:
                    assert db.get(ToolResult, result_id).result == original
                await send(f"ci:prepare:{result_id}:0")
                pending = (await storage.get_data(key))["idea_material"]
                await send("menu")
                await send("ci:confirm:" + pending["token"])
                assert calls[-1].show_alert
                await send(f"ci:prepare:{result_id}:0")
                pending = (await storage.get_data(key))["idea_material"]
                await send("ci:confirm:" + pending["token"])
                assert (await storage.get_data(key))["material_draft"]["suggested_title"]
                await send("ct:new:offer")
                assert len((await storage.get_data(key))["text_draft"]["fields"]) == 6
                answers = {
                    "product": "Курс Excel",
                    "audience": "Бухгалтеры",
                    "problem": "Долгие отчёты",
                    "desired_result": "Отчёт за 30 минут",
                    "proof_difference": "Практика на отчётах",
                    "cta_tone": "Оставьте заявку; экспертный тон",
                }
                for field in (await storage.get_data(key))["text_draft"]["fields"]:
                    if field in answers:
                        await send(answers[field], False)
                    else:
                        await action("skip")
                offer_id = (await storage.get_data(key))["text_draft"]["result_id"]
                with Session(engine) as db:
                    assert "15 вариантов" in db.get(ToolResult, offer_id).result
                await send(f"hr:edit:{offer_id}")
                confirmation = (await storage.get_data(key))["history_pending"]
                await send("hr:confirm:" + confirmation["token"])
                await send("ct:resume")
                restored = (await storage.get_data(key))["text_draft"]
                assert restored["prompt_schema"] == 1
                for field in restored["fields"]:
                    await action("keep" if restored["values"].get(field) else "skip")
                await send("ct:new:landing")
                for field in (await storage.get_data(key))["text_draft"]["fields"]:
                    await send("Вводные: " + field, False)
                with Session(engine) as db:
                    result = db.get(
                        ToolResult, (await storage.get_data(key))["text_draft"]["result_id"]
                    )
                    assert "Карта лендинга" in result.result and "# РОЛЬ" in result.result
                await send("ct:new:ideas")
                with Session(engine) as db:
                    niche_id = db.scalar(select(Niche.id).where(Niche.slug == "psychologist"))
                await action("choose:" + str(niche_id))
                await action("brief:save")
                project_id = (await storage.get_data(key))["text_draft"]["project_id"]
                with Session(engine) as db:
                    assert db.get(Project, project_id).brief["niche"] == "Психолог"
                await send("/start", False, actors[0])
                await send(f"ci:prepare:{result_id}:0", actor=actors[0])
                assert calls[-1].show_alert
                await send("/start lm_missing", False, actors[2])
                await send("ct:new:ideas", actor=actors[2])
                assert calls[-1].show_alert
        finally:
            await bot.session.close()
            await storage.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_operations_scoped_events_suggestion_status_and_project_metadata(request, content_fixture):
    client, csrf, config, context, _ = request.getfixturevalue("admin_fixture")
    engine, _, _, owners, projects, *_ = context
    bot_id = int(config.bot_token.get_secret_value().split(":")[0])
    with Session(engine) as db, db.begin():
        suggestion = NicheSuggestion(owner_user_id=owners[1], name="Новая тестовая ниша")
        db.add(suggestion)
        db.add_all(
            [
                ProductEvent(
                    bot_id=bot_id, telegram_user_id=0, name="suspicious_activity", details={}
                ),
                ProductEvent(
                    bot_id=999, telegram_user_id=0, name="suspicious_activity", details={}
                ),
            ]
        )
        db.flush()
        suggestion_id = suggestion.id
        events = list(
            db.scalars(
                select(ProductEvent).where(
                    ProductEvent.name == "suspicious_activity", ProductEvent.telegram_user_id == 0
                )
            )
        )
        event_ids = [row.id for row in events]
        own_id = next(row.id for row in events if row.bot_id == bot_id)
        foreign_id = next(row.id for row in events if row.bot_id == 999)
        revision = db.get(Project, projects[1]).revision
    try:
        assert client.get("/owner/operations/suggestions").status_code == 200
        project_page = client.get(f"/owner/operations/projects?owner={owners[1]}&page=1")
        assert f"&amp;owner={owners[1]}" in project_page.text
        url = f"/owner/operations/suggestion?id={suggestion_id}"
        assert client.get(url).status_code == 200
        assert (
            client.post(
                url,
                data={
                    "csrf_token": csrf,
                    "confirmed": "1",
                    "expected": "new",
                    "status": "reviewed",
                },
            ).status_code
            == 302
        )
        assert (
            "изменился"
            in client.post(
                url,
                data={"csrf_token": csrf, "confirmed": "1", "expected": "new", "status": "closed"},
            ).text
        )
        body = client.get("/owner/operations/suspicious").text
        assert f"<td>{own_id}</td>" in body and f"<td>{foreign_id}</td>" not in body
        assert client.get("/owner/operations/stats").status_code == 200
        response = client.post(
            f"/owner/operations/project?id={projects[1]}",
            data={
                "csrf_token": csrf,
                "confirmed": "1",
                "expected": str(revision),
                "name": "Новое название",
                "timezone": "Europe/Moscow",
                "contact": "@test_owner",
            },
        )
        assert response.status_code == 302
        with Session(engine) as db:
            project = db.get(Project, projects[1])
            assert project.name == "Новое название" and project.owner_user_id == owners[1]
        with Session(engine) as db, db.begin():
            version = db.scalar(
                select(ContentVersion).where(
                    ContentVersion.entity == "project",
                    ContentVersion.object_key == str(projects[1]),
                    ContentVersion.number == 1,
                )
            )
            restore_version(
                db,
                config,
                context[2][0],
                version.id,
                current_fingerprint(db, "project", str(projects[1])),
            )
            assert db.get(Project, projects[1]).name == "Тест услуг"
            delete_my_data(db, context[2][1])
            assert (
                db.scalar(
                    select(ContentVersion.id).where(
                        ContentVersion.entity == "project",
                        ContentVersion.object_key == str(projects[1]),
                    )
                )
                is None
            )
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(ProductEvent).where(ProductEvent.id.in_(event_ids)))


@pytest.mark.integration
def test_content_render_snapshots(request, content_fixture):
    import re

    from test_admin_postgres import RENDER_ASSETS, RENDER_SNAPSHOTS

    client, csrf, _, context, _ = request.getfixturevalue("admin_fixture")
    with Session(context[0]) as db:
        idea = db.scalar(
            select(CATALOGS["idea"]).where(
                CATALOGS["idea"].payload["niche"].astext == "marketplace"
            )
        )
        template = db.scalar(
            select(CATALOGS["template"]).where(CATALOGS["template"].slug == "offer-aida")
        )
        paths = {
            "ideas": "/owner/content-idea/",
            "idea": f"/owner/content-idea/edit?id={idea.id}",
            "offer": f"/owner/content-template/edit?id={template.id}",
            "operations": "/owner/operations/",
            "case": "/owner/content-template/edit?id="
            + str(
                db.scalar(
                    select(CATALOGS["template"].id).where(CATALOGS["template"].slug == "case-main")
                )
            ),
            "proposal": "/owner/content-template/edit?id="
            + str(
                db.scalar(
                    select(CATALOGS["template"].id).where(
                        CATALOGS["template"].slug == "proposal-service"
                    )
                )
            ),
            "funnel": "/owner/content-template/edit?id="
            + str(
                db.scalar(
                    select(CATALOGS["template"].id).where(
                        CATALOGS["template"].slug == "funnel-main"
                    )
                )
            ),
            "objection": "/owner/content-objection/edit?id="
            + str(db.scalar(select(CATALOGS["objection"].id).order_by(CATALOGS["objection"].id))),
            "article": "/owner/content-article/edit",
            "recommendation": "/owner/content-recommendation/edit",
            "partner-stats": "/owner/operations/partners",
        }
    for name, path in paths.items():
        response = client.get(path)
        assert response.status_code == 200
        RENDER_SNAPSHOTS[name] = response.data
    response = client.post(
        "/owner/content-idea/import",
        data={"csrf_token": csrf, "confirmed": "1", "csv": "Неверный образец CSV"},
    )
    assert response.status_code == 200 and "Заголовок CSV не совпадает с образцом" in response.text
    assert "Экспортируйте каталог как образец и повторите импорт" in response.text
    assert "Неверный образец CSV" in response.text
    RENDER_SNAPSHOTS["import-error"] = response.data
    for body in RENDER_SNAPSHOTS.values():
        for path in re.findall(rb'(?:src|href)="(/(?:owner/)?static/[^"?]+)', body):
            url = path.decode()
            response = client.get(url)
            assert response.status_code == 200
            RENDER_ASSETS[url] = (response.data, response.content_type)

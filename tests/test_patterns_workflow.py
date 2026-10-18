# ruff: noqa: F811 -- pytest fixture injection.
import re
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from sqlalchemy.orm import Session
from test_content_agent_postgres import content_fixture, response, tick  # noqa: F401
from test_dzen_agent import ai_fixture  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import ai_tokens
from leedbox.application import content_agent as app
from leedbox.application.cabinet_access import issue_ticket
from leedbox.application.versions import fingerprint
from leedbox.domain import content_agent as domain
from leedbox.infrastructure.models import AIOperation, ContentTask
from leedbox.web.app import create_app


def analysis():
    return {
        "patterns": [{"name": "Вопрос в начале", "source_ids": ["1-1", "1-2"]}],
        "topics": [{"title": "Маркетинг"}, {"title": "Бизнес"}],
        "source_platform": "telegram",
    }


def launch(db, config, row):
    offer = app.quote(db, row)
    mode = ai_tokens.funding(db, config, row.owner_user_id, row.bot_id, row.test_mode, offer)
    return app.launch(
        db,
        config,
        row.owner_user_id,
        row.bot_id,
        row.token,
        fingerprint({**offer, "billing": mode})[:12],
    )


@pytest.mark.parametrize(
    ("raw", "counts", "topics"),
    [
        ("3 поста на тему маркетинг и 2 на тему бизнес", [3, 2], ["маркетинг", "бизнес"]),
        ("Напиши 1 пост про маркетплейсы", [1], ["маркетплейсы"]),
        ("5", [5], ["Темы из анализа"]),
        ("2 про маркетинг; 1 про бизнес", [2, 1], ["маркетинг", "бизнес"]),
    ],
)
def test_request_parser(raw, counts, topics):
    result = domain.generation_request(raw)
    assert [r["count"] for r in result] == counts
    assert [r["topic"] for r in result] == topics


@pytest.mark.parametrize(
    "raw", ["0", "11", "10 про бизнес и 1 про маркетинг", "много", "1 на тему", ""]
)
def test_invalid_request(raw):
    with pytest.raises(ValueError):
        domain.generation_request(raw)


@pytest.mark.integration
@pytest.mark.parametrize("count", [1, 5, 10])
@pytest.mark.parametrize("index", [0, 1])
def test_analysis_then_variable_batch_billing_and_reuse(content_fixture, count, index):
    engine, config, actors, owners, bot = content_fixture
    with Session(engine) as db, db.begin():
        before = ai_tokens.available(db, owners[index], bot, True)
        parent = app.create(db, config, owners[index], bot, actors[index], "manual")
        app.add_part(
            parent,
            1,
            "Пост про маркетинг, его паттерны и подачу.\n---\nПост про бизнес и клиентов.",
        )
        launch(db, config, parent)
        token = parent.token
    with patch(
        "leedbox.application.content_agent.deepseek_generate", return_value=response(analysis())
    ) as api:
        tick(content_fixture)
        assert tick(content_fixture) is None
        assert api.call_count == 1
    with Session(engine) as db, db.begin():
        parent = db.get(ContentTask, token)
        assert parent.status == "completed" and parent.payload["phase"] == "request"
        original = db.get(AIOperation, parent.operation_id)
        assert original.result["analysis_only"]
        assert original.result["analysis"]["source_count"] == 2
        assert "Маркетинг" in domain.markdown(original.result)
        assert ai_tokens.available(db, owners[index], bot, True) == before - (300 if index else 0)
        batch = app.generation_task(db, config, parent, str(count), "request-1")
        assert app.generation_task(db, config, parent, str(count), "request-1").token == batch.token
        batch_token = batch.token
        offer = app.quote(db, batch)
        assert offer["content_phase"] == "generation"
        launch(db, config, batch)
    value = {
        "posts": [
            {
                "title": f"Тема {i}",
                "parts": [f"Уникальный пост номер {i}. " + "Содержательная мысль о бизнесе. " * 6],
            }
            for i in range(count)
        ]
    }
    with patch(
        "leedbox.application.content_agent.deepseek_generate",
        side_effect=[response(value), response(value)],
    ) as api:
        tick(content_fixture)
        tick(content_fixture)
        assert tick(content_fixture) is None
        assert api.call_count == 2
        assert len(api.call_args.args[3]["ordered_topics"]) == count
    with Session(engine) as db, db.begin():
        batch = db.get(ContentTask, batch_token)
        assert batch.status == "completed"
        op = db.get(AIOperation, batch.operation_id)
        assert op.input_tokens == 400 and op.output_tokens == 200
        assert len(op.result["posts"]) == count
        assert ai_tokens.available(db, owners[index], bot, True) == before - (900 if index else 0)
        again = app.generation_task(
            db, config, batch, "2 про маркетинг и 1 про бизнес", "request-2"
        )
        assert again.payload["analysis_token"] == token
        assert len(domain.requested_topics(again.payload["requests"])) == 3
        assert db.get(ContentTask, token).status == "completed"
        analysis_number = db.get(ContentTask, token).operation_id
        generation_number = batch.operation_id
        ticket = urlsplit(issue_ticket(db, config, actors[index])).fragment.removeprefix("token=")
    if count == 1:
        client = create_app(config, engine).test_client()
        client.environ_base["REMOTE_ADDR"] = (
            f"2001:db8::{actors[index] >> 16:x}:{actors[index] & 65535:x}"
        )
        login = client.get("/cabinet/login?next=agents")
        csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', login.text)[1]
        assert (
            client.post(
                "/cabinet/login", data={"csrf": csrf, "token": ticket, "next": "agents"}
            ).status_code
            == 303
        )
        report = client.get(f"/cabinet/agents/{analysis_number}")
        assert report.status_code == 200 and "<h1>Анализ постов</h1>" in report.text
        assert "Изучено постов/фрагментов: 2" in report.text
        assert "Маркетинг" in client.get(f"/cabinet/agents/{analysis_number}/prompt").text
        posts_page = client.get(f"/cabinet/agents/{generation_number}")
        assert posts_page.status_code == 200 and "Готовых публикаций: 1" in posts_page.text
        assert "Скопировать пост" in posts_page.text


def test_analysis_never_invents_count_or_platform_and_copy_escapes():
    sources = domain.source_materials([{"id": "1", "text": "Один\n---\nДва"}])
    value = {**analysis(), "source_count": 99, "source_platform": "other"}
    result = domain.analysis_result(value, sources)
    assert result["source_count"] == 2 and result["source_platform"] == "unknown"
    assert "не определена" in domain.analysis_feedback(result)


@pytest.mark.parametrize("count", [1, 5, 10])
def test_dynamic_threads_stays_within_limit(count):
    value = {
        "posts": [
            {"title": str(i), "parts": [f"Пост {i}. " + "Мысль с эмодзи 🎉. " * 55]}
            for i in range(count)
        ]
    }
    result = domain.posts_result(value, "threads", ["Бизнес"] * count)
    assert all(domain.units(part) <= 500 for post in result["posts"] for part in post["parts"])


@pytest.mark.integration
def test_failed_batch_keeps_paid_analysis_and_does_not_charge_generation(content_fixture):
    engine, config, actors, owners, bot = content_fixture
    with Session(engine) as db, db.begin():
        row = app.create(db, config, owners[1], bot, actors[1], "manual")
        app.add_part(row, 1, "Пост про маркетинг.\n---\nДругой пост про бизнес.")
        launch(db, config, row)
        token = row.token
    with patch(
        "leedbox.application.content_agent.deepseek_generate", return_value=response(analysis())
    ):
        tick(content_fixture)
    with Session(engine) as db, db.begin():
        parent = db.get(ContentTask, token)
        before = ai_tokens.available(db, owners[1], bot, True)
        with pytest.raises(ValueError):
            app.generation_task(db, config, parent, "11", "invalid")
        batch = app.generation_task(db, config, parent, "5", "first")
        batch_token = batch.token
        launch(db, config, batch)
    with patch(
        "leedbox.application.content_agent.deepseek_generate", return_value=response({"posts": []})
    ) as api:
        tick(content_fixture)
        assert tick(content_fixture) is None
        api.assert_called_once()
    with Session(engine) as db, db.begin():
        batch = db.get(ContentTask, batch_token)
        assert batch.status == "failed"
        assert db.get(AIOperation, batch.operation_id).status == "failed"
        assert ai_tokens.available(db, owners[1], bot, True) == before
        parent = db.get(ContentTask, token)
        assert db.get(AIOperation, parent.operation_id).result["analysis_only"]
        retry = app.generation_task(db, config, parent, "3", "second")
        assert retry.token != batch_token and retry.operation_id is None


@pytest.mark.integration
def test_unlaunched_old_upload_is_upgraded_without_losing_materials(content_fixture):
    engine, config, actors, owners, bot = content_fixture
    with Session(engine) as db, db.begin():
        row = app.create(db, config, owners[1], bot, actors[1], "manual")
        app.add_part(row, 1, "Сохранённый пример для анализа и новых публикаций.")
        row.payload = {
            key: value for key, value in row.payload.items() if key != "patterns_version"
        }
        token = row.token
        db.flush()
        reopened = app.create(db, config, owners[1], bot, actors[1], "manual")
        assert reopened.token == token and reopened.payload["patterns_version"] == 2
        assert reopened.payload["parts"][0]["text"].startswith("Сохранённый пример")
        assert app.quote(db, reopened)["content_phase"] == "analysis"
        assert reopened.operation_id is None

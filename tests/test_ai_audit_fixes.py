"""Release acceptance for findings F01–F05 and persisted session receipts."""

# ruff: noqa: F811 -- fixtures imported for pytest injection.
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session
from test_content_agent_postgres import content_fixture, response, tick  # noqa: F401
from test_dzen_agent import ai_fixture, structured_writing_result, writing_brief  # noqa: F401
from test_image_agent import article
from test_lead_magnet_agent import critique, magnets
from test_lead_magnet_agent_postgres import ready
from test_patterns_workflow import analysis, launch
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import agents, ai_failures, ai_tokens, ai_usage
from leedbox.application import content_agent as content
from leedbox.application import lead_magnet_agent as lm
from leedbox.domain import content_agent as domain
from leedbox.domain.writing_profile import validate_prompt
from leedbox.infrastructure import agents as provider
from leedbox.infrastructure.models import AIOperation, ContentTask
from leedbox.telegram.lead_magnet_agent import screen


@pytest.mark.parametrize("repeat", range(3))
def test_partial_http_response_is_safe_unknown_error(repeat):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, *args):
            raise IncompleteRead(b"dummy-secret-body", 200)

    with (
        patch.object(
            provider, "build_opener", return_value=SimpleNamespace(open=lambda *a, **k: Response())
        ),
        pytest.raises(provider.AgentError) as caught,
    ):
        provider.deepseek_generate("dummy", "dummy", "JSON", {}, 20)
    assert caught.value.status is None and "secret" not in str(caught.value)


@pytest.mark.parametrize("status", [400, 401, 402, 403, 422, 429, 500, None, "stream"])
@pytest.mark.parametrize("repeat", range(3))
def test_provider_error_releases_only_confirmed_rejection(ai_fixture, status, repeat):
    engine, config, _, owners, bot = ai_fixture
    with Session(engine) as db:
        offer = agents.quote_images(db, article())
    error = (
        IncompleteRead(b"dummy-secret-body", 100)
        if status == "stream"
        else provider.AgentError("dummy-secret-body", status)
    )
    key = f"qa-accept-{status}-{repeat}"
    with (
        patch.object(agents, "deepseek_generate", side_effect=error) as api,
        patch.object(ai_failures, "alert") as alert,
    ):
        with pytest.raises(ValueError) as caught:
            agents.generate(engine, config, owners[1], bot, key, offer)
        with pytest.raises(ValueError):
            agents.generate(engine, config, owners[1], bot, key, offer)
        assert api.call_count == 1
        alert.assert_called_once()
    assert "secret" not in str(caught.value)
    rejected = provider.rejected_request(error)
    with Session(engine) as db:
        op = ai_tokens.previous(db, owners[1], bot, True, key)
        assert op.status == ("failed" if rejected else "uncertain")
        funds = ai_tokens.account(db, owners[1], bot, True)
        assert funds.balance == 50000
        assert funds.reserved == (0 if rejected else offer["maximum"])


def test_stale_foreground_operation_never_repeats_or_releases_unknown(ai_fixture):
    engine, config, _, owners, bot = ai_fixture
    with Session(engine) as db, db.begin():
        offer = agents.quote_images(db, article())
        op = ai_tokens.reserve(
            db, owners[1], bot, True, "qa-stale-foreground", offer["maximum"], offer
        )
        op.status, op.created_at = "sent", datetime.now(UTC) - timedelta(minutes=4)
        number = op.id
    ai_failures.recover_stale(engine, config, bot)
    ai_failures.recover_stale(engine, config, bot)
    with Session(engine) as db:
        assert db.get(AIOperation, number).status == "uncertain"
        assert ai_tokens.available(db, owners[1], bot, True) == 50000 - offer["maximum"]


@pytest.mark.parametrize("flat", [False, True])
def test_profile_normalizes_equivalent_structure_and_known_slots(flat):
    value = structured_writing_result()
    value["sections"]["quality"] += " Проверяй {{ title }} и {{inputs}} по брифу."
    if flat:
        value = value["sections"]
    text = validate_prompt(value, writing_brief())["prompt"]
    assert "{{" not in text and text.count("[ВСТАВЬТЕ ЗАГОЛОВОК]") == 1
    assert "6000 знаков без пробелов" in text
    value = structured_writing_result()
    value["sections"]["quality"] += " {{unknown_secret}}"
    with pytest.raises(ValueError, match="переменные"):
        validate_prompt(value, writing_brief())


def test_old_live_profile_failures_are_now_valid_when_equivalent():
    path = Path(".runtime/qa-predeploy-20261015/live-results.json")
    if not path.exists():
        pytest.skip("Local audit responses not distributed with source")
    for record in json.loads(path.read_text(encoding="utf-8")):
        if record["case"] == "LIVE-PROFILE":
            result = validate_prompt(json.loads(record["raw_output"]), record["inputs"]["brief"])
            assert len(result["prompt"]) >= 2500


@pytest.mark.parametrize("count", [1, 3, 5, 10])
def test_count_contract_removes_old_template_conflict(count):
    for template in (domain.DRAFT, domain.EDIT):
        text = domain.batch_instruction(template, count)
        if count != 3:
            assert "ровно три" not in text and "Ровно 3" not in text and "из трёх" not in text
    request = [{"count": count, "topic": "Маркетинг"}]
    inputs = domain.generation_inputs("Описание", "threads", analysis(), request)
    assert len(inputs["requested_posts"]) == count
    assert inputs["post_count"] == count
    assert "topics" not in inputs["plan"] and "source_count" not in inputs["plan"]
    assert f"ровно {count} posts" in domain.generation_contract(request)
    with pytest.raises(ValueError, match="порядок"):
        domain.posts_result(
            {
                "posts": [
                    {"post_id": 2, "title": "Тема", "parts": ["Полезная мысль о маркетинге. " * 5]}
                ]
            },
            "threads",
            ["Маркетинг"],
        )


@pytest.mark.parametrize("index", [0, 1])
def test_lm_session_receipt_survives_criticism_and_reopening(content_fixture, index):
    engine, config, _, owners, bot = content_fixture
    token = ready(content_fixture, index)
    with patch.object(
        lm,
        "deepseek_generate",
        side_effect=[response(magnets()), response(critique()), response(magnets("После критики"))],
    ):
        tick(content_fixture)
        with Session(engine) as db, db.begin():
            row = db.get(ContentTask, token)
            initial = screen(db, config, row)[0]
            assert "Потрачено: 300" in initial and "Всего за сессию: 300" in initial
            _, digest, _, _ = lm.confirmation(db, config, row, True)
            lm.launch(db, config, row, digest, True)
        tick(content_fixture)
        tick(content_fixture)
    with Session(engine) as db:
        row = db.get(ContentTask, token)
        text = screen(db, config, row)[0]
        assert "Потрачено: 600" in text and "Всего за сессию: 900" in text
        assert "Безлимитный доступ" in text if index == 0 else "Осталось:" in text
        assert ai_usage.task_total(db, row) == 900


def test_patterns_session_counts_analysis_once_and_all_batches(content_fixture):
    engine, config, actors, owners, bot = content_fixture
    with Session(engine) as db, db.begin():
        parent = content.create(db, config, owners[1], bot, actors[1], "manual")
        content.add_part(
            parent, 1, "Пост про маркетинг и подачу.\n---\nПост про бизнес и клиентов."
        )
        launch(db, config, parent)
        token = parent.token
    with patch.object(content, "deepseek_generate", return_value=response(analysis())):
        tick(content_fixture)
    for n in range(2):
        with Session(engine) as db, db.begin():
            parent = db.get(ContentTask, token)
            child = content.generation_task(
                db, config, parent, "1 про маркетинг", f"qa-session-{n}"
            )
            launch(db, config, child)
            child_token = child.token
        with patch.object(
            content,
            "deepseek_generate",
            return_value=response(
                {"posts": [{"title": "Маркетинг", "parts": ["Полезная мысль о маркетинге. " * 5]}]}
            ),
        ):
            tick(content_fixture)
            tick(content_fixture)
        with Session(engine) as db:
            child = db.get(ContentTask, child_token)
            assert ai_usage.task_total(db, child) == 300 + (n + 1) * 600
            assert f"Всего за сессию: {300 + (n + 1) * 600}" in ai_usage.task_receipt(db, child)
    with Session(engine) as db:
        parent = db.get(ContentTask, token)
        assert ai_usage.task_total(db, parent) == 1500


@pytest.mark.parametrize("mode", ["unlimited", "trial", "balance"])
def test_every_billing_mode_sees_usage_without_currency(mode):
    row = AIOperation(
        input_tokens=800,
        output_tokens=450,
        billing_mode=mode,
        status="completed",
        balance_after=5000,
    )
    text = ai_tokens.receipt(row, 4000)
    assert "Потрачено: 1250" in text and "Всего за сессию: 4000" in text
    assert "₽" not in text and "$" not in text


@pytest.mark.parametrize("kind", ["content", "lead_magnet"])
@pytest.mark.parametrize("status", [401, 429, "stream"])
def test_background_agents_share_safe_error_transitions(content_fixture, kind, status):
    engine, config, actors, owners, bot = content_fixture
    if kind == "lead_magnet":
        token = ready(content_fixture)
    else:
        with Session(engine) as db, db.begin():
            row = content.create(db, config, owners[1], bot, actors[1], "manual")
            content.add_part(row, 1, "Полезный пост про маркетинг и подачу материала.")
            launch(db, config, row)
            token = row.token
    error = (
        IncompleteRead(b"dummy", 100)
        if status == "stream"
        else provider.AgentError("dummy", status)
    )
    module = "lead_magnet_agent" if kind == "lead_magnet" else "content_agent"
    with (
        patch(
            f"leedbox.application.{module}.deepseek_generate",
            side_effect=error,
        ) as api,
        patch.object(ai_failures, "alert") as alert,
    ):
        tick(content_fixture)
        assert tick(content_fixture) is None
        api.assert_called_once()
        alert.assert_called_once()
    with Session(engine) as db:
        row = db.get(ContentTask, token)
        op = db.get(AIOperation, row.operation_id)
        assert row.status == op.status == ("uncertain" if status == "stream" else "failed")
        assert ai_tokens.account(db, owners[1], bot, True).reserved == (
            op.reserve_tokens if status == "stream" else 0
        )


@pytest.mark.parametrize("repair_outcome", ["valid", "invalid", "stream"])
def test_format_repair_is_bounded_and_usage_is_preserved(content_fixture, repair_outcome):
    engine, config, actors, owners, bot = content_fixture
    with Session(engine) as db, db.begin():
        parent = content.create(db, config, owners[1], bot, actors[1], "manual")
        content.add_part(parent, 1, "Пост про маркетинг.\n---\nПост про бизнес.")
        launch(db, config, parent)
        root = parent.token
    with patch.object(content, "deepseek_generate", return_value=response(analysis())):
        tick(content_fixture)
    with Session(engine) as db, db.begin():
        before = ai_tokens.available(db, owners[1], bot, True)
        row = content.generation_task(db, config, db.get(ContentTask, root), "1", "qa-repair")
        launch(db, config, row)
        token = row.token
    good = {
        "posts": [{"post_id": 1, "title": "Тема", "parts": ["Полезная мысль о маркетинге. " * 5]}]
    }
    broken = response(good)
    broken["choices"][0]["message"]["content"] = (
        '{"posts":[{"title":"Тема","parts":["Полезный текст"]"}]}'
    )
    correction = (
        response(good)
        if repair_outcome == "valid"
        else (
            IncompleteRead(b"dummy", 10) if repair_outcome == "stream" else response({"posts": []})
        )
    )
    with patch.object(
        content, "deepseek_generate", side_effect=[broken, correction, response(good)]
    ) as api:
        tick(content_fixture)
        if repair_outcome == "valid":
            tick(content_fixture)
        assert tick(content_fixture) is None
        assert api.call_count == (3 if repair_outcome == "valid" else 2)
    with Session(engine) as db:
        row = db.get(ContentTask, token)
        op = db.get(AIOperation, row.operation_id)
        assert row.steps[1]["repair_attempted"]
        assert (
            row.status
            == {"valid": "completed", "invalid": "failed", "stream": "uncertain"}[repair_outcome]
        )
        if repair_outcome == "valid":
            assert op.input_tokens + op.output_tokens == 900
            assert ai_tokens.available(db, owners[1], bot, True) == before - 900
            assert ai_usage.task_total(db, row) == 1200
        elif repair_outcome == "invalid":
            assert op.input_tokens + op.output_tokens == 600
            assert ai_tokens.available(db, owners[1], bot, True) == before
        else:
            assert row.steps[1]["usage"]["total_tokens"] == 300
            assert ai_tokens.account(db, owners[1], bot, True).reserved == op.reserve_tokens


def test_threads_paragraphs_are_packed_without_losing_content():
    parts = [f"Абзац {i}: содержательная мысль о маркетинге." for i in range(10)]
    result = domain.posts_result(
        {"posts": [{"title": "Тема", "parts": parts}]}, "threads", ["Маркетинг"]
    )
    text = "\n".join(result["posts"][0]["parts"])
    assert all(part in text for part in parts)
    assert all(domain.units(part) <= 500 for part in result["posts"][0]["parts"])


def test_parallel_claim_is_exclusive_for_real_database(content_fixture):
    engine, config, actors, owners, bot = content_fixture
    with Session(engine) as db, db.begin():
        for i in range(12):
            db.add(
                ContentTask(
                    token=f"qa-claim-{i}",
                    owner_user_id=owners[1],
                    bot_id=bot,
                    test_mode=True,
                    chat_id=actors[1],
                    status="queued",
                    ready_at=datetime.now(UTC),
                    payload={},
                    provider={},
                    steps=[],
                )
            )
    with ThreadPoolExecutor(max_workers=12) as pool:
        tokens = list(pool.map(lambda _: content.claim(engine, config, bot), range(12)))
    assert None not in tokens and len(set(tokens)) == 12
    assert content.claim(engine, config, bot) is None


@pytest.mark.parametrize("queued", [10, 50, 100])
def test_bounded_worker_pool_does_not_serialize_all_jobs(queued):
    from leedbox.telegram import content_agent as worker

    pending = list(range(queued))
    complete, active, maximum = [], 0, 0
    import threading

    lock = threading.Lock()

    def claim(*args):
        with lock:
            return str(pending.pop(0)) if pending else None

    def advance(*args):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        with lock:
            active -= 1
            complete.append(args[-1])

    async def notify(*args):
        return None

    async def run():
        task = asyncio.create_task(
            worker.background(SimpleNamespace(id=1), None, SimpleNamespace(ai_workers=4))
        )
        try:
            async with asyncio.timeout(10):
                while len(complete) < queued:
                    await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    started = time.perf_counter()
    with (
        patch.object(worker.app, "claim", side_effect=claim),
        patch.object(worker.app, "advance", side_effect=advance),
        patch.object(worker, "notify", side_effect=notify),
    ):
        asyncio.run(run())
    assert 1 < maximum <= 4
    assert len(set(complete)) == queued
    assert time.perf_counter() - started < 10

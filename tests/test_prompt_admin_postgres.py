import pytest
import test_admin_postgres
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application.admin_content import restore_version
from leedbox.application.prompt_templates import current_template
from leedbox.application.settings import setting
from leedbox.application.versions import fingerprint
from leedbox.domain import ai_prompts
from leedbox.infrastructure.models import ContentVersion

admin_fixture = test_admin_postgres.admin_fixture


@pytest.mark.integration
def test_prompt_editor_validation_csrf_preview_conflicts_and_rollback(admin_fixture):
    client, csrf, config, context, _ = admin_fixture
    engine, _, actors, *_ = context
    key = "prompt_template_offer"
    with Session(engine) as db:
        original = setting(db, key)
    assert client.get("/owner/prompts/").status_code == 200
    for tool in ai_prompts.FIELDS:
        assert client.get("/owner/prompts/edit?tool=" + tool).status_code == 200
    url = "/owner/prompts/edit?tool=offer"
    changed = "Новая инструкция\n" + original
    payload = {
        "csrf_token": csrf,
        "confirmed": "1",
        "expected": fingerprint(original),
        "body": changed,
        "action": "save",
    }
    assert client.post(url, data={**payload, "csrf_token": "bad"}).status_code == 403
    invalid = client.post(url, data={**payload, "body": "{{unknown}}"})
    assert invalid.status_code == 200 and "маркер" in invalid.text
    with Session(engine) as db:
        assert setting(db, key) == original
    preview = client.post(url, data={**payload, "action": "preview"})
    assert "Демонстрационные данные" in preview.text
    with Session(engine) as db:
        assert setting(db, key) == original
    assert client.post(url, data=payload).status_code == 302
    with Session(engine) as db:
        assert current_template(db, "offer") == changed
        versions = list(
            db.scalars(
                select(ContentVersion)
                .where(ContentVersion.entity == "setting", ContentVersion.object_key == key)
                .order_by(ContentVersion.id)
            )
        )
        original_version = next(v.id for v in versions if v.data["value"] == original)
    stale = client.post(url, data={**payload, "body": "Другая правка\n" + original})
    assert "изменилась" in stale.text
    with Session(engine) as db, db.begin():
        restore_version(db, config, actors[0], original_version, fingerprint(changed))
        assert setting(db, key) == original

"""Partner data and publication gate. Legal classification is supplied by the owner."""

import re
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.services import owner_required
from leedbox.application.versions import record_version
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.materials import http_url
from leedbox.domain.services import service_text
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AuditLog, Partner


@dataclass(frozen=True)
class PartnerInput:
    name: str
    description: str
    url: str
    advertiser: str = ""
    inn: str = ""
    erid: str = ""
    marking_text: str = ""
    exception_reason: str = ""
    enabled: bool = False

    def validated(self) -> dict[str, Any]:
        fields = asdict(self)
        for key, length in (
            ("name", 100),
            ("description", 1000),
            ("advertiser", 300),
            ("inn", 12),
            ("erid", 200),
            ("marking_text", 500),
            ("exception_reason", 1000),
        ):
            fields[key] = service_text(fields[key], length, required=key in {"name", "description"})
        fields["url"] = http_url(self.url)
        if self.inn and not re.fullmatch(r"\d{10}|\d{12}", self.inn, flags=re.ASCII):
            raise ValueError("ИНН должен содержать 10 или 12 цифр")
        if self.erid and not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", self.erid):
            raise ValueError("erid должен быть идентификатором без пробелов")
        marked = all(fields[key] for key in ("advertiser", "inn", "erid", "marking_text"))
        if type(self.enabled) is not bool or (
            self.enabled and not marked and not fields["exception_reason"]
        ):
            raise ValueError(
                "Для публикации заполните маркировку или документированное основание её отсутствия"
            )
        return fields


def save_partner(
    session: Session,
    config: Settings,
    actor: int,
    values: PartnerInput,
    id: int | None = None,
    expected_revision: int | None = None,
) -> Partner:
    owner_required(config, actor)
    fields = values.validated()
    row = session.scalar(select(Partner).where(Partner.id == id).with_for_update()) if id else None
    if id and row is None:
        raise ObjectUnavailable
    if row and row.revision != expected_revision:
        raise ValueError("Партнёр изменился. Откройте карточку заново")
    before = {key: getattr(row, key) for key in fields} if row else None
    if row is None:
        row = Partner(**fields)
        session.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
        row.revision += 1
    session.flush()
    record_version(session, "partner", str(row.id), actor, fields, before)
    session.add(
        AuditLog(
            actor_telegram_id=actor,
            action="partner_saved",
            details={"partner_id": row.id, "revision": row.revision},
        )
    )
    return row

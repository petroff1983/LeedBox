"""Personal answers are separate, owner-scoped data, not catalog mutations."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.versions import lock_content
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.content import text
from leedbox.infrastructure.models import Objection, PersonalObjection, User


def save_answer(
    session: Session, owner_id: int, objection_id: int, answer: str, expected: int
) -> PersonalObjection:
    answer = text(answer, 1000)
    if session.get(User, owner_id) is None:
        raise ObjectUnavailable
    catalog = session.get(Objection, objection_id)
    if catalog is None or not catalog.enabled:
        raise ObjectUnavailable
    lock_content(session, "personal_objection", f"{owner_id}:{objection_id}")
    row = session.scalar(
        select(PersonalObjection).where(
            PersonalObjection.owner_user_id == owner_id,
            PersonalObjection.objection_id == objection_id,
        )
    )
    if (row.revision if row else 0) != expected:
        if row and row.answer == answer:
            return row
        raise ValueError("Ответ изменился. Откройте карточку заново")
    if row is None:
        row = PersonalObjection(owner_user_id=owner_id, objection_id=objection_id, answer=answer)
        session.add(row)
    else:
        row.answer, row.revision = answer, row.revision + 1
    session.flush()
    return row

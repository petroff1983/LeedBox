"""Serialize creation per owner; persist uncertain outcomes instead of retrying POST."""

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.links import create_smart_link
from leedbox.application.settings import product_event
from leedbox.infrastructure.clck import ShortenerError, shorten
from leedbox.infrastructure.models import TrackedLink


def create_short_link(
    engine: Engine,
    owner_id: int,
    project_id: int,
    bot_id: int,
    destination: str,
    action_key: str,
    api_key: str,
) -> str:
    if not api_key:
        raise ShortenerError("Сервис коротких ссылок пока не настроен владельцем.")
    with Session(engine) as session, session.begin():
        link = create_smart_link(
            session,
            owner_id,
            project_id,
            bot_id,
            destination,
            "short",
            action_key,
            emit_event=False,
        )
        if link.external_url:
            return link.external_url
        if link.status in {"uncertain", "creating"}:
            raise ShortenerError(
                "Результат предыдущего запроса неизвестен. Проверьте кабинет clck.su; "
                "для новой попытки откройте «Короткая ссылка» заново."
            )
        link.status = "creating"
        link_id, destination = link.id, link.destination
    # Commit the claim BEFORE POST: a process crash must not silently repeat creation.
    failure = None
    remote = None
    try:
        remote = shorten(destination, api_key)
    except ShortenerError as error:
        failure = error
    with Session(engine) as session, session.begin():
        saved = session.scalar(
            select(TrackedLink).where(TrackedLink.id == link_id).with_for_update()
        )
        if saved is None:
            raise ShortenerError("Запись ссылки удалена. Откройте раздел «Ссылки» заново.")
        if failure:
            saved.status = "uncertain" if failure.uncertain else "failed"
        elif remote:
            saved.external_url, saved.external_id = remote.url, remote.id
            saved.status = "active"
            product_event(session, bot_id, None, "link_created", kind="short")
    if failure:
        raise failure
    assert remote is not None
    return remote.url

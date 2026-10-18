from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.versions import lock_content
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import Project, ProjectSelection, User


def register_user(session: Session, telegram_id: int, username: str | None = None) -> User:
    if telegram_id <= 0:
        raise ValueError("Telegram ID must be positive")
    statement = (
        insert(User)
        .values(telegram_id=telegram_id, username=username)
        .on_conflict_do_update(index_elements=[User.telegram_id], set_={"username": username})
        .returning(User)
    )
    return session.execute(statement).scalar_one()


def create_project(
    session: Session,
    owner_user_id: int,
    name: str,
    timezone: str = "Europe/Moscow",
    *,
    action_key: str | None = None,
) -> Project:
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError("Название проекта: от 1 до 100 символов")
    if any(ord(char) < 32 for char in name):
        raise ValueError("Название проекта не должно содержать управляющие символы")
    ZoneInfo(timezone)
    creation_key = None
    if action_key is not None:
        if not action_key or len(action_key) > 60:
            raise ValueError("Invalid project creation key")
        creation_key = f"{owner_user_id}:{action_key}"
        lock_content(session, "project_create", creation_key)
        existing = session.scalar(
            select(Project).where(
                Project.creation_key == creation_key, Project.owner_user_id == owner_user_id
            )
        )
        if existing:
            if existing.name != name or existing.timezone != timezone:
                raise ValueError("Эта форма уже сохранена. Откройте проект для изменения")
            return existing
    project = Project(
        owner_user_id=owner_user_id, name=name, timezone=timezone, creation_key=creation_key
    )
    session.add(project)
    session.flush()
    return project


def get_project(
    session: Session, owner_user_id: int, project_id: int, *, locked: bool = False
) -> Project:
    if not 0 < project_id <= 2**63 - 1:
        raise ObjectUnavailable
    query = select(Project).where(Project.id == project_id, Project.owner_user_id == owner_user_id)
    project = session.scalar(query.with_for_update() if locked else query)
    if project is None:
        raise ObjectUnavailable
    return project


def ensure_project(session: Session, owner_id: int) -> Project:
    if session.scalar(select(User.id).where(User.id == owner_id).with_for_update()) is None:
        raise ObjectUnavailable
    selection = session.get(ProjectSelection, owner_id)
    project = (
        session.scalar(
            select(Project).where(
                Project.owner_user_id == owner_id,
                Project.archived.is_(False),
                Project.id == selection.project_id,
            )
        )
        if selection
        else None
    )
    if project is None:
        project = session.scalar(
            select(Project)
            .where(Project.owner_user_id == owner_id, Project.archived.is_(False))
            .order_by(Project.id)
        )
    if project is None:
        project = create_project(session, owner_id, "Мой проект")
    if selection:
        selection.project_id = project.id
    else:
        session.add(ProjectSelection(owner_user_id=owner_id, project_id=project.id))
    session.flush()
    return project


def select_project(session: Session, owner_id: int, id: int, expected: int) -> None:
    if session.scalar(select(User.id).where(User.id == owner_id).with_for_update()) is None:
        raise ObjectUnavailable
    project = get_project(session, owner_id, id, locked=True)
    if project.archived or project.revision != expected:
        raise ValueError("Проект изменился или архивирован. Откройте его заново")
    selection = session.get(ProjectSelection, owner_id)
    if selection:
        selection.project_id = id
    else:
        session.add(ProjectSelection(owner_user_id=owner_id, project_id=id))
    session.flush()


def set_archived(session: Session, owner_id: int, id: int, expected: int, archived: bool) -> None:
    project = get_project(session, owner_id, id, locked=True)
    if project.archived == archived:
        return
    if project.revision != expected:
        raise ValueError("Проект изменился. Откройте его заново")
    project.archived = archived
    project.revision += 1


def save_brief_answer(
    session: Session, owner_id: int, id: int, expected: int, field: str, answer: str | None
) -> None:
    from leedbox.domain.brief import BRIEF_FIELDS, validate_answer

    project = get_project(session, owner_id, id, locked=True)
    if project.archived or project.revision != expected:
        raise ValueError("Проект изменился или архивирован. Откройте его заново")
    if field not in BRIEF_FIELDS:
        raise ValueError("Неизвестное поле брифа")
    brief = dict(project.brief)
    if answer is None:
        brief.pop(field, None)
    else:
        brief[field] = validate_answer(field, answer)
    project.brief = brief
    project.revision += 1

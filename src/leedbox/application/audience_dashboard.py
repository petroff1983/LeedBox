"""Read-only audience reports. Every query is scoped to one expert and bot."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, func, select, union_all
from sqlalchemy.orm import Session

from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.materials import http_url
from leedbox.infrastructure.models import (
    DeliveryEvent,
    LinkEvent,
    Material,
    MaterialVersion,
    ProductEvent,
    Recipient,
    TrackedLink,
)

PAGE_SIZE = 20


@dataclass(frozen=True)
class Period:
    start: datetime | None
    end: datetime | None
    label: str


def period_for(value: str, start: str = "", end: str = "", now: datetime | None = None) -> Period:
    now = now or datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if value == "all":
        return Period(None, None, "За всё время")
    if value in {"today", "7", "30"}:
        days = 1 if value == "today" else int(value)
        return Period(
            midnight - timedelta(days=days - 1),
            midnight + timedelta(days=1),
            "Сегодня" if days == 1 else f"Последние {days} дней",
        )
    if value == "custom":
        try:
            first = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC)
            last = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            raise ValueError("Укажите начальную и конечную дату") from None
        if not first <= last <= first + timedelta(days=365):
            raise ValueError("Период должен быть от 1 до 366 дней")
        return Period(first, last + timedelta(days=1), f"{first:%d.%m.%Y} — {last:%d.%m.%Y}")
    raise ValueError("Выберите период из списка")


class Dashboard:
    def __init__(
        self, session: Session, owner: int, bot: int, period: Period, material: int | None = None
    ) -> None:
        self.db, self.owner, self.bot, self.period, self.material = (
            session,
            owner,
            bot,
            period,
            material,
        )
        if material is not None and not session.scalar(
            select(Material.id).where(
                Material.id == material, Material.owner_user_id == owner, Material.bot_id == bot
            )
        ):
            raise ObjectUnavailable

    def scope(self) -> Any:
        conditions = [Material.owner_user_id == self.owner, Material.bot_id == self.bot]
        if self.material is not None:
            conditions.append(Material.id == self.material)
        return and_(*conditions)

    def window(self, field: Any) -> Any:
        conditions = []
        if self.period.start:
            conditions.append(field >= self.period.start)
        if self.period.end:
            conditions.append(field < self.period.end)
        return and_(True, *conditions)

    def starts(self) -> Select[Any]:
        return (
            select(
                ProductEvent.material_id.label("material_id"),
                ProductEvent.telegram_user_id.label("person"),
                ProductEvent.created_at.label("at"),
            )
            .join(Material, Material.id == ProductEvent.material_id)
            .where(
                self.scope(),
                ProductEvent.bot_id == self.bot,
                ProductEvent.name == "user_started",
                ProductEvent.details["kind"].as_string() == "recipient",
                self.window(ProductEvent.created_at),
            )
        )

    def deliveries(self) -> Select[Any]:
        return (
            select(
                DeliveryEvent.material_id.label("material_id"),
                DeliveryEvent.telegram_user_id.label("person"),
                DeliveryEvent.created_at.label("at"),
            )
            .join(Material, Material.id == DeliveryEvent.material_id)
            .where(
                self.scope(), DeliveryEvent.status == "sent", self.window(DeliveryEvent.created_at)
            )
        )

    def marked(self, platform: str = "", historical: bool = False) -> Select[Any]:
        statement = (
            select(
                LinkEvent.telegram_user_id.label("person"),
                LinkEvent.created_at.label("at"),
                LinkEvent.id.label("event_id"),
                TrackedLink.id.label("link_id"),
                TrackedLink.material_id.label("material_id"),
            )
            .join(TrackedLink, TrackedLink.id == LinkEvent.link_id)
            .join(Material, Material.id == TrackedLink.material_id)
            .where(
                self.scope(),
                TrackedLink.owner_user_id == self.owner,
                TrackedLink.bot_id == self.bot,
                TrackedLink.kind == "material",
                LinkEvent.kind == "start",
                LinkEvent.telegram_user_id.is_not(None),
            )
        )
        if not historical:
            statement = statement.where(self.window(LinkEvent.created_at))
        if platform:
            statement = statement.where(TrackedLink.source == platform)
        return statement

    def activity(self) -> Any:
        # UNION ALL then DISTINCT at aggregation: one person is never summed across gifts/sources.
        marked = self.marked().subquery()
        legacy = (
            select(
                Recipient.material_id.label("material_id"),
                Recipient.telegram_user_id.label("person"),
                Recipient.first_delivered_at.label("at"),
            )
            .join(Material, Material.id == Recipient.material_id)
            .where(self.scope(), self.window(Recipient.first_delivered_at))
        )
        return union_all(
            self.starts(),
            self.deliveries(),
            legacy,
            select(marked.c.material_id, marked.c.person, marked.c.at),
        ).subquery()

    def summary(self) -> dict[str, int]:
        starts, deliveries, activity = (
            self.starts().subquery(),
            self.deliveries().subquery(),
            self.activity(),
        )
        return {
            "people": self.db.scalar(select(func.count(func.distinct(activity.c.person)))) or 0,
            "starts": self.db.scalar(select(func.count()).select_from(starts)) or 0,
            "deliveries": self.db.scalar(select(func.count()).select_from(deliveries)) or 0,
            "recipients": self.db.scalar(select(func.count(func.distinct(deliveries.c.person))))
            or 0,
        }

    def options(self) -> tuple[list[Any], list[str]]:
        materials = list(
            self.db.execute(
                select(Material.id, MaterialVersion.title, Material.archived)
                .join(
                    MaterialVersion,
                    and_(
                        MaterialVersion.material_id == Material.id,
                        MaterialVersion.number == Material.current_version,
                    ),
                )
                .where(Material.owner_user_id == self.owner, Material.bot_id == self.bot)
                .order_by(Material.id)
            )
        )
        platforms = list(
            self.db.scalars(
                select(TrackedLink.source)
                .join(Material, Material.id == TrackedLink.material_id)
                .where(
                    self.scope(),
                    TrackedLink.owner_user_id == self.owner,
                    TrackedLink.bot_id == self.bot,
                    TrackedLink.kind == "material",
                )
                .distinct()
                .order_by(TrackedLink.source)
            )
        )
        return materials, platforms

    def materials(
        self, sort: str = "people", page: int = 0, limit: int = PAGE_SIZE
    ) -> list[dict[str, Any]]:
        starts, deliveries = self.starts().subquery(), self.deliveries().subquery()
        s = (
            select(
                starts.c.material_id,
                func.count().label("starts"),
                func.count(func.distinct(starts.c.person)).label("people"),
            )
            .group_by(starts.c.material_id)
            .subquery()
        )
        d = (
            select(
                deliveries.c.material_id,
                func.count().label("deliveries"),
                func.count(func.distinct(deliveries.c.person)).label("recipients"),
            )
            .group_by(deliveries.c.material_id)
            .subquery()
        )
        columns = {
            key: func.coalesce(source.c[key], 0).label(key)
            for source, keys in [(s, ("starts", "people")), (d, ("deliveries", "recipients"))]
            for key in keys
        }
        statement = (
            select(Material.id, MaterialVersion.title, Material.archived, *columns.values())
            .join(
                MaterialVersion,
                and_(
                    MaterialVersion.material_id == Material.id,
                    MaterialVersion.number == Material.current_version,
                ),
            )
            .outerjoin(s, s.c.material_id == Material.id)
            .outerjoin(d, d.c.material_id == Material.id)
            .where(self.scope())
            .order_by(columns.get(sort, columns["people"]).desc(), Material.id.desc())
            .offset(page * limit)
            .limit(limit + 1)
        )
        return [dict(row) for row in self.db.execute(statement).mappings()]

    def sources(
        self, platform: str = "", sort: str = "people", page: int = 0, limit: int = PAGE_SIZE
    ) -> list[dict[str, Any]]:
        events = self.marked(platform).subquery()
        counts = (
            select(
                events.c.link_id,
                func.count().label("starts"),
                func.count(func.distinct(events.c.person)).label("people"),
            )
            .group_by(events.c.link_id)
            .subquery()
        )
        people, starts = func.coalesce(counts.c.people, 0), func.coalesce(counts.c.starts, 0)
        statement = (
            select(
                TrackedLink.id,
                TrackedLink.source,
                TrackedLink.campaign,
                TrackedLink.external_url,
                MaterialVersion.title,
                people.label("people"),
                starts.label("starts"),
            )
            .join(Material, Material.id == TrackedLink.material_id)
            .join(
                MaterialVersion,
                and_(
                    MaterialVersion.material_id == Material.id,
                    MaterialVersion.number == Material.current_version,
                ),
            )
            .outerjoin(counts, counts.c.link_id == TrackedLink.id)
            .where(
                self.scope(),
                TrackedLink.owner_user_id == self.owner,
                TrackedLink.bot_id == self.bot,
                TrackedLink.kind == "material",
            )
        )
        if platform:
            statement = statement.where(TrackedLink.source == platform)
        rows = [
            dict(row)
            for row in self.db.execute(
                statement.order_by(
                    (starts if sort == "starts" else people).desc(), TrackedLink.id.desc()
                )
                .offset(page * limit)
                .limit(limit + 1)
            ).mappings()
        ]
        for row in rows:
            try:
                row["external_url"] = http_url(row["external_url"]) if row["external_url"] else None
            except ValueError:
                row["external_url"] = None
        return rows

    def people(self, platform: str = "", query: str = "", page: int = 0) -> list[dict[str, Any]]:
        activity = self.activity()
        statement = (
            select(
                activity.c.person,
                func.min(activity.c.at).label("first"),
                func.max(activity.c.at).label("last"),
            )
            .where(activity.c.person.is_not(None))
            .group_by(activity.c.person)
        )
        if platform:
            marked = self.marked(platform).subquery()
            statement = statement.where(activity.c.person.in_(select(marked.c.person)))
        if query:
            if not query.isascii() or not query.isdigit() or not 0 < int(query) < 2**63:
                raise ValueError("Для поиска введите числовой Telegram ID")
            statement = statement.where(activity.c.person == int(query))
        rows = [
            dict(row)
            for row in self.db.execute(
                statement.order_by(func.max(activity.c.at).desc(), activity.c.person)
                .offset(page * PAGE_SIZE)
                .limit(PAGE_SIZE + 1)
            ).mappings()
        ]
        ids = [row["person"] for row in rows]
        if not ids:
            return rows
        # First/last sources are all-time across this expert's gifts, not the selected period.
        history = Dashboard(self.db, self.owner, self.bot, Period(None, None, ""))
        marked = history.marked(historical=True).subquery()
        ranked = (
            select(
                marked.c.person,
                marked.c.link_id,
                func.row_number()
                .over(partition_by=marked.c.person, order_by=(marked.c.at, marked.c.event_id))
                .label("first_rank"),
                func.row_number()
                .over(
                    partition_by=marked.c.person,
                    order_by=(marked.c.at.desc(), marked.c.event_id.desc()),
                )
                .label("last_rank"),
            )
            .where(marked.c.person.in_(ids))
            .subquery()
        )
        labels: dict[int, dict[str, str]] = {person: {} for person in ids}
        for person, source, campaign, first, last in self.db.execute(
            select(
                ranked.c.person,
                TrackedLink.source,
                TrackedLink.campaign,
                ranked.c.first_rank,
                ranked.c.last_rank,
            )
            .join(TrackedLink, TrackedLink.id == ranked.c.link_id)
            .where((ranked.c.first_rank == 1) | (ranked.c.last_rank == 1))
        ):
            if first == 1:
                labels[person]["first_source"] = f"{source} · {campaign}"
            if last == 1:
                labels[person]["last_source"] = f"{source} · {campaign}"
        gifts: dict[int, list[str]] = {person: [] for person in ids}
        for person, title in self.db.execute(
            select(Recipient.telegram_user_id, MaterialVersion.title)
            .join(Material, Material.id == Recipient.material_id)
            .join(
                MaterialVersion,
                and_(
                    MaterialVersion.material_id == Material.id,
                    MaterialVersion.number == Material.current_version,
                ),
            )
            .where(history.scope(), Recipient.telegram_user_id.in_(ids))
            .order_by(Material.id)
        ):
            gifts[person].append(title)
        for row in rows:
            row.update(labels[row["person"]])
            row["gifts"] = gifts[row["person"]]
        return rows

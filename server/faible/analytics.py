"""Employee and studio analytics over the derived tables (DECISIONS #59).

Counting rules (also returned to the page as `rules`, so the dashboard states them):

- A *generation* is an activity captured live by the Faible extension, attributed to the
  Faible user signed in to it (hard rule 8). Never to the shared service login.
- Service history (`service_generations`, from the service's own history listings and from
  imported Higgsfield exporter files) that matches an activity on (service, job id) is the
  same generation, counted once, as the activity. History that matches no activity is
  *unattributed*: it is shown in its own section and never added to an employee's numbers.
- Costs are summed per currency exactly as the service reported them (`credits_cost`, the
  final value: a refunded failure reports 0). A live activity without a cost takes the cost
  its history record reports, counted as `cost_from_history`. Generations with no cost
  anywhere are counted as unknown, never as zero. The UI-credit figure for Higgsfield is an
  estimate (÷100) and is labelled as one.
- Day and hour buckets and the date range are in the requested IANA time zone. Activities
  are dated by when the extension saw the request; history by the service's creation time,
  or by when it was first observed when the service gave none.

Access: artists see their own activities, downloads and usage captures. Directors and admins
see every employee, the comparison, and unattributed history.
"""

import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import ColumnElement, and_, case, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from faible.models import (
    Activity,
    ActivityInput,
    ActivityOutput,
    Asset,
    AssetLocation,
    Download,
    Event,
    Link,
    Project,
    ServiceGeneration,
    Shot,
    UsageCapture,
    User,
)
from faible.views import _num

MANAGER_ROLES = {"admin", "director"}
NONE = "(none)"  # filter value: no model / unassigned shot
TERMINAL_BAD = ("failed", "cancelled")
HIGGSFIELD_UNITS_PER_UI_CREDIT = Decimal(100)

RULES = {
    "attribution": "Generations are attributed to the Faible user signed in to the extension "
    "that captured them, never to the shared Higgsfield/Magnific login.",
    "deduplication": "A history record with the same service and job id as a live capture is "
    "the same generation and is counted once. History matching no live capture is shown "
    "separately as unattributed.",
    "costs": "Costs are summed per currency as the service reported them (final cost: a "
    "refunded failure reports 0). Generations without a reported cost are counted as unknown, "
    "not as zero.",
    "estimate": "Estimated UI credits = Higgsfield cost ÷ 100 (the Higgsfield exporter's "
    "convention; Higgsfield's unit is not verified, see docs/adapters/higgsfield.md).",
    "dates": "Activities are dated by when the extension saw the request; history by the "
    "service's creation time (first observation when missing).",
}


def is_manager(user: User) -> bool:
    return user.role in MANAGER_ROLES


@dataclass(frozen=True)
class Filters:
    employee_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None
    tz: str = "UTC"
    service: str | None = None
    model: str | None = None
    status: str | None = None
    project_id: uuid.UUID | None = None
    shot: str | None = None  # shot uuid, or NONE for unassigned
    kind: str | None = None

    def bounds(self) -> tuple[datetime | None, datetime | None]:
        zone = ZoneInfo(self.tz)
        lo = datetime.combine(self.date_from, time.min, zone) if self.date_from else None
        hi = (
            datetime.combine(self.date_to + timedelta(days=1), time.min, zone)
            if self.date_to
            else None
        )
        return lo, hi

    @property
    def person_or_shot(self) -> bool:
        """Filters only a live capture can satisfy (history has no employee or shot)."""
        return bool(self.employee_id or self.project_id or self.shot)

    def as_dict(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "from": self.date_from,
            "to": self.date_to,
            "tz": self.tz,
            "service": self.service,
            "model": self.model,
            "status": self.status,
            "project_id": self.project_id,
            "shot": self.shot,
            "kind": self.kind,
        }


def make_filters(
    *,
    employee_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    tz: str | None = None,
    service: str | None = None,
    model: str | None = None,
    status: str | None = None,
    project_id: uuid.UUID | None = None,
    shot: str | None = None,
    kind: str | None = None,
) -> Filters:
    """Validated filters from query parameters (422 on a bad time zone, range or shot)."""
    tz = tz or "UTC"
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(422, f"unknown time zone {tz!r}") from exc
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "'from' is after 'to'")
    if shot and shot != NONE:
        try:
            uuid.UUID(shot)
        except ValueError as exc:
            raise HTTPException(422, "shot must be a shot id or '(none)'") from exc
    return Filters(
        employee_id=employee_id,
        date_from=date_from,
        date_to=date_to,
        tz=tz,
        service=service or None,
        model=model or None,
        status=status or None,
        project_id=project_id,
        shot=shot or None,
        kind=kind or None,
    )


def scoped(user: User, f: Filters) -> Filters:
    """Apply the role model: an artist only ever sees themselves."""
    if is_manager(user):
        return f
    if f.employee_id and f.employee_id != user.id:
        raise HTTPException(403, "You can only view your own dashboard")
    return replace(f, employee_id=user.id)


# ---------------------------------------------------------------- conditions


def activity_where(f: Filters) -> list[ColumnElement[bool]]:
    lo, hi = f.bounds()
    c: list[ColumnElement[bool]] = []
    if f.employee_id:
        c.append(Activity.captured_by_user_id == f.employee_id)
    if lo:
        c.append(Activity.started_at >= lo)
    if hi:
        c.append(Activity.started_at < hi)
    if f.service:
        c.append(Activity.service == f.service)
    if f.model:
        c.append(Activity.model_id.is_(None) if f.model == NONE else Activity.model_id == f.model)
    if f.status:
        c.append(Activity.status == f.status)
    if f.kind:
        c.append(Activity.kind == f.kind)
    if f.project_id:
        c.append(Activity.shot_id.in_(select(Shot.id).where(Shot.project_id == f.project_id)))
    if f.shot:
        c.append(
            Activity.shot_id.is_(None) if f.shot == NONE else Activity.shot_id == uuid.UUID(f.shot)
        )
    return c


def _matches_activity(sg: Any, *extra: ColumnElement[bool]) -> ColumnElement[bool]:
    return exists(
        select(Activity.id).where(
            Activity.service == sg.service, Activity.service_job_id == sg.service_job_id, *extra
        )
    )


def history_date(sg: Any = ServiceGeneration) -> ColumnElement[datetime]:
    return func.coalesce(sg.service_created_at, sg.first_observed_at)


def history_where(f: Filters) -> list[ColumnElement[bool]]:
    """History conditions. Employee/project/shot filters keep only history that matches a
    live capture satisfying them (unattributed history cannot belong to anyone)."""
    lo, hi = f.bounds()
    sg = ServiceGeneration
    c: list[ColumnElement[bool]] = []
    if lo:
        c.append(history_date() >= lo)
    if hi:
        c.append(history_date() < hi)
    if f.service:
        c.append(sg.service == f.service)
    if f.model:
        c.append(sg.model_id.is_(None) if f.model == NONE else sg.model_id == f.model)
    if f.status:
        c.append(sg.status == f.status)
    if f.kind:
        c.append(sg.kind == f.kind)
    if f.person_or_shot:
        people = activity_where(
            Filters(employee_id=f.employee_id, project_id=f.project_id, shot=f.shot, tz=f.tz)
        )
        c.append(_matches_activity(sg, *people))
    return c


# ---------------------------------------------------------------- activity aggregation

_hist = aliased(ServiceGeneration)
_join_hist = and_(
    _hist.service == Activity.service, _hist.service_job_id == Activity.service_job_id
)
_cost_from_history = and_(Activity.credits_cost.is_(None), _hist.credits_cost.is_not(None))
_cost = func.coalesce(Activity.credits_cost, _hist.credits_cost)
_currency = case((_cost_from_history, _hist.credits_currency), else_=Activity.credits_currency)


def _bucket() -> dict[str, Any]:
    return {
        "count": 0,
        "succeeded": 0,
        "failed": 0,
        "running": 0,
        "credits": {},
        "cost_known": 0,
        "cost_from_history": 0,
        "last_at": None,
    }


def _grouped(
    session: Session, f: Filters, *keys: Any, join_shot: bool = False
) -> dict[tuple[Any, ...], dict[str, Any]]:
    """One row per key with counts, status split and per-currency reported cost."""
    q = (
        select(
            *keys,
            _currency,
            func.count(),
            func.sum(case((Activity.status == "succeeded", 1), else_=0)),
            func.sum(case((Activity.status.in_(TERMINAL_BAD), 1), else_=0)),
            func.sum(case((Activity.status.in_(("queued", "running")), 1), else_=0)),
            func.coalesce(func.sum(_cost), 0),
            func.count(_cost),
            func.sum(case((_cost_from_history, 1), else_=0)),
            func.max(Activity.started_at),
        )
        .select_from(Activity)
        .outerjoin(_hist, _join_hist)
    )
    if join_shot:
        q = q.outerjoin(Shot, Shot.id == Activity.shot_id)
    q = q.where(*activity_where(f)).group_by(*keys, _currency)
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    n = len(keys)
    for row in session.execute(q):
        key = tuple(row[:n])
        currency, count, ok, bad, running, spend, known, from_hist, latest = row[n:]
        b = out.setdefault(key, _bucket())
        b["count"] += count
        b["succeeded"] += ok
        b["failed"] += bad
        b["running"] += running
        b["cost_known"] += known
        b["cost_from_history"] += from_hist
        if known:
            b["credits"][currency] = b["credits"].get(currency, Decimal(0)) + Decimal(spend)
        if b["last_at"] is None or latest > b["last_at"]:
            b["last_at"] = latest
    return out


def _finish(b: dict[str, Any]) -> dict[str, Any]:
    credits: dict[str, Decimal] = b["credits"]
    return {
        **b,
        "cost_unknown": b["count"] - b["cost_known"],
        "credits": {cur: _num(v) for cur, v in sorted(credits.items())},
        "estimated_ui_credits": estimated_ui_credits(credits),
    }


def estimated_ui_credits(credits: dict[str, Decimal]) -> str | None:
    units = credits.get("higgsfield")
    return None if units is None else _num(units / HIGGSFIELD_UNITS_PER_UI_CREDIT)


def _rows(grouped: dict[tuple[Any, ...], dict[str, Any]], label: Any) -> list[dict[str, Any]]:
    rows = [{**label(key), **_finish(b)} for key, b in grouped.items()]
    return sorted(rows, key=lambda r: (-r["count"], str(r.get("label"))))


# ---------------------------------------------------------------- read models


def _names(session: Session) -> dict[uuid.UUID, User]:
    return {u.id: u for u in session.scalars(select(User))}


def _shot_labels(session: Session) -> dict[uuid.UUID, tuple[str, str, uuid.UUID]]:
    return {
        s.id: (s.code, code, s.project_id)
        for s, code in session.execute(select(Shot, Project.code).join(Project))
    }


def _chain_url(shot_id: uuid.UUID | None, asset_id: uuid.UUID | None) -> str | None:
    """Where Faible shows this generation's prompt chain (spec §2.3, `faible.chain`)."""
    if asset_id and shot_id:
        return f"/shots/{shot_id}?asset={asset_id}"
    if asset_id:
        return f"/assets/{asset_id}"
    return f"/shots/{shot_id}" if shot_id else None


def _producers(session: Session, asset_ids: set[uuid.UUID]) -> dict[uuid.UUID, set[uuid.UUID]]:
    """asset → activities that produced it: output edges and asserted links, the same two
    edges `faible.chain` walks backwards."""
    out: dict[uuid.UUID, set[uuid.UUID]] = {}
    if not asset_ids:
        return out
    for aid, act in session.execute(
        select(ActivityOutput.asset_id, ActivityOutput.activity_id).where(
            ActivityOutput.asset_id.in_(asset_ids)
        )
    ):
        if aid is not None:
            out.setdefault(aid, set()).add(act)
    for aid, act in session.execute(
        select(Link.asset_id, Link.target_id).where(
            Link.asset_id.in_(asset_ids), Link.target_type == "activity"
        )
    ):
        out.setdefault(aid, set()).add(act)
    return out


def prompt_rows(
    session: Session, activities: list[Activity], users: dict[uuid.UUID, User]
) -> list[dict[str, Any]]:
    """Each generation with its prompt, inputs, outputs and its neighbours in the prompt
    chain: the generations that produced its inputs (upstream) and the ones that used its
    outputs (downstream). The full chain with prompt diffs is at `chain_url`."""
    ids = [a.id for a in activities]
    shots = _shot_labels(session)
    inputs: dict[uuid.UUID, list[ActivityInput]] = {}
    outputs: dict[uuid.UUID, list[ActivityOutput]] = {}
    for i in session.scalars(
        select(ActivityInput)
        .where(ActivityInput.activity_id.in_(ids))
        .order_by(ActivityInput.position)
    ):
        inputs.setdefault(i.activity_id, []).append(i)
    for o in session.scalars(
        select(ActivityOutput)
        .where(ActivityOutput.activity_id.in_(ids))
        .order_by(ActivityOutput.index)
    ):
        outputs.setdefault(o.activity_id, []).append(o)
    in_assets = {i.asset_id for rows in inputs.values() for i in rows if i.asset_id}
    out_assets = {o.asset_id for rows in outputs.values() for o in rows if o.asset_id}
    assets = (
        {
            a.id: a
            for a in session.scalars(select(Asset).where(Asset.id.in_(in_assets | out_assets)))
        }
        if in_assets | out_assets
        else {}
    )
    producers = _producers(session, in_assets)
    consumers: dict[uuid.UUID, set[uuid.UUID]] = {}
    if out_assets:
        for aid, act in session.execute(
            select(ActivityInput.asset_id, ActivityInput.activity_id).where(
                ActivityInput.asset_id.in_(out_assets)
            )
        ):
            if aid is not None:
                consumers.setdefault(aid, set()).add(act)
    related_ids = {x for s in producers.values() for x in s} | {
        x for s in consumers.values() for x in s
    }
    related = (
        {a.id: a for a in session.scalars(select(Activity).where(Activity.id.in_(related_ids)))}
        if related_ids
        else {}
    )
    first_output = {
        act: aid
        for act, aid in session.execute(
            select(ActivityOutput.activity_id, ActivityOutput.asset_id)
            .where(
                ActivityOutput.activity_id.in_(related_ids), ActivityOutput.asset_id.is_not(None)
            )
            .order_by(ActivityOutput.activity_id, ActivityOutput.index.desc())
        )
    }

    def asset_ref(aid: uuid.UUID | None) -> dict[str, Any] | None:
        a = assets.get(aid) if aid else None
        return (
            {"id": a.id, "sha256": a.sha256, "media_type": a.media_type, "stored": a.stored}
            if a
            else None
        )

    def neighbour(act_id: uuid.UUID) -> dict[str, Any] | None:
        n = related.get(act_id)
        if n is None:
            return None
        who = users.get(n.captured_by_user_id) if n.captured_by_user_id else None
        return {
            "id": n.id,
            "service": n.service,
            "model_id": n.model_id,
            "started_at": n.started_at,
            "prompt": (n.prompt or "")[:160],
            "email": who.email if who else None,
            "chain_url": _chain_url(n.shot_id, first_output.get(n.id)),
        }

    rows = []
    for a in activities:
        ins = inputs.get(a.id, [])
        outs = outputs.get(a.id, [])
        upstream = sorted(
            {p for i in ins if i.asset_id for p in producers.get(i.asset_id, set()) if p != a.id},
            key=str,
        )
        downstream = sorted(
            {c for o in outs if o.asset_id for c in consumers.get(o.asset_id, set()) if c != a.id},
            key=str,
        )
        first = next((o.asset_id for o in outs if o.asset_id), None)
        shot = shots.get(a.shot_id) if a.shot_id else None
        who = users.get(a.captured_by_user_id) if a.captured_by_user_id else None
        rows.append(
            {
                "id": a.id,
                "started_at": a.started_at,
                "service": a.service,
                "kind": a.kind,
                "model_id": a.model_id,
                "status": a.status,
                "prompt": a.prompt,
                "negative_prompt": a.negative_prompt,
                "credits_cost": _num(a.credits_cost),
                "credits_currency": a.credits_currency,
                "evidence": a.evidence,
                "email": who.email if who else None,
                "shot_id": a.shot_id,
                "shot_code": shot[0] if shot else None,
                "project_code": shot[1] if shot else None,
                "inputs": [
                    {
                        "role": i.role,
                        "asset": asset_ref(i.asset_id),
                        "service_asset_id": i.ref_service_asset_id,
                    }
                    for i in ins
                ],
                "outputs": [
                    {
                        "index": o.index,
                        "asset": asset_ref(o.asset_id),
                        "service_asset_id": o.service_asset_id,
                        "media_type": o.media_type,
                    }
                    for o in outs
                ],
                "upstream": [x for x in map(neighbour, upstream) if x],
                "downstream": [x for x in map(neighbour, downstream) if x],
                "chain_url": _chain_url(a.shot_id, first),
            }
        )
    return rows


def analytics_data(session: Session, user: User, f: Filters) -> dict[str, Any]:
    manager = is_manager(user)
    users = _names(session)
    if manager and f.employee_id and f.employee_id not in users:
        raise HTTPException(404, "Employee not found")
    f = scoped(user, f)
    shots = _shot_labels(session)
    projects = {p.id: p for p in session.scalars(select(Project).order_by(Project.code))}
    day = func.to_char(func.timezone(f.tz, Activity.started_at), "YYYY-MM-DD")
    hour = func.extract("hour", func.timezone(f.tz, Activity.started_at))

    def user_label(key: tuple[Any, ...]) -> dict[str, Any]:
        u = users.get(key[0]) if key[0] else None
        return {
            "key": str(key[0]) if key[0] else NONE,
            "label": u.email if u else "No Faible user",
            "display_name": u.display_name if u else None,
            "role": u.role if u else None,
        }

    def shot_label(key: tuple[Any, ...]) -> dict[str, Any]:
        s = shots.get(key[0]) if key[0] else None
        return {
            "key": str(key[0]) if key[0] else NONE,
            "label": f"{s[1]} / {s[0]}" if s else "Unassigned",
        }

    def project_label(key: tuple[Any, ...]) -> dict[str, Any]:
        p = projects.get(key[0]) if key[0] else None
        return {"key": str(key[0]) if key[0] else NONE, "label": p.code if p else "Unassigned"}

    total = _grouped(session, f).get((), _bucket())
    breakdowns = {
        "service": _rows(
            _grouped(session, f, Activity.service), lambda k: {"key": k[0], "label": k[0]}
        ),
        "model": _rows(
            _grouped(session, f, Activity.model_id, Activity.service),
            lambda k: {
                "key": k[0] or NONE,
                "label": k[0] or "Unknown model",
                "service": k[1],
            },
        ),
        "status": _rows(
            _grouped(session, f, Activity.status), lambda k: {"key": k[0], "label": k[0]}
        ),
        "kind": _rows(_grouped(session, f, Activity.kind), lambda k: {"key": k[0], "label": k[0]}),
        "project": _rows(_grouped(session, f, Shot.project_id, join_shot=True), project_label),
        "shot": _rows(_grouped(session, f, Activity.shot_id), shot_label),
    }
    by_day = sorted(
        ({"day": k[0], **_finish(b)} for k, b in _grouped(session, f, day).items()),
        key=lambda r: r["day"],
    )
    by_hour = sorted(
        ({"hour": int(k[0]), **_finish(b)} for k, b in _grouped(session, f, hour).items()),
        key=lambda r: r["hour"],
    )

    employees = None
    employee_models = None
    if manager and not f.employee_id:
        grouped = _grouped(session, f, Activity.captured_by_user_id)
        for uid in users:
            grouped.setdefault((uid,), _bucket())
        rows = _rows(grouped, user_label)
        for rank, row in enumerate(rows, 1):
            row["rank"] = rank if row["count"] else None
        employees = rows
        employee_models = [
            {"employee": str(k[0]) if k[0] else NONE, "model": k[1] or NONE, "count": b["count"]}
            for k, b in _grouped(
                session, f, Activity.captured_by_user_id, Activity.model_id
            ).items()
        ]

    ids = select(Activity.id).where(*activity_where(f))
    inputs_total, inputs_resolved = session.execute(
        select(func.count(), func.count(ActivityInput.asset_id)).where(
            ActivityInput.activity_id.in_(ids)
        )
    ).one()
    outputs_total, outputs_resolved = session.execute(
        select(func.count(), func.count(ActivityOutput.asset_id)).where(
            ActivityOutput.activity_id.in_(ids)
        )
    ).one()
    prompt_count, distinct_prompts = session.execute(
        select(func.count(Activity.prompt), func.count(func.distinct(Activity.prompt))).where(
            *activity_where(f)
        )
    ).one()
    recent = list(
        session.scalars(
            select(Activity)
            .where(*activity_where(f), Activity.prompt.is_not(None))
            .order_by(Activity.started_at.desc(), Activity.id.desc())
            .limit(25)
        )
    )

    downloads = downloads_data(session, f, users)
    return {
        "scope": {
            "is_manager": manager,
            "employee": user_label((f.employee_id,)) if f.employee_id else None,
            "filters": f.as_dict(),
        },
        "rules": RULES,
        "options": filter_options(session, user, users, shots),
        "totals": {
            **_finish(total),
            "inputs": {"total": inputs_total, "resolved": inputs_resolved},
            "outputs": {"total": outputs_total, "resolved": outputs_resolved},
            "prompts": {"total": prompt_count, "distinct": distinct_prompts},
            "downloads": downloads["total"],
        },
        "breakdowns": breakdowns,
        "by_day": by_day,
        "by_hour": by_hour,
        "employees": employees,
        "employee_models": employee_models,
        "prompts": prompt_rows(session, recent, users),
        "downloads": downloads,
        "usage_captures": usage_capture_list(session, user, f, limit=10),
        "history": history_summary(session, f) if manager else None,
    }


def filter_options(
    session: Session,
    user: User,
    users: dict[uuid.UUID, User],
    shots: dict[uuid.UUID, tuple[str, str, uuid.UUID]],
) -> dict[str, Any]:
    """Dropdown values: everything present in the viewer's own scope (artists never learn
    another employee's model or shot names from this list)."""
    manager = is_manager(user)
    mine = [] if manager else [Activity.captured_by_user_id == user.id]

    def distinct(col: Any, *where: ColumnElement[bool]) -> list[Any]:
        return sorted(
            {v for (v,) in session.execute(select(col).where(*where).distinct())},
            key=lambda v: (v is None, str(v)),
        )

    models = set(distinct(Activity.model_id, *mine))
    services = set(distinct(Activity.service, *mine))
    statuses = set(distinct(Activity.status, *mine))
    kinds = set(distinct(Activity.kind, *mine))
    if manager:
        models |= set(distinct(ServiceGeneration.model_id))
        services |= set(distinct(ServiceGeneration.service))
        statuses |= set(distinct(ServiceGeneration.status))
        kinds |= set(distinct(ServiceGeneration.kind))
    return {
        "employees": [
            {"id": u.id, "email": u.email, "display_name": u.display_name, "role": u.role}
            for u in sorted(users.values(), key=lambda u: u.email)
            if manager or u.id == user.id
        ],
        "services": sorted(services),
        "models": [NONE if m is None else m for m in sorted(models, key=lambda m: (m is None, m))],
        "statuses": sorted(statuses),
        "kinds": sorted(kinds),
        "projects": [
            {"id": p.id, "code": p.code, "name": p.name}
            for p in session.scalars(select(Project).order_by(Project.code))
        ],
        "shots": [
            {"id": sid, "code": code, "project": project, "project_id": pid}
            for sid, (code, project, pid) in sorted(
                shots.items(), key=lambda kv: kv[1][1:2] + kv[1][:1]
            )
        ],
        "timezones": ["UTC", "Asia/Kolkata"],
    }


def downloads_data(
    session: Session, f: Filters, users: dict[uuid.UUID, User], limit: int = 20
) -> dict[str, Any]:
    """Downloads filter by employee, period and service only: a download is a file, not a
    generation, so model/status/shot do not apply to it."""
    lo, hi = f.bounds()
    c: list[ColumnElement[bool]] = []
    if f.employee_id:
        c.append(Download.user_id == f.employee_id)
    if lo:
        c.append(Download.occurred_at >= lo)
    if hi:
        c.append(Download.occurred_at < hi)
    if f.service:
        c.append(Download.service == f.service)
    total, hashed = session.execute(
        select(func.count(), func.count(Download.sha256)).where(*c)
    ).one()
    rows = []
    for d in session.scalars(
        select(Download).where(*c).order_by(Download.occurred_at.desc()).limit(limit)
    ):
        who = users.get(d.user_id) if d.user_id else None
        rows.append(
            {
                "at": d.occurred_at,
                "filename": d.filename,
                "service": d.service,
                "email": who.email if who else None,
                "asset_id": d.asset_id,
                "hashed": d.sha256 is not None,
                "hash_method": d.hash_method,
                "evidence": d.evidence,
            }
        )
    return {"total": total, "hashed": hashed, "recent": rows}


# ---------------------------------------------------------------- usage captures


def usage_capture_list(
    session: Session, user: User, f: Filters, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    f = scoped(user, f)
    lo, hi = f.bounds()
    c: list[ColumnElement[bool]] = []
    if f.employee_id:
        c.append(UsageCapture.captured_by_user_id == f.employee_id)
    if lo:
        c.append(UsageCapture.captured_at >= lo)
    if hi:
        c.append(UsageCapture.captured_at < hi)
    if f.service:
        c.append(UsageCapture.service == f.service)
    users = _names(session)
    total = session.scalar(select(func.count()).select_from(UsageCapture).where(*c)) or 0
    rows = []
    for u in session.scalars(
        select(UsageCapture)
        .where(*c)
        .order_by(UsageCapture.captured_at.desc())
        .offset(offset)
        .limit(limit)
    ):
        who = users.get(u.captured_by_user_id) if u.captured_by_user_id else None
        rows.append(
            {
                "id": u.id,
                "captured_at": u.captured_at,
                "service": u.service,
                "email": who.email if who else None,
                "page_title": u.page_title,
                "page_url": u.page_url,
                "login": u.login_email or u.login_label,
                "response_count": u.response_count,
                "table_count": len(u.tables),
                "text_chars": u.text_chars,
                "source": u.source,
                "source_file": u.source_file,
            }
        )
    return {"total": total, "items": rows, "limit": limit, "offset": offset}


def usage_capture_detail(session: Session, user: User, capture_id: uuid.UUID) -> dict[str, Any]:
    row = session.get(UsageCapture, capture_id)
    if row is None or (not is_manager(user) and row.captured_by_user_id != user.id):
        raise HTTPException(404, "No such usage capture")
    payload = session.scalar(select(Event.payload).where(Event.event_id == row.event_id)) or {}
    text = payload.get("text") or ""
    return {
        "id": row.id,
        "captured_at": row.captured_at,
        "page_title": row.page_title,
        "page_url": row.page_url,
        "login": row.login_email or row.login_label,
        "dom_evidence": payload.get("dom_evidence", "observed_dom"),
        "tables": row.tables,
        "responses": [
            {
                "url": r.get("url"),
                "page_path": r.get("page_path"),
                "captured_at": r.get("captured_at"),
            }
            for r in payload.get("responses") or []
        ],
        "text_excerpt": text[:5000],
        "text_chars": len(text),
    }


# ---------------------------------------------------------------- service history


def history_summary(session: Session, f: Filters) -> dict[str, Any]:
    sg = ServiceGeneration
    where = history_where(f)
    matched = _matches_activity(sg)
    day = func.to_char(func.timezone(f.tz, history_date()), "YYYY-MM-DD")

    def grouped(*keys: Any) -> list[dict[str, Any]]:
        q = (
            select(
                *keys,
                sg.credits_currency,
                func.count(),
                func.sum(case((matched, 0), else_=1)),
                func.coalesce(func.sum(case((matched, None), else_=sg.credits_cost)), 0),
                func.count(case((matched, None), else_=sg.credits_cost)),
            )
            .where(*where)
            .group_by(*keys, sg.credits_currency)
        )
        out: dict[tuple[Any, ...], dict[str, Any]] = {}
        n = len(keys)
        for row in session.execute(q):
            key = tuple(row[:n])
            currency, count, unmatched, spend, known = row[n:]
            b = out.setdefault(key, {"count": 0, "unattributed": 0, "credits": {}, "cost_known": 0})
            b["count"] += count
            b["unattributed"] += unmatched
            b["cost_known"] += known
            if known:
                b["credits"][currency] = b["credits"].get(currency, Decimal(0)) + Decimal(spend)
        return [
            {
                "key": list(k),
                **b,
                "credits": {c: _num(v) for c, v in b["credits"].items()},
                "estimated_ui_credits": estimated_ui_credits(b["credits"]),
            }
            for k, b in out.items()
        ]

    total = next(iter(grouped()), {"count": 0, "unattributed": 0, "credits": {}, "cost_known": 0})
    login = func.coalesce(sg.login_email, sg.login_label)
    return {
        "applies": not f.person_or_shot,
        "note": (
            "Employee, project and shot filters keep only history matching a live capture; "
            "unattributed history cannot belong to an employee or shot."
            if f.person_or_shot
            else None
        ),
        "totals": total,
        "by_login": sorted(grouped(login), key=lambda r: -r["count"]),
        "by_model": sorted(grouped(sg.model_id), key=lambda r: -r["count"]),
        "by_status": sorted(grouped(sg.status), key=lambda r: -r["count"]),
        "by_day": sorted(grouped(day), key=lambda r: r["key"][0]),
        "credit_note": "Unattributed credits only: matched history is counted with its activity.",
    }


def history_page(
    session: Session,
    user: User,
    f: Filters,
    *,
    unattributed_only: bool = False,
    login: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    if not is_manager(user):
        raise HTTPException(403, "Studio history is visible to directors and admins")
    sg = ServiceGeneration
    where = history_where(f)
    if unattributed_only:
        where.append(~_matches_activity(sg))
    if login:
        where.append(
            or_(sg.login_email == login, sg.login_label == login)
            if login != NONE
            else and_(sg.login_email.is_(None), sg.login_label.is_(None))
        )
    total = session.scalar(select(func.count()).select_from(sg).where(*where)) or 0
    rows = list(
        session.scalars(
            select(sg)
            .where(*where)
            .order_by(history_date().desc(), sg.service_job_id)
            .offset(offset)
            .limit(limit)
        )
    )
    users = _names(session)
    shots = _shot_labels(session)
    keys = [(r.service, r.service_job_id) for r in rows]
    acts = (
        {
            (a.service, a.service_job_id): a
            for a in session.scalars(
                select(Activity).where(
                    or_(
                        *[
                            and_(Activity.service == s, Activity.service_job_id == j)
                            for s, j in keys
                        ]
                    )
                )
            )
        }
        if keys
        else {}
    )
    urls = {o["url"] for r in rows for o in r.outputs if isinstance(o, dict) and o.get("url")}
    located = (
        dict(
            session.execute(
                select(AssetLocation.url, AssetLocation.asset_id).where(AssetLocation.url.in_(urls))
            ).all()
        )
        if urls
        else {}
    )
    items = []
    for r in rows:
        a = acts.get((r.service, r.service_job_id))
        who = users.get(a.captured_by_user_id) if a and a.captured_by_user_id else None
        shot = shots.get(a.shot_id) if a and a.shot_id else None
        items.append(
            {
                "id": r.id,
                "service": r.service,
                "service_job_id": r.service_job_id,
                "model_id": r.model_id,
                "kind": r.kind,
                "status": r.status,
                "service_status": r.service_status,
                "prompt": r.prompt,
                "created_at": r.service_created_at,
                "first_observed_at": r.first_observed_at,
                "observation_count": r.observation_count,
                "credits_cost": _num(r.credits_cost),
                "credits_currency": r.credits_currency,
                "fail_reason": r.fail_reason,
                "login": r.login_email or r.login_label,
                "service_user_id": r.service_user_id,
                "inputs": len(r.inputs),
                "outputs": [
                    {**o, "asset_id": located.get(str(o.get("url") or ""))}
                    for o in r.outputs
                    if isinstance(o, dict)
                ],
                "evidence": r.evidence,
                "attributed_to": (
                    {
                        "activity_id": a.id,
                        "email": who.email if who else None,
                        "shot_code": shot[0] if shot else None,
                    }
                    if a
                    else None
                ),
            }
        )
    return {"total": total, "items": items, "limit": limit, "offset": offset}

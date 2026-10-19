"""Web UI (spec §7): server-rendered Jinja2, plain forms, an httpOnly session cookie.

Pages: login, projects, project (shots), shot (versions + chain), asset (preview, hash,
locations, chain, similar, assert-link form), unassigned queue, import.
Every state change goes through the same event log as the API (`link.asserted`,
`shot_version.created`, `asset.registered`), attributed to the logged-in user.

CSRF: the session cookie is SameSite=Lax (not sent on cross-site POSTs) and POSTs whose
Origin header names another host are refused.
"""

import hashlib
import io
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from faible.api import SESSION_COOKIE
from faible.auth import AuthError, authenticate, decode_token, is_disabled, issue_token
from faible.chain import asset_chain, union_chain
from faible.db import get_session
from faible.events import Evidence
from faible.ingest_asset import AssetRejected, ingest_asset
from faible.log import EventIn, append_events
from faible.models import (
    Activity,
    ActivityOutput,
    Asset,
    Project,
    Sequence,
    Shot,
    ShotVersion,
    User,
)
from faible.views import asset_detail, project_tree, shot_detail

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(include_in_schema=False)
DbSession = Annotated[Session, Depends(get_session)]


class LoginRequired(Exception):
    pass


def web_user(session: DbSession, faible_session: Annotated[str | None, Cookie()] = None) -> User:
    if not faible_session:
        raise LoginRequired
    try:
        user = session.get(User, decode_token(faible_session, "web"))
    except AuthError as exc:
        raise LoginRequired from exc
    if user is None or is_disabled(session, user.id):
        raise LoginRequired
    return user


WebUser = Annotated[User, Depends(web_user)]


def same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-origin form post refused")


def page(request: Request, name: str, user: User | None, **ctx: Any) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, name, {"user": user, **ctx})


def emit(
    session: Session, user: User, type_: str, payload: dict[str, Any], evidence: Evidence
) -> None:
    (r,) = append_events(
        session,
        [
            EventIn(
                event_id=uuid.uuid4(),
                type=type_,
                occurred_at=datetime.now(UTC),
                evidence=evidence,
                payload=payload,
            )
        ],
        source="server",
        actor_user_id=user.id,
    )
    if r.status != "accepted":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, r.reason or r.status)


def all_shots(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Shot, Project.code)
        .join(Project, Project.id == Shot.project_id)
        .order_by(Project.code, Shot.code)
    ).all()
    return [
        {"id": s.id, "code": s.code, "project": code, "description": s.description}
        for s, code in rows
    ]


def unassigned_count(session: Session) -> int:
    return (
        session.scalar(select(func.count()).select_from(Activity).where(Activity.shot_id.is_(None)))
        or 0
    )


# ---------------------------------------------------------------- auth


@router.get("/dashboard")
def dashboard_page(request: Request, user: WebUser) -> HTMLResponse:
    return page(request, "dashboard.html", user)


@router.get("/login")
def login_form(request: Request, error: str | None = None) -> HTMLResponse:
    return page(request, "login.html", None, error=error)


@router.post("/login")
def login(
    request: Request,
    session: DbSession,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    same_origin(request)
    try:
        user_id = authenticate(session, email, password, "web")
    except AuthError:
        return RedirectResponse("/login?error=Wrong+email+or+password", status.HTTP_303_SEE_OTHER)
    session.commit()
    res = RedirectResponse("/dashboard", status.HTTP_303_SEE_OTHER)
    res.set_cookie(
        SESSION_COOKIE,
        issue_token(user_id, "web"),
        httponly=True,
        samesite="lax",
        max_age=30 * 86400,
    )
    return res


@router.get("/logout")
def logout() -> Response:
    res = RedirectResponse("/login", status.HTTP_303_SEE_OTHER)
    res.delete_cookie(SESSION_COOKIE)
    return res


# ---------------------------------------------------------------- projects and shots


@router.get("/")
def home(request: Request, session: DbSession, user: WebUser) -> Response:
    projects = list(session.scalars(select(Project).order_by(Project.code)))
    if len(projects) == 1 and user.role not in ("admin", "director"):
        return RedirectResponse(f"/projects/{projects[0].id}", status.HTTP_303_SEE_OTHER)
    return page(
        request, "projects.html", user, projects=projects, unassigned=unassigned_count(session)
    )


def require_manager(user: User) -> None:
    if user.role not in ("admin", "director"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")


@router.post("/projects")
def create_project(
    request: Request,
    session: DbSession,
    user: WebUser,
    code: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
) -> Response:
    """Admin creates a project; employees then pick its scenes in the extension."""
    same_origin(request)
    require_manager(user)
    code = code.strip().upper().replace(" ", "_")
    if not code:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "project code required")
    if session.scalar(select(Project).where(Project.code == code)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"project {code} already exists")
    project_id = uuid.uuid4()
    emit(
        session,
        user,
        "project.created",
        {"project_id": str(project_id), "name": name.strip() or code, "code": code},
        "originated",
    )
    session.commit()
    return RedirectResponse(f"/projects/{project_id}", status.HTTP_303_SEE_OTHER)


@router.post("/projects/{project_id}/scenes")
def create_scene(
    project_id: uuid.UUID,
    request: Request,
    session: DbSession,
    user: WebUser,
    code: Annotated[str, Form()],
    sequence: Annotated[str, Form()] = "Main",
    description: Annotated[str, Form()] = "",
) -> Response:
    """Admin adds a scene (a shot inside a sequence) that employees can select."""
    same_origin(request)
    require_manager(user)
    if session.get(Project, project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    code = code.strip().upper().replace(" ", "_")
    sequence = sequence.strip() or "Main"
    if not code:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "scene code required")
    if session.scalar(select(Shot).where(Shot.project_id == project_id, Shot.code == code)):
        raise HTTPException(status.HTTP_409_CONFLICT, f"scene {code} already exists")
    seq = session.scalar(
        select(Sequence).where(Sequence.project_id == project_id, Sequence.name == sequence)
    )
    if seq is None:
        sequence_id = uuid.uuid4()
        order = (
            session.scalar(
                select(func.count()).select_from(Sequence).where(Sequence.project_id == project_id)
            )
            or 0
        ) + 1
        emit(
            session,
            user,
            "sequence.created",
            {
                "sequence_id": str(sequence_id),
                "project_id": str(project_id),
                "name": sequence,
                "order": order,
            },
            "originated",
        )
    else:
        sequence_id = seq.id
    emit(
        session,
        user,
        "shot.created",
        {
            "shot_id": str(uuid.uuid4()),
            "sequence_id": str(sequence_id),
            "code": code,
            "description": description.strip(),
        },
        "originated",
    )
    session.commit()
    return RedirectResponse(f"/projects/{project_id}", status.HTTP_303_SEE_OTHER)


@router.get("/projects/{project_id}")
def project_page(
    project_id: uuid.UUID, request: Request, session: DbSession, user: WebUser
) -> HTMLResponse:
    tree = project_tree(session, project_id)
    if tree is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    stats: dict[uuid.UUID, dict[str, Any]] = {}
    for shot_id, n, spend in session.execute(
        select(
            Activity.shot_id, func.count(), func.coalesce(func.sum(Activity.credits_cost), 0)
        ).group_by(Activity.shot_id)
    ):
        if shot_id:
            stats[shot_id] = {"activities": n, "spend": spend}
    for shot_id, n in session.execute(
        select(ShotVersion.shot_id, func.count()).group_by(ShotVersion.shot_id)
    ):
        stats.setdefault(shot_id, {"activities": 0, "spend": 0})["versions"] = n
    budgets = {
        s.id: s.credit_budget
        for s in session.scalars(select(Shot).where(Shot.project_id == project_id))
    }
    return page(
        request,
        "project.html",
        user,
        tree=tree,
        stats=stats,
        budgets=budgets,
        unassigned=unassigned_count(session),
    )


@router.get("/shots/{shot_id}")
def shot_page(
    shot_id: uuid.UUID,
    request: Request,
    session: DbSession,
    user: WebUser,
    version: uuid.UUID | None = None,
    asset: uuid.UUID | None = None,
) -> HTMLResponse:
    detail = shot_detail(session, shot_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    versions = detail["versions"]
    selected: dict[str, Any] | None = None
    chain = None
    if asset is not None:
        chain = asset_chain(session, asset)
        selected = {"kind": "asset", "id": asset}
    elif versions:
        chosen = next((v for v in versions if v["id"] == version), versions[-1])
        chain = asset_chain(session, chosen["asset_id"])
        selected = {
            "kind": "version",
            "id": chosen["id"],
            "version_number": chosen["version_number"],
        }
    elif detail["activities"]:
        outs = [o["asset_id"] for a in detail["activities"] for o in a["outputs"] if o["asset_id"]]
        chain = union_chain(session, outs) if outs else None
        selected = {"kind": "all"} if chain else None
    project = session.get_one(Shot, shot_id).project_id
    return page(
        request,
        "shot.html",
        user,
        d=detail,
        chain=chain,
        selected=selected,
        project_id=project,
        unassigned=unassigned_count(session),
    )


@router.post("/shots/{shot_id}/promote")
def promote(
    shot_id: uuid.UUID,
    request: Request,
    session: DbSession,
    user: WebUser,
    sha256: Annotated[str, Form()],
    notes: Annotated[str, Form()] = "",
) -> Response:
    same_origin(request)
    if session.get(Shot, shot_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    number = (
        session.scalar(
            select(func.max(ShotVersion.version_number)).where(ShotVersion.shot_id == shot_id)
        )
        or 0
    ) + 1
    version_id = uuid.uuid4()
    emit(
        session,
        user,
        "shot_version.created",
        {
            "version_id": str(version_id),
            "shot_id": str(shot_id),
            "sha256": sha256,
            "version_number": number,
            "notes": notes,
        },
        "asserted",
    )
    session.commit()
    return RedirectResponse(f"/shots/{shot_id}?version={version_id}", status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------- assets


@router.get("/assets/{asset_id}")
def asset_page(
    asset_id: uuid.UUID, request: Request, session: DbSession, user: WebUser, linked: bool = False
) -> HTMLResponse:
    detail = asset_detail(session, asset_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    chain = asset_chain(session, asset_id)
    recent_activities = list(
        session.scalars(select(Activity).order_by(Activity.started_at.desc()).limit(40))
    )
    return page(
        request,
        "asset.html",
        user,
        d=detail,
        chain=chain,
        shots=all_shots(session),
        activities=recent_activities,
        linked=linked,
        unassigned=unassigned_count(session),
    )


@router.post("/assets/{asset_id}/link")
def assert_link(
    asset_id: uuid.UUID,
    request: Request,
    session: DbSession,
    user: WebUser,
    target: Annotated[str, Form()],
    reason: Annotated[str, Form()] = "",
) -> Response:
    """target is "shot:<uuid>" or "activity:<uuid>"."""
    same_origin(request)
    asset = session.get(Asset, asset_id)
    kind, _, tid = target.partition(":")
    if asset is None or kind not in ("shot", "activity"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "pick a shot or an activity")
    target_id = uuid.UUID(tid)
    if session.get(Shot if kind == "shot" else Activity, target_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    emit(
        session,
        user,
        "link.asserted",
        {
            "link_id": str(uuid.uuid4()),
            "sha256": asset.sha256,
            "target_type": kind,
            "target_id": str(target_id),
            "reason": reason or None,
        },
        "asserted",
    )
    session.commit()
    return RedirectResponse(f"/assets/{asset_id}?linked=1", status.HTTP_303_SEE_OTHER)


@router.get("/import")
def import_form(
    request: Request, session: DbSession, user: WebUser, error: str | None = None
) -> HTMLResponse:
    return page(request, "import.html", user, error=error, unassigned=unassigned_count(session))


@router.post("/import")
async def import_file(
    request: Request, session: DbSession, user: WebUser, file: UploadFile
) -> Response:
    """A file made or changed outside the services (a re-encode, a comp, a hand edit).
    Registered with evidence `asserted`: a person put it here. Fingerprinting then proposes
    inferred links to the assets it resembles."""
    same_origin(request)
    data = await file.read()
    sha = hashlib.sha256(data).hexdigest()
    try:
        result = ingest_asset(
            session,
            io.BytesIO(data),
            claimed_sha256=sha,
            media_type=file.content_type,
            locations=[
                {"service": "manual", "url": "", "service_asset_id": file.filename or "upload"}
            ],
            evidence="asserted",
            actor_user_id=user.id,
        )
    except AssetRejected as exc:
        return RedirectResponse(f"/import?error={exc}", status.HTTP_303_SEE_OTHER)
    session.commit()
    return RedirectResponse(f"/assets/{result.asset_id}", status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------- unassigned queue


@router.get("/unassigned")
def unassigned(request: Request, session: DbSession, user: WebUser) -> HTMLResponse:
    rows = []
    names = {u.id: u.display_name for u in session.scalars(select(User))}
    for a in session.scalars(
        select(Activity).where(Activity.shot_id.is_(None)).order_by(Activity.started_at.desc())
    ):
        out = session.execute(
            select(ActivityOutput.asset_id, Asset.sha256, Asset.media_type, Asset.stored)
            .join(Asset, Asset.id == ActivityOutput.asset_id)
            .where(ActivityOutput.activity_id == a.id)
            .order_by(ActivityOutput.index)
            .limit(1)
        ).first()
        rows.append(
            {
                "a": a,
                "by": names.get(a.captured_by_user_id) if a.captured_by_user_id else None,
                "out": out,
            }
        )
    return page(
        request, "unassigned.html", user, rows=rows, shots=all_shots(session), unassigned=len(rows)
    )


@router.post("/unassigned/assign")
async def assign(request: Request, session: DbSession, user: WebUser) -> Response:
    same_origin(request)
    form = await request.form()
    shot_id = uuid.UUID(str(form.get("shot_id")))
    if session.get(Shot, shot_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    shas = sorted({str(v) for v in form.getlist("sha256")})
    for sha in shas:
        emit(
            session,
            user,
            "link.asserted",
            {
                "link_id": str(uuid.uuid4()),
                "sha256": sha,
                "target_type": "shot",
                "target_id": str(shot_id),
                "reason": "assigned in unassigned queue",
            },
            "asserted",
        )
    session.commit()
    return RedirectResponse("/unassigned", status.HTTP_303_SEE_OTHER)


def fmt_json(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


TEMPLATES.env.filters["tojson_sorted"] = fmt_json

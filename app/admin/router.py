"""Server-rendered, tenant-scoped administration dashboard routes."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from app.admin.auth import (
    clear_session_cookie,
    credentials_configured,
    credentials_match,
    read_session,
    require_admin,
    safe_return_to,
    set_session_cookie,
    verify_csrf,
)
from app.api.dependencies import get_api_context
from app.db.protocol import Database
from app.domain.models import (
    AppointmentStatus,
    Clinic,
    ClinicStatus,
    JobStatus,
)

TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_ROOT))
router = APIRouter(tags=["admin"])
protected = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
FormText = Annotated[str, Form()]
FormUUID = Annotated[UUID, Form()]
FormInteger = Annotated[int, Form()]
FormDecimal = Annotated[Decimal, Form()]


def _money(value: Decimal | int | float | str) -> str:
    return f"R{Decimal(str(value)):,.2f}"


def _local_datetime(
    value: datetime | None, timezone_name: str, fmt: str = "%d %b %Y, %H:%M"
) -> str:
    if value is None:
        return "—"
    return value.astimezone(ZoneInfo(timezone_name)).strftime(fmt)


def _date_input(value: date | None) -> str:
    return value.isoformat() if value else ""


templates.env.filters["money"] = _money
templates.env.filters["local_dt"] = _local_datetime
templates.env.filters["date_input"] = _date_input


@router.get("/admin/login", response_class=HTMLResponse)
async def login_page(
    request: Request, next_path: str | None = Query(None, alias="next")
) -> Response:
    """Show the administrator sign-in page."""

    if read_session(request) is not None:
        return RedirectResponse(safe_return_to(next_path), status_code=303)
    settings = get_api_context(request).settings
    return templates.TemplateResponse(
        request,
        "admin/login.html",
        {
            "configured": credentials_configured(settings),
            "next_path": safe_return_to(next_path),
            "error": None,
        },
    )


@router.post("/admin/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: FormText,
    password: FormText,
    next_path: FormText = "/admin",
) -> Response:
    """Validate configured credentials and establish a signed session."""

    settings = get_api_context(request).settings
    if not credentials_match(settings, username, password):
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {
                "configured": credentials_configured(settings),
                "next_path": safe_return_to(next_path),
                "error": "Those credentials did not match our records.",
            },
            status_code=401,
        )
    response = RedirectResponse(safe_return_to(next_path), status_code=303)
    set_session_cookie(response, settings, username)
    return response


@protected.post("/logout")
async def logout(request: Request, csrf_token: FormText) -> Response:
    """End the current administrator session."""

    session = require_admin(request)
    verify_csrf(session, csrf_token)
    response = RedirectResponse("/admin/login", status_code=303)
    clear_session_cookie(response)
    return response


@protected.get("", response_class=HTMLResponse)
async def overview(request: Request, clinic_id: UUID | None = None) -> Response:
    """Render the day sheet and its operational headline figures."""

    database = _database(request)
    context = await _page_context(request, database, clinic_id, "overview")
    clinic = context["clinic"]
    clinics = context["clinics"]
    if not isinstance(clinic, Clinic):
        context.update(
            stats=_empty_stats(),
            today_bookings=[],
            activities=[],
            trial_warnings=[],
        )
        return templates.TemplateResponse(request, "admin/overview.html", context)

    now = _clock_now(request)
    day_start, day_end = _local_day_bounds(now.astimezone(ZoneInfo(clinic.timezone)).date(), clinic)
    today_bookings = await database.list_booking_summaries(clinic.id, day_start, day_end)
    no_shows = await database.admin_list_appointments(
        clinic.id,
        starts_at=now - timedelta(days=30),
        ends_at=now + timedelta(seconds=1),
        status=AppointmentStatus.NO_SHOW,
    )
    failed_jobs = await database.list_jobs(clinic.id, status=JobStatus.FAILED)
    messages = await database.list_message_log(clinic.id, limit=8)
    outbox = await database.list_outbox(clinic.id, limit=8)
    activities: list[dict[str, Any]] = [
        {
            "at": item.created_at,
            "kind": item.channel,
            "title": f"{item.direction.title()} {item.channel}",
            "detail": item.body,
        }
        for item in messages
    ] + [
        {
            "at": item.created_at,
            "kind": item.status.value,
            "title": f"{item.channel.title()} delivery {item.status.value}",
            "detail": _payload_preview(item.payload),
        }
        for item in outbox
    ]
    activities.sort(key=lambda item: item["at"], reverse=True)
    trial_warnings = _trial_warnings(clinics, now)
    active_trials = sum(
        1
        for item in clinics
        if item.status is ClinicStatus.TRIAL
        and item.trial_started_at + timedelta(days=item.trial_days) >= now
    )
    context.update(
        stats=[
            {"value": len(today_bookings), "label": "Today's bookings", "delta": "Live day sheet"},
            {
                "value": sum(
                    1
                    for item in today_bookings
                    if item.appointment.status is AppointmentStatus.CONFIRMED
                ),
                "label": "Confirmed today",
                "delta": "Ready to receive",
            },
            {"value": len(no_shows), "label": "No-shows · 30d", "delta": "Patient follow-up"},
            {"value": active_trials, "label": "Active trials", "delta": "Across clinics"},
            {"value": len(failed_jobs), "label": "Failed jobs", "delta": "Needs attention"},
        ],
        today_bookings=today_bookings,
        activities=activities[:10],
        trial_warnings=trial_warnings,
    )
    return templates.TemplateResponse(request, "admin/overview.html", context)


@protected.get("/appointments", response_class=HTMLResponse)
async def appointments(
    request: Request,
    clinic_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str = "",
    service_id: UUID | None = None,
    search: str = "",
    week: date | None = None,
    view: str = "calendar",
) -> Response:
    """Show searchable appointment lists and a tenant-local weekly strip."""

    database = _database(request)
    context = await _page_context(request, database, clinic_id, "appointments")
    clinic = context["clinic"]
    if not isinstance(clinic, Clinic):
        context.update(bookings=[], services=[], week_days=[], filters={})
        return templates.TemplateResponse(request, "admin/appointments.html", context)

    now = _clock_now(request)
    timezone = ZoneInfo(clinic.timezone)
    local_today = now.astimezone(timezone).date()
    range_start = date_from or local_today - timedelta(days=30)
    range_end = date_to or local_today + timedelta(days=60)
    starts_at, _ = _local_day_bounds(range_start, clinic)
    _, ends_at = _local_day_bounds(range_end, clinic)
    status_value = _appointment_status(status)
    bookings = await database.admin_list_appointments(
        clinic.id,
        starts_at=starts_at,
        ends_at=ends_at,
        status=status_value,
        service_id=service_id,
        search=search or None,
    )
    week_anchor = week or local_today
    week_start = week_anchor - timedelta(days=week_anchor.weekday())
    week_end = week_start + timedelta(days=6)
    week_utc_start, _ = _local_day_bounds(week_start, clinic)
    _, week_utc_end = _local_day_bounds(week_end, clinic)
    week_bookings = await database.admin_list_appointments(
        clinic.id, starts_at=week_utc_start, ends_at=week_utc_end, limit=500
    )
    week_days = []
    for offset in range(7):
        local_day = week_start + timedelta(days=offset)
        week_days.append(
            {
                "date": local_day,
                "is_today": local_day == local_today,
                "bookings": [
                    item
                    for item in week_bookings
                    if item.appointment.starts_at.astimezone(timezone).date() == local_day
                ],
            }
        )
    context.update(
        bookings=bookings,
        services=await database.list_services(clinic.id),
        week_days=week_days,
        week_start=week_start,
        previous_week=week_start - timedelta(days=7),
        next_week=week_start + timedelta(days=7),
        view="list" if view == "list" else "calendar",
        filters={
            "date_from": range_start,
            "date_to": range_end,
            "status": status,
            "service_id": str(service_id) if service_id else "",
            "search": search,
        },
    )
    return templates.TemplateResponse(request, "admin/appointments.html", context)


@protected.get("/appointments/{appointment_id}", response_class=HTMLResponse)
async def appointment_detail(
    request: Request, appointment_id: UUID, clinic_id: UUID | None = None
) -> Response:
    """Show one appointment, its patient, consent, and conversation history."""

    database = _database(request)
    context = await _page_context(request, database, clinic_id, "appointments")
    clinic = _require_selected_clinic(context)
    summary = await database.get_booking_summary(appointment_id)
    if summary is None or summary.appointment.clinic_id != clinic.id:
        raise HTTPException(status_code=404, detail="Appointment not found")
    context.update(
        summary=summary,
        consents=await database.list_patient_consents(clinic.id, appointment_id=appointment_id),
        messages=await database.list_message_log(
            clinic.id, patient_id=summary.patient.id, limit=200
        ),
    )
    return templates.TemplateResponse(request, "admin/appointment_detail.html", context)


@protected.post("/appointments/{appointment_id}/status")
async def appointment_status(
    request: Request,
    appointment_id: UUID,
    clinic_id: FormUUID,
    new_status: FormText,
    csrf_token: FormText,
) -> Response:
    """Apply one allowed administrator appointment status transition."""

    session = require_admin(request)
    verify_csrf(session, csrf_token)
    database = _database(request)
    clinic = await _clinic_owned(database, clinic_id)
    summary = await database.get_booking_summary(appointment_id)
    if summary is None or summary.appointment.clinic_id != clinic.id:
        raise HTTPException(status_code=404, detail="Appointment not found")
    updated = None
    if new_status == AppointmentStatus.CONFIRMED.value:
        updated = await database.transition_appointment_status(
            appointment_id,
            summary.patient.id,
            [AppointmentStatus.BOOKED],
            AppointmentStatus.CONFIRMED,
        )
    elif new_status == AppointmentStatus.CANCELLED.value:
        updated = await database.transition_appointment_status(
            appointment_id,
            summary.patient.id,
            [AppointmentStatus.BOOKED, AppointmentStatus.CONFIRMED],
            AppointmentStatus.CANCELLED,
        )
    elif new_status == AppointmentStatus.NO_SHOW.value:
        updated = await database.mark_no_show(appointment_id)
    else:
        raise HTTPException(status_code=422, detail="Unsupported appointment status")
    message = (
        "Appointment status updated." if updated else "That status change is no longer allowed."
    )
    kind = "success" if updated else "error"
    return _redirect(f"/admin/appointments/{appointment_id}", clinic.id, message, kind)


@protected.get("/patients", response_class=HTMLResponse)
async def patients(request: Request, clinic_id: UUID | None = None, search: str = "") -> Response:
    """Render a searchable patient directory."""

    database = _database(request)
    context = await _page_context(request, database, clinic_id, "patients")
    clinic = context["clinic"]
    if not isinstance(clinic, Clinic):
        context.update(patients=[], booking_counts={}, search=search)
    else:
        patient_rows = await database.list_patients(clinic.id, search or None)
        bookings = await database.admin_list_appointments(clinic.id, limit=2000)
        counts: dict[UUID, int] = {}
        for item in bookings:
            counts[item.patient.id] = counts.get(item.patient.id, 0) + 1
        context.update(patients=patient_rows, booking_counts=counts, search=search)
    return templates.TemplateResponse(request, "admin/patients.html", context)


@protected.get("/patients/{patient_id}", response_class=HTMLResponse)
async def patient_detail(
    request: Request, patient_id: UUID, clinic_id: UUID | None = None
) -> Response:
    """Show a patient profile with bookings, consent, and message history."""

    database = _database(request)
    context = await _page_context(request, database, clinic_id, "patients")
    clinic = _require_selected_clinic(context)
    patient = await database.get_patient(patient_id)
    if patient is None or patient.clinic_id != clinic.id:
        raise HTTPException(status_code=404, detail="Patient not found")
    context.update(
        patient=patient,
        bookings=await database.admin_list_appointments(
            clinic.id, patient_id=patient.id, limit=500
        ),
        messages=await database.list_message_log(clinic.id, patient_id=patient.id, limit=500),
        consents=await database.list_patient_consents(clinic.id, patient_id=patient.id),
    )
    return templates.TemplateResponse(request, "admin/patient_detail.html", context)


@protected.get("/services", response_class=HTMLResponse)
async def services(request: Request, clinic_id: UUID | None = None) -> Response:
    """Render the service catalogue editor."""

    database = _database(request)
    context = await _page_context(request, database, clinic_id, "services")
    clinic = context["clinic"]
    service_rows = await database.list_services(clinic.id) if isinstance(clinic, Clinic) else []
    context.update(services=service_rows)
    return templates.TemplateResponse(request, "admin/services.html", context)


@protected.post("/services")
async def create_service(
    request: Request,
    clinic_id: FormUUID,
    name: FormText,
    duration_min: FormInteger,
    price: FormDecimal,
    csrf_token: FormText,
) -> Response:
    """Create a service for the selected clinic."""

    verify_csrf(require_admin(request), csrf_token)
    database = _database(request)
    await _clinic_owned(database, clinic_id)
    if not name.strip() or duration_min <= 0 or price < 0:
        return _redirect(
            "/admin/services", clinic_id, "Enter a valid service, duration, and price.", "error"
        )
    await database.create_service(
        {
            "id": uuid4(),
            "clinic_id": clinic_id,
            "name": name.strip(),
            "duration_min": duration_min,
            "price": price,
        }
    )
    return _redirect("/admin/services", clinic_id, "Service created.")


@protected.post("/services/{service_id}/edit")
async def edit_service(
    request: Request,
    service_id: UUID,
    clinic_id: FormUUID,
    name: FormText,
    duration_min: FormInteger,
    price: FormDecimal,
    csrf_token: FormText,
) -> Response:
    """Update an existing tenant service."""

    verify_csrf(require_admin(request), csrf_token)
    database = _database(request)
    service = await database.get_service(service_id)
    if service is None or service.clinic_id != clinic_id:
        raise HTTPException(status_code=404, detail="Service not found")
    if not name.strip() or duration_min <= 0 or price < 0:
        return _redirect("/admin/services", clinic_id, "Enter valid service values.", "error")
    await database.update_service(
        service_id,
        {"name": name.strip(), "duration_min": duration_min, "price": price},
    )
    return _redirect("/admin/services", clinic_id, "Service updated.")


@protected.post("/services/{service_id}/delete")
async def delete_service(
    request: Request,
    service_id: UUID,
    clinic_id: FormUUID,
    csrf_token: FormText,
) -> Response:
    """Delete a service unless an open future booking still uses it."""

    verify_csrf(require_admin(request), csrf_token)
    database = _database(request)
    service = await database.get_service(service_id)
    if service is None or service.clinic_id != clinic_id:
        raise HTTPException(status_code=404, detail="Service not found")
    if await database.service_has_future_bookings(service_id, _clock_now(request)):
        return _redirect(
            "/admin/services",
            clinic_id,
            "This service has future bookings and cannot be deleted.",
            "error",
        )
    await database.delete_service(service_id)
    return _redirect("/admin/services", clinic_id, "Service deleted.")


@protected.get("/clinics", response_class=HTMLResponse)
async def clinics(request: Request, clinic_id: UUID | None = None) -> Response:
    """Show the multi-tenant clinic register."""

    database = _database(request)
    context = await _page_context(request, database, clinic_id, "clinics")
    now = _clock_now(request)
    context.update(
        now=now,
        trial_countdowns={
            item.id: max(
                0,
                (item.trial_started_at + timedelta(days=item.trial_days) - now).days,
            )
            for item in context["clinics"]
            if isinstance(item, Clinic) and item.status is ClinicStatus.TRIAL
        },
    )
    return templates.TemplateResponse(request, "admin/clinics.html", context)


@protected.get("/clinics/new", response_class=HTMLResponse)
async def new_clinic(request: Request, clinic_id: UUID | None = None) -> Response:
    """Render no-code clinic onboarding."""

    context = await _page_context(request, _database(request), clinic_id, "clinics")
    context.update(editing=None, services=[], webhook=None, last_sync=None)
    return templates.TemplateResponse(request, "admin/clinic_form.html", context)


@protected.post("/clinics/new")
async def create_clinic(request: Request) -> Response:
    """Create a clinic and its inline starter services."""

    form = await request.form()
    verify_csrf(require_admin(request), _form_text(form, "csrf_token"))
    database = _database(request)
    try:
        values = _clinic_form_values(form, _clock_now(request), creating=True)
    except ValueError as exc:
        return _redirect("/admin/clinics/new", None, str(exc), "error")
    clinic = await database.create_clinic(values)
    names = [_form_item(value) for value in form.getlist("service_name")]
    durations = [_form_item(value) for value in form.getlist("service_duration")]
    prices = [_form_item(value) for value in form.getlist("service_price")]
    for index, name in enumerate(names):
        if not name.strip():
            continue
        duration = _safe_int(durations[index] if index < len(durations) else "30", 30)
        price = _safe_decimal(prices[index] if index < len(prices) else "0")
        await database.create_service(
            {
                "id": uuid4(),
                "clinic_id": clinic.id,
                "name": name.strip(),
                "duration_min": max(duration, 1),
                "price": max(price, Decimal(0)),
            }
        )
    return _redirect(f"/admin/clinics/{clinic.id}", clinic.id, "Clinic onboarded.")


@protected.get("/clinics/{editing_id}", response_class=HTMLResponse)
async def clinic_detail(
    request: Request, editing_id: UUID, clinic_id: UUID | None = None
) -> Response:
    """Render clinic configuration and integration health."""

    database = _database(request)
    context = await _page_context(request, database, clinic_id or editing_id, "clinics")
    editing = await database.get_clinic(editing_id)
    if editing is None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    events = await database.list_webhook_events(editing.id, limit=1)
    bookings = await database.admin_list_appointments(editing.id, limit=200)
    synced = next((item for item in bookings if item.appointment.google_event_id), None)
    context.update(
        editing=editing,
        services=await database.list_services(editing.id),
        webhook=events[0] if events else None,
        last_sync=synced.appointment if synced else None,
        google_configured=bool(
            get_api_context(request).settings.google_client_id
            and get_api_context(request).settings.google_client_secret
            and get_api_context(request).settings.google_refresh_token
        ),
    )
    return templates.TemplateResponse(request, "admin/clinic_form.html", context)


@protected.post("/clinics/{editing_id}")
async def update_clinic(request: Request, editing_id: UUID) -> Response:
    """Save editable clinic configuration."""

    form = await request.form()
    verify_csrf(require_admin(request), _form_text(form, "csrf_token"))
    database = _database(request)
    await _clinic_owned(database, editing_id)
    try:
        values = _clinic_form_values(form, _clock_now(request), creating=False)
    except ValueError as exc:
        return _redirect(f"/admin/clinics/{editing_id}", editing_id, str(exc), "error")
    await database.update_clinic(editing_id, values)
    return _redirect(f"/admin/clinics/{editing_id}", editing_id, "Clinic settings saved.")


@protected.post("/clinics/{editing_id}/test-alert")
async def clinic_test_alert(
    request: Request,
    editing_id: UUID,
    csrf_token: FormText,
) -> Response:
    """Queue a Telegram test message through the existing durable outbox."""

    verify_csrf(require_admin(request), csrf_token)
    database = _database(request)
    clinic = await _clinic_owned(database, editing_id)
    if not clinic.telegram_chat_id:
        return _redirect(
            f"/admin/clinics/{editing_id}", editing_id, "Add a Telegram chat ID first.", "error"
        )
    await database.enqueue_outbox(
        clinic.id,
        "telegram",
        clinic.telegram_chat_id,
        {"text": f"BookaBL test alert for {clinic.name}. Your admin connection is ready."},
    )
    return _redirect(f"/admin/clinics/{editing_id}", editing_id, "Test alert queued.")


@protected.get("/ops", response_class=HTMLResponse)
async def operations(
    request: Request, clinic_id: UUID | None = None, tab: str = "outbox"
) -> Response:
    """Render outbox, automation, webhook, and no-show operations."""

    database = _database(request)
    context = await _page_context(request, database, clinic_id, "ops")
    clinic = context["clinic"]
    if isinstance(clinic, Clinic):
        no_shows = await database.admin_list_appointments(
            clinic.id, status=AppointmentStatus.NO_SHOW, limit=500
        )
        context.update(
            outbox=await database.list_outbox(clinic.id),
            jobs=await database.list_jobs(clinic.id),
            events=await database.list_webhook_events(clinic.id),
            no_shows=no_shows,
        )
    else:
        context.update(outbox=[], jobs=[], events=[], no_shows=[])
    context["tab"] = tab if tab in {"outbox", "jobs", "webhooks", "no-shows"} else "outbox"
    return templates.TemplateResponse(request, "admin/ops.html", context)


@protected.post("/ops/outbox/{outbox_id}/retry")
async def retry_outbox(
    request: Request,
    outbox_id: UUID,
    clinic_id: FormUUID,
    csrf_token: FormText,
) -> Response:
    """Move one tenant-owned outbox record back to pending now."""

    verify_csrf(require_admin(request), csrf_token)
    database = _database(request)
    item = await database.get_outbox(outbox_id)
    if item is None or item.clinic_id != clinic_id:
        raise HTTPException(status_code=404, detail="Outbox item not found")
    await database.retry_outbox(outbox_id, _clock_now(request), "Manual retry", failed=False)
    return _redirect(
        "/admin/ops", clinic_id, "Outbox item queued for retry.", extra={"tab": "outbox"}
    )


@protected.post("/ops/jobs/{job_id}/retry")
async def retry_job(
    request: Request,
    job_id: UUID,
    clinic_id: FormUUID,
    csrf_token: FormText,
) -> Response:
    """Move one tenant-owned automation job back to pending now."""

    verify_csrf(require_admin(request), csrf_token)
    database = _database(request)
    item = await database.get_job(job_id)
    if item is None or item.clinic_id != clinic_id:
        raise HTTPException(status_code=404, detail="Automation job not found")
    await database.retry_job(job_id, _clock_now(request), "Manual retry", failed=False)
    return _redirect("/admin/ops", clinic_id, "Job queued for retry.", extra={"tab": "jobs"})


router.include_router(protected)


def _database(request: Request) -> Database:
    database = get_api_context(request).database
    if database is None:
        raise HTTPException(status_code=503, detail="Admin database is unavailable")
    return database


async def _page_context(
    request: Request,
    database: Database,
    clinic_id: UUID | None,
    active_nav: str,
) -> dict[str, Any]:
    session = require_admin(request)
    clinics = await database.list_clinics()
    selected = next((clinic for clinic in clinics if clinic.id == clinic_id), None)
    if selected is None and clinics:
        selected = clinics[0]
    return {
        "request": request,
        "clinics": clinics,
        "clinic": selected,
        "clinic_id": str(selected.id) if selected else "",
        "active_nav": active_nav,
        "csrf_token": str(session["csrf"]),
        "notice": request.query_params.get("notice"),
        "notice_kind": request.query_params.get("kind", "success"),
        "admin_name": str(session["username"]),
    }


def _require_selected_clinic(context: dict[str, Any]) -> Clinic:
    clinic = context.get("clinic")
    if not isinstance(clinic, Clinic):
        raise HTTPException(status_code=404, detail="No clinic is configured")
    return clinic


async def _clinic_owned(database: Database, clinic_id: UUID) -> Clinic:
    clinic = await database.get_clinic(clinic_id)
    if clinic is None:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return clinic


def _clock_now(request: Request) -> datetime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is not None:
        value = runtime.clock.now()
        if isinstance(value, datetime):
            return value
    return datetime.now(tz=UTC)


def _local_day_bounds(local_day: date, clinic: Clinic) -> tuple[datetime, datetime]:
    timezone = ZoneInfo(clinic.timezone)
    start = datetime.combine(local_day, time.min, timezone)
    end = datetime.combine(local_day + timedelta(days=1), time.min, timezone)
    return start.astimezone(UTC), end.astimezone(UTC)


def _appointment_status(value: str) -> AppointmentStatus | None:
    try:
        return AppointmentStatus(value) if value else None
    except ValueError:
        return None


def _payload_preview(payload: dict[str, Any]) -> str:
    for key in ("text", "body", "template_name"):
        if key in payload:
            return str(payload[key])
    return "Queued provider payload"


def _trial_warnings(clinics: list[Clinic], now: datetime) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for clinic in clinics:
        if clinic.status is not ClinicStatus.TRIAL:
            continue
        ends_at = clinic.trial_started_at + timedelta(days=clinic.trial_days)
        remaining = ends_at - now
        if timedelta(0) <= remaining <= timedelta(days=3):
            warnings.append(
                {
                    "clinic": clinic,
                    "ends_at": ends_at,
                    "days": max(0, remaining.days),
                }
            )
    return sorted(warnings, key=lambda item: item["ends_at"])


def _empty_stats() -> list[dict[str, Any]]:
    return [
        {"value": 0, "label": label, "delta": "Awaiting clinic setup"}
        for label in (
            "Today's bookings",
            "Confirmed today",
            "No-shows · 30d",
            "Active trials",
            "Failed jobs",
        )
    ]


def _redirect(
    path: str,
    clinic_id: UUID | None,
    notice: str,
    kind: str = "success",
    *,
    extra: dict[str, str] | None = None,
) -> RedirectResponse:
    query = {"notice": notice, "kind": kind}
    if clinic_id is not None:
        query["clinic_id"] = str(clinic_id)
    if extra:
        query.update(extra)
    return RedirectResponse(f"{path}?{urlencode(query)}", status_code=303)


def _clinic_form_values(form: FormData, now: datetime, *, creating: bool) -> dict[str, Any]:
    try:
        timezone_name = _form_text(form, "timezone") or "Africa/Johannesburg"
        ZoneInfo(timezone_name)
        work_start = time.fromisoformat(_form_text(form, "work_start"))
        work_end = time.fromisoformat(_form_text(form, "work_end"))
        if work_end <= work_start:
            raise ValueError("Closing time must be later than opening time.")
        work_days = sorted(
            {
                int(_form_item(value))
                for value in form.getlist("work_days")
                if _form_item(value).isdigit() and 1 <= int(_form_item(value)) <= 7
            }
        )
        if not work_days:
            raise ValueError("Choose at least one working day.")
        reminder_offsets = [
            int(value.strip())
            for value in _form_text(form, "reminder_offsets_h").split(",")
            if value.strip()
        ]
        if any(value < 0 for value in reminder_offsets):
            raise ValueError("Reminder offsets cannot be negative.")
        status = ClinicStatus(_form_text(form, "status"))
        values: dict[str, Any] = {
            "name": _required(form, "name", "Clinic name"),
            "industry": _form_text(form, "industry") or "dental",
            "package": _form_text(form, "package") or "starter",
            "status": status,
            "trial_days": max(int(_form_text(form, "trial_days") or "0"), 0),
            "monthly_fee": max(_safe_decimal(_form_text(form, "monthly_fee")), Decimal(0)),
            "wa_phone_id": _required(form, "wa_phone_id", "WhatsApp phone ID"),
            "telegram_chat_id": _optional(_form_text(form, "telegram_chat_id")),
            "google_calendar_id": _optional(_form_text(form, "google_calendar_id")),
            "google_review_url": _optional(_form_text(form, "google_review_url")),
            "timezone": timezone_name,
            "work_start": work_start,
            "work_end": work_end,
            "work_days": work_days,
            "reminder_offsets_h": reminder_offsets,
            "brand_voice": _optional(_form_text(form, "brand_voice")),
        }
    except (InvalidOperation, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(f"Please review the clinic fields: {exc}") from exc
    if creating:
        values.update(id=uuid4(), trial_started_at=now, created_at=now)
    return values


def _required(form: FormData, key: str, label: str) -> str:
    value = _form_text(form, key).strip()
    if not value:
        raise ValueError(f"{label} is required.")
    return value


def _form_text(form: FormData, key: str) -> str:
    return _form_item(form.get(key, ""))


def _form_item(value: object) -> str:
    return value if isinstance(value, str) else ""


def _safe_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except ValueError:
        return fallback


def _safe_decimal(value: str) -> Decimal:
    try:
        return Decimal(value or "0")
    except InvalidOperation:
        return Decimal(0)


def _optional(value: str) -> str | None:
    stripped = value.strip()
    return stripped or None

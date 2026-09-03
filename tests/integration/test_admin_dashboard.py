from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from app.api.dependencies import ApiContext
from app.core.clock import FrozenClock
from app.core.config import Settings
from app.db.memory import InMemoryDatabase
from app.domain.models import (
    Clinic,
    ClinicStatus,
    FAQEntry,
    FinalizeBookingCommand,
    PatientConsent,
    Service,
)
from app.main import create_app
from app.services.whatsapp_ingress import WhatsAppIngress
from pydantic import SecretStr

NOW = datetime(2026, 8, 17, 8, tzinfo=UTC)
CLINIC_ID = UUID("00000000-0000-4000-8000-000000000001")
SERVICE_ID = UUID("00000000-0000-4000-8000-000000000101")


async def build_admin_api() -> tuple[object, InMemoryDatabase, UUID, UUID]:
    database = InMemoryDatabase(FrozenClock(NOW))
    clinic = Clinic(
        id=CLINIC_ID,
        name="Heritage Dental",
        status=ClinicStatus.ACTIVE,
        trial_started_at=NOW,
        wa_phone_id="phone-1",
        telegram_chat_id="owner-chat",
        timezone="Africa/Johannesburg",
        work_start=time(8),
        work_end=time(17),
        created_at=NOW,
    )
    database.add_clinic(clinic)
    database.add_service(
        Service(
            id=SERVICE_ID,
            clinic_id=CLINIC_ID,
            name="Cleaning",
            duration_min=30,
            price=Decimal("850"),
        )
    )
    patient = await database.get_or_create_patient(CLINIC_ID, "27820000000", "John")
    appointment = await database.finalize_booking(
        FinalizeBookingCommand(
            clinic_id=CLINIC_ID,
            patient_id=patient.id,
            service_id=SERVICE_ID,
            starts_at=NOW + timedelta(hours=2),
            ends_at=NOW + timedelta(hours=2, minutes=30),
            medical_aid_name="Discovery Health",
            medical_aid_number="1234567",
            dependent_code="01",
            whatsapp_to=patient.wa_number,
            whatsapp_payload={"kind": "text", "text": "Booked"},
            telegram_to=clinic.telegram_chat_id,
            telegram_payload={"text": "New booking"},
        )
    )
    consent = PatientConsent(
        id=uuid4(),
        clinic_id=CLINIC_ID,
        patient_id=patient.id,
        appointment_id=appointment.id,
        consent_type="POPIA",
        consent_text="Patient agreed to appointment communication.",
        consent_version="1.0",
        consented_at=NOW,
    )
    database.consents[consent.id] = consent
    faq = FAQEntry(
        id=uuid4(),
        clinic_id=CLINIC_ID,
        question="Do you accept medical aid?",
        answer="Yes. Bring your membership details.",
        category="Payments",
        active=True,
        created_at=NOW,
    )
    database.faq_entries[faq.id] = faq
    settings = Settings(
        _env_file=None,
        app_env="dev",
        admin_username=SecretStr("admin"),
        admin_password=SecretStr("heritage-secret"),
    )
    context = ApiContext(
        settings=settings,
        whatsapp_ingress=WhatsAppIngress(database),
        database=database,
    )
    return create_app(context), database, patient.id, appointment.id


@pytest.mark.asyncio
async def test_admin_login_and_every_page_renders() -> None:
    app, _database, patient_id, appointment_id = await build_admin_api()
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        anonymous = await client.get("/admin")
        assert anonymous.status_code == 303
        assert anonymous.headers["location"].startswith("/admin/login")

        login_page = await client.get("/admin/login")
        assert login_page.status_code == 200
        assert "BookaBL" in login_page.text
        login = await client.post(
            "/admin/login",
            data={"username": "admin", "password": "heritage-secret"},
        )
        assert login.status_code == 303
        assert "HttpOnly" in login.headers["set-cookie"]

        scoped = f"clinic_id={CLINIC_ID}"
        paths = [
            f"/admin?{scoped}",
            f"/admin/appointments?{scoped}",
            f"/admin/appointments?{scoped}&view=list",
            f"/admin/appointments/{appointment_id}?{scoped}",
            f"/admin/patients?{scoped}",
            f"/admin/patients/{patient_id}?{scoped}",
            f"/admin/services?{scoped}",
            f"/admin/clinics?{scoped}",
            f"/admin/clinics/new?{scoped}",
            f"/admin/clinics/{CLINIC_ID}?{scoped}",
            f"/admin/ops?{scoped}&tab=outbox",
            f"/admin/ops?{scoped}&tab=jobs",
            f"/admin/ops?{scoped}&tab=webhooks",
            f"/admin/ops?{scoped}&tab=no-shows",
            f"/admin/faq?{scoped}",
        ]
        for path in paths:
            response = await client.get(path)
            assert response.status_code == 200, path
            assert "BookaBL" in response.text, path
            assert response.headers["cache-control"] == "no-store", path
        stylesheet = await client.get("/static/admin.css")
        assert stylesheet.status_code == 200
        assert "--teal: #0e5a4a" in stylesheet.text


@pytest.mark.asyncio
async def test_admin_empty_clinic_pages_are_inviting() -> None:
    database = InMemoryDatabase(FrozenClock(NOW))
    settings = Settings(
        _env_file=None,
        admin_username=SecretStr("admin"),
        admin_password=SecretStr("heritage-secret"),
    )
    app = create_app(
        ApiContext(
            settings=settings,
            whatsapp_ingress=WhatsAppIngress(database),
            database=database,
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/admin/login",
            data={"username": "admin", "password": "heritage-secret"},
        )
        response = await client.get("/admin/clinics")
    assert response.status_code == 200
    assert "Your first practice starts here" in response.text

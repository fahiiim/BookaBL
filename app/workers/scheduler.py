"""Reminder, no-show, and calendar-retry automation job scheduler."""

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.adapters.calendar import CalendarProvider
from app.core.clock import Clock
from app.db.protocol import Database
from app.domain.models import AppointmentStatus, AutomationJob
from app.services.notifications import NotificationFormatter

logger = logging.getLogger(__name__)
JOB_BACKOFF_SECONDS = (30, 120, 600, 3600)


class Scheduler:
    """Claim due jobs and apply idempotent domain actions."""

    def __init__(
        self,
        database: Database,
        calendar: CalendarProvider,
        notifications: NotificationFormatter,
        clock: Clock,
        *,
        batch_size: int = 20,
        poll_seconds: float = 1,
    ) -> None:
        self._database = database
        self._calendar = calendar
        self._notifications = notifications
        self._clock = clock
        self._batch_size = batch_size
        self._poll_seconds = poll_seconds

    async def run_once(self) -> int:
        """Process one due-job batch and return the number of claimed jobs."""

        jobs = await self._database.pop_due_jobs(self._batch_size)
        for job in jobs:
            try:
                await self._dispatch(job)
                await self._database.complete_job(job.id)
            except Exception as exc:
                logger.exception("automation_job_failed")
                failed = job.attempts >= 5
                delay = (
                    timedelta(0)
                    if failed
                    else timedelta(seconds=JOB_BACKOFF_SECONDS[job.attempts - 1])
                )
                await self._database.retry_job(
                    job.id,
                    self._clock.now() + delay,
                    str(exc),
                    failed=failed,
                )
        return len(jobs)

    async def _dispatch(self, job: AutomationJob) -> None:
        if job.job_type == "reminder":
            await self._send_reminder(job)
        elif job.job_type == "no_show_check":
            await self._tag_no_show(job)
        elif job.job_type == "calendar_retry":
            await self._retry_calendar(job)
        else:
            raise ValueError(f"Unknown automation job type: {job.job_type}")

    async def _send_reminder(self, job: AutomationJob) -> None:
        if job.appointment_id is None:
            raise ValueError("Reminder job has no appointment")
        summary = await self._database.get_booking_summary(job.appointment_id)
        if summary is None:
            return
        appointment = summary.appointment
        if appointment.status not in {
            AppointmentStatus.BOOKED,
            AppointmentStatus.CONFIRMED,
        } or appointment.starts_at <= self._clock.now():
            return
        clinic = await self._database.get_clinic(job.clinic_id)
        if clinic is None:
            return
        actions = [
            {"id": f"confirm:{appointment.id}", "title": "Confirm"},
            {"id": f"reschedule:{appointment.id}", "title": "Reschedule"},
            {"id": f"cancel:{appointment.id}", "title": "Cancel"},
        ]
        template = self._reminder_template(clinic.wa_templates)
        payload: dict[str, Any]
        if template:
            payload = {
                "kind": "template",
                "template_name": template[0],
                "language_code": template[1],
                "button_payloads": [action["id"] for action in actions],
            }
        else:
            local_time = appointment.starts_at.astimezone(ZoneInfo(clinic.timezone))
            payload = {
                "kind": "buttons",
                "body": (
                    f"Reminder: your {summary.service.name} appointment is "
                    f"{local_time:%a %d %b at %H:%M}."
                ),
                "buttons": actions,
            }
        await self._database.enqueue_outbox(
            clinic.id, "whatsapp", summary.patient.wa_number, payload
        )

    async def _tag_no_show(self, job: AutomationJob) -> None:
        if job.appointment_id is None:
            raise ValueError("No-show job has no appointment")
        updated = await self._database.mark_no_show(job.appointment_id)
        if updated is None:
            return
        summary = await self._database.get_booking_summary(updated.id)
        clinic = await self._database.get_clinic(job.clinic_id)
        if summary and clinic and clinic.telegram_chat_id:
            await self._database.enqueue_outbox(
                clinic.id,
                "telegram",
                clinic.telegram_chat_id,
                {
                    "text": self._notifications.owner_status_change(
                        "No-show",
                        clinic,
                        summary.patient,
                        summary.service,
                        summary.appointment,
                    )
                },
            )

    async def _retry_calendar(self, job: AutomationJob) -> None:
        if job.appointment_id is None:
            raise ValueError("Calendar retry job has no appointment")
        summary = await self._database.get_booking_summary(job.appointment_id)
        clinic = await self._database.get_clinic(job.clinic_id)
        if summary is None or clinic is None or summary.appointment.google_event_id:
            return
        if summary.appointment.status is AppointmentStatus.CANCELLED:
            return
        event_id = await self._calendar.create_event(
            clinic,
            summary.service.name,
            summary.patient.name,
            summary.appointment.starts_at,
            summary.appointment.ends_at,
        )
        await self._database.set_google_event_id(summary.appointment.id, event_id)

    @staticmethod
    def _reminder_template(templates: dict[str, Any]) -> tuple[str, str] | None:
        configured = templates.get("reminder")
        if isinstance(configured, str) and configured:
            return configured, "en"
        if isinstance(configured, dict) and configured.get("name"):
            return str(configured["name"]), str(configured.get("language", "en"))
        return None

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Poll until ``stop`` is set."""

        while not stop.is_set():
            processed = await self.run_once()
            if processed == 0:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)

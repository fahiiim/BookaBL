"""Commercial access gate for trial and inactive clinics."""

from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from app.core.clock import Clock
from app.domain.models import Clinic, ClinicStatus


@dataclass(frozen=True, slots=True)
class TrialDecision:
    """Result of evaluating whether a clinic may serve patient flows."""

    blocked: bool
    reason: str | None = None


class TrialGate:
    """Evaluate config-driven clinic access using the injected clock."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def evaluate(self, clinic: Clinic) -> TrialDecision:
        """Block expired trials and clinics not in an operating status."""

        if clinic.status is ClinicStatus.ACTIVE:
            return TrialDecision(blocked=False)
        if clinic.status is ClinicStatus.TRIAL:
            trial_ends_at = clinic.trial_started_at + timedelta(days=clinic.trial_days)
            if self._clock.now() <= trial_ends_at:
                return TrialDecision(blocked=False)
            return TrialDecision(blocked=True, reason="trial_expired")
        return TrialDecision(blocked=True, reason=f"clinic_{clinic.status.value}")

    def local_date(self, clinic: Clinic) -> date:
        """Return the clinic-local date used for once-daily throttling."""

        return self._clock.now().astimezone(ZoneInfo(clinic.timezone)).date()

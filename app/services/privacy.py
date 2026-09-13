"""Privacy-preserving display helpers for external provider messages."""

import re
from datetime import date, datetime

MONTH_LABELS = (
    "Jan.",
    "Feb.",
    "Mar.",
    "Apr.",
    "May",
    "Jun.",
    "Jul.",
    "Aug.",
    "Sept.",
    "Oct.",
    "Nov.",
    "Dec.",
)


def minimal_patient_name(full_name: str) -> str:
    """Return a first name plus initials for every remaining name component."""

    parts = [part for part in full_name.strip().split() if part]
    if not parts:
        return "Patient"
    initials = "".join(_initial(part) for part in parts[1:])
    return f"{parts[0]} {initials}".strip()


def short_date(value: date | datetime) -> str:
    """Return the compact explicit date style requested for external messages."""

    return f"{value.day:02d} {MONTH_LABELS[value.month - 1]}"


def _initial(value: str) -> str:
    cleaned = re.sub(r"[^\w]", "", value, flags=re.UNICODE)
    return cleaned[0].upper() if cleaned else ""

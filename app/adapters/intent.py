"""Intent-classification port with structured OpenAI output and safe fallback."""

import logging
from enum import StrEnum
from typing import Protocol

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class IntentKind(StrEnum):
    """Small set of intents the deterministic state machine may accept."""

    BOOK = "book"
    GREETING = "greeting"
    CONFIRM = "confirm"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    OTHER = "other"


class IntentResult(BaseModel):
    """Strict structured output returned by an intent classifier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: IntentKind
    confidence: float = Field(ge=0, le=1)


class IntentModel(Protocol):
    """Classify a patient utterance without controlling flow transitions."""

    async def classify(self, text: str) -> IntentResult:
        """Return one supported intent with a bounded confidence score."""


class KeywordIntent:
    """Deterministic intent classifier used directly and as the LLM fallback."""

    async def classify(self, text: str) -> IntentResult:
        normalized = text.casefold().strip()
        if any(word in normalized for word in ("reschedule", "move", "change booking")):
            return IntentResult(intent=IntentKind.RESCHEDULE, confidence=0.9)
        if any(word in normalized for word in ("cancel", "call off")):
            return IntentResult(intent=IntentKind.CANCEL, confidence=0.9)
        if normalized in {"confirm", "confirmed", "yes", "yes confirm"}:
            return IntentResult(intent=IntentKind.CONFIRM, confidence=0.9)
        if any(word in normalized for word in ("book", "appointment", "dentist")):
            return IntentResult(intent=IntentKind.BOOK, confidence=0.9)
        if any(word in normalized for word in ("hello", "hi", "hey", "good morning")):
            return IntentResult(intent=IntentKind.GREETING, confidence=0.8)
        return IntentResult(intent=IntentKind.OTHER, confidence=0.5)


class OpenAIIntent:
    """Structured-output intent model with fallback on every model or transport error."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        client: AsyncOpenAI | None = None,
        fallback: IntentModel | None = None,
    ) -> None:
        self._client = client or AsyncOpenAI(api_key=api_key)
        self._model = model
        self._fallback = fallback or KeywordIntent()

    async def classify(self, text: str) -> IntentResult:
        try:
            response = await self._client.responses.parse(
                model=self._model,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "Classify the patient's intent. Propose only an intent; "
                            "the deterministic booking state machine owns all transitions."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                text_format=IntentResult,
            )
            parsed = response.output_parsed
            if parsed is None:
                raise ValueError("OpenAI returned no parsed intent")
            return parsed
        except Exception as exc:
            logger.warning("intent_model_fallback", exc_info=exc)
            return await self._fallback.classify(text)


class FakeIntent:
    """Configurable intent model used by flow tests."""

    def __init__(self, result: IntentResult | None = None) -> None:
        self.result = result or IntentResult(intent=IntentKind.BOOK, confidence=1)
        self.inputs: list[str] = []

    async def classify(self, text: str) -> IntentResult:
        self.inputs.append(text)
        return self.result


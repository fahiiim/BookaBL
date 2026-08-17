"""Pure booking-conversation transition rules."""

from typing import ClassVar

from app.core.exceptions import InvalidTransitionError
from app.domain.models import ConversationStep


class ConversationTransitions:
    """Validate state changes while leaving input interpretation to the flow."""

    _allowed: ClassVar[dict[ConversationStep, frozenset[ConversationStep]]] = {
        ConversationStep.IDLE: frozenset(
            {ConversationStep.AWAIT_SERVICE, ConversationStep.AWAIT_SLOT}
        ),
        ConversationStep.AWAIT_SERVICE: frozenset(
            {ConversationStep.AWAIT_SERVICE, ConversationStep.AWAIT_SLOT, ConversationStep.IDLE}
        ),
        ConversationStep.AWAIT_SLOT: frozenset(
            {ConversationStep.AWAIT_SLOT, ConversationStep.AWAIT_MA_NAME, ConversationStep.IDLE}
        ),
        ConversationStep.AWAIT_MA_NAME: frozenset(
            {
                ConversationStep.AWAIT_MA_NAME,
                ConversationStep.AWAIT_MA_NUMBER,
                ConversationStep.IDLE,
            }
        ),
        ConversationStep.AWAIT_MA_NUMBER: frozenset(
            {
                ConversationStep.AWAIT_MA_NUMBER,
                ConversationStep.AWAIT_MA_DEPENDENT,
                ConversationStep.IDLE,
            }
        ),
        ConversationStep.AWAIT_MA_DEPENDENT: frozenset(
            {ConversationStep.AWAIT_MA_DEPENDENT, ConversationStep.IDLE}
        ),
    }

    @classmethod
    def validate(
        cls, current: ConversationStep, target: ConversationStep
    ) -> ConversationStep:
        """Return ``target`` when allowed, otherwise raise a typed domain error."""

        if target not in cls._allowed[current]:
            raise InvalidTransitionError(f"Cannot transition from {current} to {target}")
        return target

import random
from uuid import UUID

from infra.providers.types import (
    ProviderOutcome,
    ProviderResult,
)

# Deterministic control markers for demos and tests. Put one anywhere in the
# message body to force a provider outcome regardless of the random fail rate.
PERMANENT_MARKER = "[FAIL_PERMANENT]"
TEMPORARY_MARKER = "[FAIL_TEMP]"


class MockSmsProvider:
    """Provider that can simulate failures so the retry/refund paths are
    exercisable without a real telecom integration.

    Outcome is decided in this order:

    1. Body contains ``[FAIL_PERMANENT]`` -> permanent failure (no retry).
    2. Body contains ``[FAIL_TEMP]``      -> temporary failure (retried).
    3. Otherwise a temporary failure with probability ``fail_rate``.
    4. Otherwise success.
    """

    def __init__(self, *, fail_rate: float = 0.0):
        self.fail_rate = fail_rate

    def send(
        self,
        *,
        message_id: UUID,
        recipient: str,  # noqa: ARG002
        body: str,
    ) -> ProviderResult:
        if PERMANENT_MARKER in body:
            return ProviderResult(
                outcome=ProviderOutcome.PERMANENT_FAILURE,
                error="mock: forced permanent failure",
            )

        if TEMPORARY_MARKER in body:
            return ProviderResult(
                outcome=ProviderOutcome.TEMPORARY_FAILURE,
                error="mock: forced temporary failure",
            )

        if self.fail_rate > 0.0 and random.random() < self.fail_rate:
            return ProviderResult(
                outcome=ProviderOutcome.TEMPORARY_FAILURE,
                error="mock: random temporary failure",
            )

        return ProviderResult(
            outcome=ProviderOutcome.SUCCESS,
            provider_message_id=f"mock-{message_id}",
        )

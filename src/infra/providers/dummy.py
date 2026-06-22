from uuid import UUID

from infra.providers.types import (
    ProviderOutcome,
    ProviderResult,
)


class DummySmsProvider:
    def send(
        self,
        *,
        message_id: UUID,
        recipient: str,  # noqa: ARG002
        body: str,  # noqa: ARG002
    ) -> ProviderResult:
        return ProviderResult(
            outcome=ProviderOutcome.SUCCESS,
            provider_message_id=f"dummy-{message_id}",
        )

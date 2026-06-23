from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class ProviderOutcome(StrEnum):
    SUCCESS = "success"
    TEMPORARY_FAILURE = "temporary_failure"
    PERMANENT_FAILURE = "permanent_failure"


@dataclass(frozen=True)
class ProviderResult:
    outcome: ProviderOutcome
    provider_message_id: str | None = None
    error: str | None = None


class SmsProvider(Protocol):
    def send(
        self,
        *,
        message_id: UUID,
        recipient: str,
        body: str,
    ) -> ProviderResult: ...

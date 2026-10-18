"""Optional AI provider interface and durable-job input/output validation."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

import httpx
from psycopg import Connection
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from realestate_crm.config import Settings
from realestate_crm.services.ai_prompts import PROMPTS


class AIError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CallScriptInput(StrictInput):
    objective: str = Field(min_length=1, max_length=120)
    language: str = Field(default="English", min_length=1, max_length=40)
    context: dict[str, Any] = Field(default_factory=dict)
    desired_length: str = Field(default="brief", max_length=80)


class StructuredNotesInput(StrictInput):
    notes: str = Field(min_length=1, max_length=20_000)
    existing_preferences: dict[str, Any] = Field(default_factory=dict)


class ObjectionInput(StrictInput):
    objection: str = Field(min_length=1, max_length=500)
    context: dict[str, Any] = Field(default_factory=dict)


class FollowupDraftInput(StrictInput):
    context: dict[str, Any] = Field(default_factory=dict)


class OpportunityAssistInput(StrictInput):
    current_stage: str = Field(min_length=1, max_length=40)
    next_action: str | None = Field(default=None, max_length=500)
    buyer_requirements: dict[str, Any] = Field(default_factory=dict)
    property_summary: dict[str, Any] = Field(default_factory=dict)
    recent_recorded_events: list[dict[str, Any]] = Field(default_factory=list, max_length=8)


class SalesAnalysisInput(StrictInput):
    period: str = Field(min_length=1, max_length=20)
    start: str = Field(min_length=10, max_length=10)
    end: str = Field(min_length=10, max_length=10)
    metrics: dict[str, int | float] = Field(default_factory=dict)
    campaigns: list[dict[str, Any]] = Field(default_factory=list, max_length=5)
    property_performance: list[dict[str, Any]] = Field(default_factory=list, max_length=5)


class CallScriptOutput(StrictOutput):
    opening: str = Field(min_length=1, max_length=350)
    reason_for_calling: str = Field(min_length=1, max_length=500)
    questions: list[str] = Field(min_length=1, max_length=2)
    next_step: str = Field(min_length=1, max_length=250)
    alternatives: list[str] = Field(default_factory=list, max_length=2)


class StructuredNotesOutput(StrictOutput):
    property_type: str | None = None
    preferred_location: str | None = Field(default=None, max_length=160)
    accepted_locations: list[str] = Field(default_factory=list, max_length=8)
    min_budget: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    max_budget: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    budget_declined: bool | None = None
    currency: str | None = None
    purchase_purpose: str | None = None
    bedrooms: int | None = Field(default=None, ge=0, le=100)
    preferred_payment_plan: str | None = None
    expected_timeline: str | None = None
    genuine_interest: bool | None = None
    followup_requested: bool = False
    followup_text: str | None = Field(default=None, max_length=300)
    missing_information: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("property_type")
    @classmethod
    def validate_property_type(cls, value: str | None) -> str | None:
        choices = {
            "APARTMENT",
            "COMMERCIAL_SHOP",
            "OFFICE",
            "HOUSE_VILLA",
            "LAND",
            "COMMERCIAL_BUILDING",
            "OTHER",
        }
        if value is not None and value.upper() not in choices:
            raise ValueError("Unsupported property type")
        return value.upper() if value else None

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.upper()
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            raise ValueError("Unsupported currency")
        return value

    @field_validator("expected_timeline")
    @classmethod
    def validate_timeline(cls, value: str | None) -> str | None:
        choices = {
            "IMMEDIATELY",
            "WITHIN_1_MONTH",
            "WITHIN_3_MONTHS",
            "WITHIN_6_MONTHS",
            "WITHIN_1_YEAR",
            "MORE_THAN_1_YEAR",
            "RESEARCHING",
            "UNKNOWN",
        }
        if value is not None and value.upper() not in choices:
            raise ValueError("Unsupported timeline")
        return value.upper() if value else None

    @field_validator("preferred_payment_plan")
    @classmethod
    def validate_payment(cls, value: str | None) -> str | None:
        choices = {
            "FULL_CASH",
            "INSTALLMENT",
            "BANK_FINANCING",
            "MORTGAGE",
            "FLEXIBLE",
            "UNDECIDED",
            "UNKNOWN",
        }
        if value is not None and value.upper() not in choices:
            raise ValueError("Unsupported payment preference")
        return value.upper() if value else None

    @field_validator("purchase_purpose")
    @classmethod
    def validate_purpose(cls, value: str | None) -> str | None:
        choices = {
            "PERSONAL_RESIDENCE",
            "INVESTMENT",
            "RENTAL_INCOME",
            "COMMERCIAL_BUSINESS",
            "LAND_INVESTMENT",
            "OTHER",
            "UNKNOWN",
        }
        if value is not None and value.upper() not in choices:
            raise ValueError("Unsupported purchase purpose")
        return value.upper() if value else None

    @field_validator("followup_requested")
    @classmethod
    def ambiguous_followup_is_not_a_schedule(cls, value: bool) -> bool:
        return value

    @field_validator("missing_information")
    @classmethod
    def bound_missing(cls, values: list[str]) -> list[str]:
        return [item[:160] for item in values]

    @field_validator("preferred_location")
    @classmethod
    def bound_location(cls, value: str | None) -> str | None:
        return value.strip()[:160] if value else None

    @field_validator("accepted_locations")
    @classmethod
    def bound_locations(cls, values: list[str]) -> list[str]:
        return [item.strip()[:120] for item in values if item.strip()]

    @field_validator("max_budget")
    @classmethod
    def nonnegative_max(cls, value: Decimal | None) -> Decimal | None:
        return value

    def model_post_init(self, __context: Any) -> None:
        if (
            self.min_budget is not None
            and self.max_budget is not None
            and self.min_budget > self.max_budget
        ):
            raise ValueError("Minimum budget cannot exceed maximum budget")


class ObjectionOutput(StrictOutput):
    respectful_response: str = Field(min_length=1, max_length=1000)
    clarifying_question: str = Field(min_length=1, max_length=500)
    possible_next_action: str = Field(min_length=1, max_length=500)
    stop_when: str = Field(min_length=1, max_length=500)


class FollowupDraftOutput(StrictOutput):
    message: str = Field(min_length=1, max_length=2000)
    do_not_send: bool = False


class OpportunityAssistOutput(StrictOutput):
    summary: str = Field(min_length=1, max_length=1500)
    suggested_action: str = Field(min_length=1, max_length=400)
    reason: str = Field(min_length=1, max_length=800)
    followup_draft: str | None = Field(default=None, max_length=1200)
    do_not_contact: bool = False


class SalesAnalysisOutput(StrictOutput):
    observations: list[str] = Field(default_factory=list, max_length=5)
    recommendations: list[str] = Field(default_factory=list, max_length=5)
    caveats: list[str] = Field(default_factory=list, max_length=5)


OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "CALL_SCRIPT": CallScriptOutput,
    "STRUCTURE_NOTES": StructuredNotesOutput,
    "OBJECTION": ObjectionOutput,
    "FOLLOWUP_DRAFT": FollowupDraftOutput,
    "OPPORTUNITY_ASSIST": OpportunityAssistOutput,
    "SALES_ANALYSIS": SalesAnalysisOutput,
}

INPUT_MODELS: dict[str, type[BaseModel]] = {
    "CALL_SCRIPT": CallScriptInput,
    "STRUCTURE_NOTES": StructuredNotesInput,
    "OBJECTION": ObjectionInput,
    "FOLLOWUP_DRAFT": FollowupDraftInput,
    "OPPORTUNITY_ASSIST": OpportunityAssistInput,
    "SALES_ANALYSIS": SalesAnalysisInput,
}


def validate_input(job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if job_type not in INPUT_MODELS:
        raise AIError("Unsupported AI operation")
    provider_payload = dict(payload)
    if job_type == "STRUCTURE_NOTES":
        provider_payload.pop("source_note", None)
    try:
        return INPUT_MODELS[job_type].model_validate(provider_payload).model_dump(mode="json")
    except ValidationError as exc:
        raise AIError("AI input did not match the required operation schema") from exc


class AIProvider(Protocol):
    async def generate(
        self, operation: str, payload: dict[str, Any], output_model: type[BaseModel]
    ) -> tuple[dict[str, Any], dict[str, int]]: ...


class OpenAICompatibleProvider:
    """Chat Completions JSON-mode adapter; endpoint/model are administrator supplied."""

    def __init__(self, settings: Settings):
        self.settings = settings

    async def generate(
        self, operation: str, payload: dict[str, Any], output_model: type[BaseModel]
    ) -> tuple[dict[str, Any], dict[str, int]]:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > self.settings.ai_max_input_chars:
            raise AIError("AI input is over the configured length limit")
        system = (
            PROMPTS[operation]
            + "\nRequired JSON schema:\n"
            + json.dumps(output_model.model_json_schema(), ensure_ascii=False)
        )
        base_url = self.settings.ai_base_url.rstrip("/")
        url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
        key = self.settings.ai_api_key.get_secret_value()
        body = {
            "model": self.settings.ai_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": encoded},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": max(128, self.settings.ai_max_output_chars // 4),
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.ai_request_timeout_seconds
            ) as client:
                response = await client.post(
                    url, json=body, headers={"Authorization": f"Bearer {key}"}
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise AIError("AI provider timed out or could not be reached", retryable=True) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise AIError(f"AI provider returned HTTP {response.status_code}", retryable=True)
        if response.status_code >= 400:
            raise AIError(f"AI provider rejected the request (HTTP {response.status_code})")
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str) or len(content) > self.settings.ai_max_output_chars:
                raise AIError("AI output is missing or exceeds the configured length limit")
            parsed = json.loads(content)
            result = output_model.model_validate(parsed).model_dump(mode="json")
            usage = body.get("usage") or {}
            tokens = {
                "input_tokens": int(usage.get("prompt_tokens", 0)),
                "output_tokens": int(usage.get("completion_tokens", 0)),
            }
            return result, tokens
        except (
            KeyError,
            IndexError,
            TypeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise AIError(
                "AI response did not match the required output schema", retryable=True
            ) from exc


def queue_ai_job(
    conn: Connection,
    *,
    settings: Settings,
    user_id: int,
    chat_id: int,
    job_type: str,
    payload: dict[str, Any],
    contact_id: UUID | None = None,
    property_id: UUID | None = None,
    opportunity_id: UUID | None = None,
) -> dict[str, Any]:
    if not getattr(settings, "ai_enabled", False):
        raise AIError("AI assistant is disabled")
    if job_type not in OUTPUT_MODELS:
        raise AIError("Unsupported AI operation")
    provider_payload = validate_input(job_type, payload)
    raw_provider = json.dumps(provider_payload, ensure_ascii=False, separators=(",", ":"))
    if len(raw_provider) > settings.ai_max_input_chars:
        raise AIError("AI input is over the configured length limit")
    stored_payload = dict(provider_payload)
    if job_type == "STRUCTURE_NOTES" and "source_note" in payload:
        source_note = payload["source_note"]
        if not isinstance(source_note, str) or len(source_note) > settings.ai_max_input_chars:
            raise AIError("Original notes are over the configured length limit")
        stored_payload["source_note"] = source_note
    raw = json.dumps(stored_payload, ensure_ascii=False, separators=(",", ":"))
    return conn.execute(
        """insert into ai_jobs
             (telegram_user_id,chat_id,contact_id,property_id,opportunity_id,job_type,input_payload)
           values (%s,%s,%s,%s,%s,%s,%s) returning id""",
        (user_id, chat_id, contact_id, property_id, opportunity_id, job_type, raw),
    ).fetchone()


async def generate_ai_result(
    settings: Settings, job_type: str, payload: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, int]]:
    if not getattr(settings, "ai_enabled", False):
        raise AIError("AI assistant is disabled")
    if settings.ai_provider != "openai_compatible":
        raise AIError("The configured AI provider is not supported")
    validated_payload = validate_input(job_type, payload)
    provider: AIProvider = OpenAICompatibleProvider(settings)
    return await provider.generate(job_type, validated_payload, OUTPUT_MODELS[job_type])

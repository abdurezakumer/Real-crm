from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator


class PropertyInterestInput(BaseModel):
    property_type: str | None = None
    preferred_location: str | None = None
    accepted_locations: list[str] = Field(default_factory=list)
    min_budget: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    max_budget: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    currency: str = "ETB"
    bedrooms: int | None = Field(default=None, ge=0, le=100)
    min_bedrooms: int | None = Field(default=None, ge=0, le=100)
    max_bedrooms: int | None = Field(default=None, ge=0, le=100)
    min_area_sqm: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    max_area_sqm: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    purchase_purpose: str | None = None
    floor_preference: str | None = None
    new_or_resale: str | None = None
    profile_complete: bool = False
    preferred_payment_plan: str | None = None
    readiness: str | None = None
    expected_timeline: str | None = None
    additional_requirements: str | None = None

    @field_validator("property_type")
    @classmethod
    def supported_property(cls, value: str) -> str:
        supported = {
            "APARTMENT",
            "COMMERCIAL_SHOP",
            "OFFICE",
            "HOUSE_VILLA",
            "LAND",
            "COMMERCIAL_BUILDING",
            "OTHER",
        }
        if value is None:
            return value
        value = value.upper()
        if value not in supported:
            raise ValueError("Choose a listed property type")
        return value

    @field_validator("purchase_purpose")
    @classmethod
    def supported_purpose(cls, value: str | None) -> str | None:
        if value is None:
            return value
        choices = {
            "PERSONAL_RESIDENCE",
            "INVESTMENT",
            "RENTAL_INCOME",
            "COMMERCIAL_BUSINESS",
            "LAND_INVESTMENT",
            "OTHER",
            "UNKNOWN",
        }
        value = value.upper()
        if value not in choices:
            raise ValueError("Choose a supported purchase purpose")
        return value

    @field_validator("expected_timeline")
    @classmethod
    def supported_timeline(cls, value: str | None) -> str | None:
        if value is None:
            return value
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
        value = value.upper()
        if value not in choices:
            raise ValueError("Choose a supported purchase timeline")
        return value

    @field_validator("preferred_payment_plan")
    @classmethod
    def supported_payment_preference(cls, value: str | None) -> str | None:
        if value is None:
            return value
        choices = {
            "FULL_CASH",
            "INSTALLMENT",
            "BANK_FINANCING",
            "MORTGAGE",
            "FLEXIBLE",
            "UNDECIDED",
            "UNKNOWN",
        }
        value = value.upper()
        if value not in choices:
            raise ValueError("Choose a supported payment preference")
        return value

    @field_validator("new_or_resale")
    @classmethod
    def supported_condition(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.upper()
        if value not in {"NEW", "RESALE", "EITHER", "UNKNOWN"}:
            raise ValueError("Choose NEW, RESALE, EITHER, or UNKNOWN")
        return value

    @field_validator("floor_preference")
    @classmethod
    def bound_floor_preference(cls, value: str | None) -> str | None:
        return value.strip()[:80] if value else None

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        value = value.upper()
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            raise ValueError("Currency must be a three-letter code, such as ETB")
        return value

    @model_validator(mode="after")
    def ordered_budget(self):
        if (
            self.min_budget is not None
            and self.max_budget is not None
            and self.min_budget > self.max_budget
        ):
            raise ValueError("Minimum budget cannot exceed maximum budget")
        if (
            self.min_bedrooms is not None
            and self.max_bedrooms is not None
            and self.min_bedrooms > self.max_bedrooms
        ):
            raise ValueError("Minimum bedrooms cannot exceed maximum bedrooms")
        if (
            self.min_area_sqm is not None
            and self.max_area_sqm is not None
            and self.min_area_sqm > self.max_area_sqm
        ):
            raise ValueError("Minimum area cannot exceed maximum area")
        self.accepted_locations = [
            item.strip()[:120] for item in self.accepted_locations if item.strip()
        ]
        return self


class PropertyInput(BaseModel):
    project_name: str | None = Field(default=None, max_length=160)
    title: str = Field(min_length=1, max_length=200)
    property_type: str
    developer_or_seller: str | None = Field(default=None, max_length=160)
    location: str | None = Field(default=None, max_length=160)
    sub_location: str | None = Field(default=None, max_length=160)
    price: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    currency: str
    minimum_negotiable_price: Decimal | None = Field(
        default=None, ge=0, max_digits=16, decimal_places=2
    )
    bedrooms: int | None = Field(default=None, ge=0, le=100)
    bathrooms: int | None = Field(default=None, ge=0, le=100)
    floor_number: str | None = Field(default=None, max_length=40)
    area_sqm: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    available_payment_plans: list[str] = Field(default_factory=list)
    payment_plans_verified: bool = False
    construction_status: str = "UNKNOWN"
    delivery_date: str | None = None
    availability_status: str = "UNVERIFIED"
    description: str | None = Field(default=None, max_length=2000)
    internal_notes: str | None = Field(default=None, max_length=2000)
    listing_source: str | None = Field(default=None, max_length=160)

    @field_validator("property_type")
    @classmethod
    def valid_property_type(cls, value: str) -> str:
        value = value.upper()
        supported = {
            "APARTMENT",
            "COMMERCIAL_SHOP",
            "OFFICE",
            "HOUSE_VILLA",
            "LAND",
            "COMMERCIAL_BUILDING",
            "OTHER",
        }
        if value not in supported:
            raise ValueError("Choose a supported property type")
        return value

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        value = value.upper()
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            raise ValueError("Currency must be a three-letter code, such as ETB")
        return value

    @field_validator("construction_status")
    @classmethod
    def valid_construction(cls, value: str) -> str:
        value = value.upper()
        if value not in {
            "UNDER_CONSTRUCTION",
            "NEAR_COMPLETION",
            "READY_FOR_DELIVERY",
            "COMPLETED",
            "UNKNOWN",
        }:
            raise ValueError("Choose a supported construction status")
        return value

    @field_validator("availability_status")
    @classmethod
    def valid_availability(cls, value: str) -> str:
        value = value.upper()
        if value not in {"AVAILABLE", "RESERVED", "SOLD", "TEMPORARILY_UNAVAILABLE", "UNVERIFIED"}:
            raise ValueError("Choose a supported availability status")
        return value

    @model_validator(mode="after")
    def valid_price_range(self):
        if (
            self.minimum_negotiable_price is not None
            and self.price is not None
            and self.minimum_negotiable_price > self.price
        ):
            raise ValueError("Minimum negotiable price cannot exceed listed price")
        if self.delivery_date is not None:
            from datetime import date

            date.fromisoformat(self.delivery_date)
        self.available_payment_plans = [
            item.strip().upper()[:40] for item in self.available_payment_plans if item.strip()
        ]
        return self


class OpportunityInput(BaseModel):
    estimated_sale_value: Decimal | None = Field(
        default=None, ge=0, max_digits=16, decimal_places=2
    )
    currency: str = "ETB"
    expected_closing_date: str | None = None
    next_action: str | None = Field(default=None, max_length=500)
    next_action_due: str | None = None
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            raise ValueError("Currency must be a three-letter code, such as ETB")
        return value

    @field_validator("expected_closing_date")
    @classmethod
    def valid_closing_date(cls, value: str | None) -> str | None:
        if value:
            from datetime import date

            date.fromisoformat(value)
        return value

    @field_validator("next_action_due")
    @classmethod
    def valid_action_datetime(cls, value: str | None) -> str | None:
        if value:
            from datetime import datetime

            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("Next-action time must include a timezone")
        return value


class NegotiationInput(BaseModel):
    asking_price: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    buyer_offer: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    seller_counteroffer: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    current_discussed_price: Decimal | None = Field(
        default=None, ge=0, max_digits=16, decimal_places=2
    )
    currency: str = "ETB"
    down_payment: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    payment_plan: str | None = Field(default=None, max_length=1000)
    proposed_reservation_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=16, decimal_places=2
    )
    offer_date: str | None = None
    offer_expiry_date: str | None = None
    negotiation_status: str | None = None
    next_action: str | None = Field(default=None, max_length=500)
    next_action_due: str | None = None
    event_type: str | None = None
    note: str | None = Field(default=None, max_length=2000)

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            raise ValueError("Currency must be a three-letter code, such as ETB")
        return value

    @field_validator("offer_date", "offer_expiry_date")
    @classmethod
    def valid_offer_date(cls, value: str | None) -> str | None:
        if value:
            from datetime import date

            date.fromisoformat(value)
        return value

    @field_validator("next_action_due")
    @classmethod
    def valid_negotiation_datetime(cls, value: str | None) -> str | None:
        if value:
            from datetime import datetime

            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("Next-action time must include a timezone")
        return value

    @model_validator(mode="after")
    def valid_offer_and_payment(self):
        if self.offer_date and self.offer_expiry_date and self.offer_expiry_date < self.offer_date:
            raise ValueError("Offer expiry cannot be before the offer date")
        discussed = self.current_discussed_price
        if (
            discussed is not None
            and self.down_payment is not None
            and self.down_payment > discussed
        ):
            raise ValueError("Down payment cannot exceed the discussed price")
        return self


class VisitInput(BaseModel):
    scheduled_at: str
    meeting_point: str | None = Field(default=None, max_length=500)
    internal_note: str | None = Field(default=None, max_length=2000)
    buyer_instructions: str | None = Field(default=None, max_length=1000)
    duplicate_confirmed: bool = False
    availability_confirmed: bool = False

    @field_validator("scheduled_at")
    @classmethod
    def valid_visit_datetime(cls, value: str) -> str:
        from datetime import datetime

        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Visit time must include a timezone")
        return value


class ReservationInput(BaseModel):
    reservation_date: str
    reservation_amount: Decimal = Field(ge=0, max_digits=16, decimal_places=2)
    currency: str
    reservation_reference: str | None = Field(default=None, max_length=200)
    expires_at: str | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            raise ValueError("Currency must be a three-letter code, such as ETB")
        return value

    @field_validator("reservation_date")
    @classmethod
    def valid_reservation_date(cls, value: str) -> str:
        from datetime import date

        date.fromisoformat(value)
        return value

    @field_validator("expires_at")
    @classmethod
    def valid_reservation_expiry(cls, value: str | None) -> str | None:
        if value:
            from datetime import datetime

            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("Reservation expiry must include a timezone")
        return value


class SaleInput(BaseModel):
    recorded_sale_price: Decimal = Field(gt=0, max_digits=16, decimal_places=2)
    currency: str
    sale_date: str
    payment_plan_summary: str | None = Field(default=None, max_length=1000)
    supporting_reference: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            raise ValueError("Currency must be a three-letter code, such as ETB")
        return value

    @field_validator("sale_date")
    @classmethod
    def valid_sale_date(cls, value: str) -> str:
        from datetime import date

        date.fromisoformat(value)
        return value

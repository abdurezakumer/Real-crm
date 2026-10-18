import phonenumbers
from phonenumbers import NumberParseException


def normalize_phone(value: object, default_region: str = "ET") -> str | None:
    """Return E.164 for a valid phone number, otherwise None."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() in {"none", "nan", "null"}:
        return None
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except NumberParseException:
        return None
    if not phonenumbers.is_possible_number(parsed) or not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def tel_uri(value: str) -> str:
    normalized = normalize_phone(value)
    if normalized is None:
        raise ValueError("Cannot create a dialer link for an invalid phone number")
    return f"tel:{normalized}"

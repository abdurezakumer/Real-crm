import re
import unicodedata
from decimal import Decimal

import phonenumbers
from phonenumbers import NumberParseException


def _phone_text(value: object) -> str:
    """Turn common Excel numeric/text cells into a useful phone string."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        return str(value.quantize(Decimal("1")))
    raw = str(value).strip()
    return re.sub(r"(?<=\d)\.0+$", "", raw)


def _ascii_digits(value: str) -> str:
    digits = []
    for character in value:
        try:
            digits.append(str(unicodedata.digit(character)))
        except (TypeError, ValueError):
            continue
    return "".join(digits)


def normalize_phone(value: object, default_region: str = "ET") -> str | None:
    """Return E.164 for a valid phone number, otherwise None."""
    if value is None:
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    if isinstance(value, Decimal) and value.is_finite() and value != value.to_integral_value():
        return None
    raw = _phone_text(value)
    if not raw or raw.lower() in {"none", "nan", "null"}:
        return None

    # Excel users often insert spaces or punctuation between digits. Normalize
    # the Ethiopian mobile formats explicitly before asking libphonenumber to
    # validate the result:
    #   +2519XXXXXXXX, 2519XXXXXXXX, 09XXXXXXXX, and 9XXXXXXXX.
    digits = _ascii_digits(raw)
    candidate = None
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 12 and digits.startswith("2519"):
        candidate = f"+{digits}"
    elif len(digits) == 10 and digits.startswith("09"):
        candidate = f"+251{digits[1:]}"
    elif len(digits) == 9 and digits.startswith("9"):
        candidate = f"+251{digits}"

    try:
        parsed = phonenumbers.parse(candidate or raw, default_region)
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

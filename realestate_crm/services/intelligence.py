"""Deterministic property matching, qualification, scoring, and inventory operations."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg import Connection

MATCH_WEIGHTS = {
    "budget": 35,
    "location": 25,
    "property_type": 15,
    "bedrooms": 10,
    "payment": 10,
    "timeline": 5,
}
LEAD_WEIGHTS = {
    "interest": 25,
    "requirements_budget": 20,
    "timeline": 20,
    "engagement": 15,
    "next_action": 10,
    "recent_positive": 10,
}
TIMELINE_MONTHS = {
    "IMMEDIATELY": 0,
    "WITHIN_1_MONTH": 1,
    "WITHIN_3_MONTHS": 3,
    "WITHIN_6_MONTHS": 6,
    "WITHIN_1_YEAR": 12,
}
PAYMENT_ALIASES = {
    "FULL_CASH": {"FULL_CASH", "CASH"},
    "INSTALLMENT": {"INSTALLMENT", "INSTALLMENTS", "INSTALLMENT_PLAN"},
    "BANK_FINANCING": {"BANK_FINANCING", "BANK_FINANCE"},
    "MORTGAGE": {"MORTGAGE", "FINANCING"},
    "FLEXIBLE": set(),
}


def match_weights_from_settings(settings: Any) -> dict[str, int]:
    return {
        key: int(getattr(settings, f"match_{key}_weight", default))
        for key, default in MATCH_WEIGHTS.items()
    }


def lead_weights_from_settings(settings: Any) -> dict[str, int]:
    keys = {
        "interest": "interest",
        "requirements_budget": "requirements_budget",
        "timeline": "timeline",
        "engagement": "engagement",
        "next_action": "next_action",
        "recent_positive": "recent_positive",
    }
    return {key: int(getattr(settings, f"lead_{key}_weight", LEAD_WEIGHTS[key])) for key in keys}


def _number(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def match_property(
    buyer: dict[str, Any],
    prop: dict[str, Any],
    *,
    weights: dict[str, int] | None = None,
    allow_near_budget: bool = False,
    hard_constraints: dict[str, bool] | None = None,
    alternative_location_points: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Return a reproducible, explained match. Unknown values never earn points."""
    weights = {**MATCH_WEIGHTS, **(weights or {})}
    hard = {"property_type": True, "location": True, "budget": True, "bedrooms": True}
    hard.update(hard_constraints or {})
    today = today or date.today()
    score = 0
    reasons: list[str] = []
    missing: list[str] = []
    rejected: list[str] = []

    if prop.get("availability_status") != "AVAILABLE":
        rejected.append("Property is not verified as available")

    buyer_type = str(buyer.get("property_type") or "").upper()
    property_type = str(prop.get("property_type") or "").upper()
    if not buyer_type:
        missing.append("Property type preference not recorded")
    elif buyer_type == property_type:
        score += weights["property_type"]
        reasons.append("Property type matches")
    elif hard["property_type"]:
        rejected.append("Property type does not match")
    else:
        missing.append("Property type differs from preference")

    preferred = _normalized(buyer.get("preferred_location"))
    alternates = {_normalized(item) for item in buyer.get("accepted_locations") or []}
    prop_locations = {_normalized(prop.get("location")), _normalized(prop.get("sub_location"))}
    prop_locations.discard("")
    if not preferred and not alternates:
        missing.append("Preferred location not recorded")
    elif preferred and preferred in prop_locations:
        score += weights["location"]
        reasons.append("Preferred location matches")
    elif prop_locations.intersection(alternates):
        points = alternative_location_points
        if points is None:
            points = max(0, weights["location"] - 5)
        score += min(weights["location"], points)
        reasons.append("An accepted alternative location matches")
    elif hard["location"]:
        rejected.append("Property is outside the accepted locations")
    else:
        missing.append("Location differs from stated preferences")

    currency = str(buyer.get("currency") or "").upper()
    property_currency = str(prop.get("currency") or "").upper()
    low, high, price = (
        _number(buyer.get("min_budget")),
        _number(buyer.get("max_budget")),
        _number(prop.get("price")),
    )
    if low is None and high is None:
        missing.append("Budget not recorded")
    elif not currency:
        missing.append("Budget currency not recorded")
    elif currency != property_currency:
        if hard["budget"]:
            rejected.append("Budget currency differs from the property currency")
        else:
            missing.append("Budget currency differs from the property currency")
    elif price is None:
        missing.append("Verified property price not recorded")
    elif (low is None or price >= low) and (high is None or price <= high):
        score += weights["budget"]
        reasons.append("Price is within the recorded budget")
    elif high is not None and price > high:
        over = price - high
        near_limit = high * Decimal("0.10")
        if allow_near_budget and high > 0 and over <= near_limit:
            score += max(1, weights["budget"] // 2)
            reasons.append("Price is up to 10% above budget; confirm affordability")
        elif hard["budget"]:
            rejected.append("Price is above the maximum budget")
        else:
            missing.append("Price is above the recorded maximum budget")
    else:
        # A stated minimum is treated as a preference, not as proof of inability to buy.
        reasons.append("Price is below the preferred minimum; confirm expectations")

    min_bedrooms = buyer.get("min_bedrooms")
    max_bedrooms = buyer.get("max_bedrooms")
    if min_bedrooms is None and max_bedrooms is None and buyer.get("bedrooms") is not None:
        min_bedrooms = max_bedrooms = buyer["bedrooms"]
    bedrooms = prop.get("bedrooms")
    if min_bedrooms is None and max_bedrooms is None:
        missing.append("Bedroom preference not recorded")
    elif bedrooms is None:
        missing.append("Property bedroom count not recorded")
    elif (min_bedrooms is None or bedrooms >= min_bedrooms) and (
        max_bedrooms is None or bedrooms <= max_bedrooms
    ):
        score += weights["bedrooms"]
        reasons.append("Bedroom count matches")
    elif hard["bedrooms"]:
        rejected.append("Bedroom count is outside the requested range")

    payment = str(buyer.get("preferred_payment_plan") or "").upper()
    property_plans = {str(item).upper() for item in prop.get("available_payment_plans") or []}
    if not payment or payment in {"UNKNOWN", "UNDECIDED"}:
        missing.append("Payment preference not confirmed")
    elif not prop.get("payment_plans_verified"):
        missing.append("Property payment options are not verified")
    else:
        compatible = (
            bool(property_plans)
            if payment == "FLEXIBLE"
            else bool(PAYMENT_ALIASES.get(payment, {payment}).intersection(property_plans))
        )
        if compatible:
            score += weights["payment"]
            reasons.append("Payment preference matches verified property options")
        else:
            missing.append("Verified payment options do not match the buyer preference")

    timeline = str(buyer.get("expected_timeline") or "").upper()
    delivery = prop.get("delivery_date")
    status = str(prop.get("construction_status") or "UNKNOWN").upper()
    if timeline in {"", "UNKNOWN", "RESEARCHING", "JUST_RESEARCHING"}:
        missing.append("Purchase timeline not confirmed")
    elif status in {"READY_FOR_DELIVERY", "COMPLETED"}:
        score += weights["timeline"]
        reasons.append("Property is ready within the buyer's stated purchase horizon")
    elif delivery is not None and timeline in TIMELINE_MONTHS:
        if isinstance(delivery, str):
            delivery = date.fromisoformat(delivery)
        months_until_delivery = max(
            0, (delivery.year - today.year) * 12 + delivery.month - today.month
        )
        if months_until_delivery <= TIMELINE_MONTHS[timeline]:
            score += weights["timeline"]
            reasons.append("Estimated delivery fits the stated purchase timeline")
        else:
            missing.append("Estimated delivery is later than the stated purchase timeline")
    else:
        missing.append("Property delivery timing is not verified")

    score = max(0, min(100, int(score)))
    category = (
        "EXCELLENT"
        if score >= 85
        else "STRONG"
        if score >= 70
        else "POSSIBLE"
        if score >= 50
        else "WEAK"
    )
    return {
        "score": score,
        "category": category,
        "reasons": reasons,
        "missing_information": missing,
        "rejected": bool(rejected),
        "rejection_reasons": rejected,
        "partial": bool(missing),
        "rule_version": "phase2-match-v1",
    }


def derive_qualification(values: dict[str, Any]) -> dict[str, Any]:
    """Determine status from explicitly recorded checklist fields only."""
    criteria = {
        "Property requirement": bool(values.get("property_requirement_known")),
        "Budget recorded or declined": bool(
            values.get("budget_disclosed") or values.get("budget_declined")
        ),
        "Timeline confirmed, including unknown": bool(values.get("timeline_confirmed")),
        "Genuine interest expressed": bool(values.get("genuine_interest")),
        "Concrete next action": bool(str(values.get("next_action") or "").strip()),
    }
    if values.get("qualification_status") == "DISQUALIFIED":
        status = "DISQUALIFIED"
    else:
        complete = all(criteria.values())
        if complete:
            timeline = str(values.get("expected_timeline") or "").upper()
            status = (
                "HIGH_PRIORITY_QUALIFIED"
                if values.get("requested_property_visit")
                or timeline in {"IMMEDIATELY", "WITHIN_1_MONTH", "WITHIN_3_MONTHS"}
                else "QUALIFIED"
            )
        elif any(criteria.values()):
            status = "IN_PROGRESS"
        else:
            status = "UNQUALIFIED"
    return {
        "status": status,
        "criteria": criteria,
        "missing": [k for k, v in criteria.items() if not v],
    }


def explain_lead_score(
    values: dict[str, Any], weights: dict[str, int] | None = None
) -> dict[str, Any]:
    weights = {**LEAD_WEIGHTS, **(weights or {})}
    score = 0
    reasons: list[str] = []
    missing: list[str] = []

    if values.get("genuine_interest") or values.get("interest_level") in {
        "MEDIUM",
        "HIGH",
        "VERY_HIGH",
    }:
        score += weights["interest"]
        reasons.append("Confirmed purchase interest")
    else:
        missing.append("Purchase interest not confirmed")

    requirement = bool(values.get("property_type") or values.get("preferred_location"))
    budget = bool(values.get("budget_known"))
    if requirement and budget:
        score += weights["requirements_budget"]
        reasons.append("Property requirements and budget recorded")
    elif requirement or budget:
        score += weights["requirements_budget"] // 2
        reasons.append("Some property or budget details recorded")
        missing.append("Property requirements or budget details incomplete")
    else:
        missing.append("Property requirements and budget missing")

    if str(values.get("expected_timeline") or "").upper() in set(TIMELINE_MONTHS) | {
        "MORE_THAN_1_YEAR"
    }:
        score += weights["timeline"]
        reasons.append("Purchase timeline recorded")
    else:
        missing.append("Purchase timeline not recorded")

    if values.get("meaningful_engagement"):
        score += weights["engagement"]
        reasons.append("Requested details, a meeting, or a property visit")
    else:
        missing.append("Meaningful engagement not recorded")

    if values.get("next_action"):
        score += weights["next_action"]
        reasons.append("Concrete next action recorded")
    else:
        missing.append("Next action not recorded")

    if values.get("recent_positive"):
        score += weights["recent_positive"]
        reasons.append("Positive interaction recorded in the last 30 days")
    else:
        missing.append("No recent positive interaction recorded")

    score = max(0, min(100, int(score)))
    category = "A" if score >= 80 else "B" if score >= 60 else "C" if score >= 40 else "NEEDS_INFO"
    return {
        "score": score,
        "category": category,
        "reasons": reasons,
        "missing_information": missing,
        "rule_version": "phase2-lead-v1",
    }


def recalculate_lead_score(
    conn: Connection, contact_id: UUID, weights: dict[str, int] | None = None
) -> dict[str, Any] | None:
    if weights is None:
        from pydantic import ValidationError

        from realestate_crm.config import get_settings

        try:
            weights = lead_weights_from_settings(get_settings())
        except ValidationError:
            weights = None
    values = conn.execute(
        """select c.interest_level, p.property_type, p.preferred_location,
                  (p.min_budget is not null or p.max_budget is not null or coalesce(q.budget_declined,false)) as budget_known,
                  p.expected_timeline, coalesce(q.genuine_interest,false) genuine_interest,
                  coalesce(q.next_action,'') <> '' or exists (
                    select 1 from follow_ups f where f.contact_id=c.id and f.status='PENDING'
                  ) as next_action,
                  exists (select 1 from call_logs l where l.contact_id=c.id and (
                    l.outcome_code in ('REQUESTED_PROPERTY_DETAILS','REQUESTED_PROPERTY_VISIT',
                      'MEETING_SCHEDULED','QUALIFIED') or l.outcome_category='SALES_INTEREST'
                  )) as meaningful_engagement,
                  exists (select 1 from call_logs l where l.contact_id=c.id
                    and l.created_at >= now()-interval '30 days'
                    and l.outcome_category in ('SALES_INTEREST','PROPERTY_REQUIREMENTS','FOLLOW_UP')
                    and l.outcome_code not in ('NO_ANSWER','BUSY','VOICEMAIL')) as recent_positive
           from contacts c
           left join lateral (select * from contact_property_interests x where x.contact_id=c.id
             order by x.updated_at desc limit 1) p on true
           left join lead_qualifications q on q.contact_id=c.id
           where c.id=%s""",
        (contact_id,),
    ).fetchone()
    if not values:
        return None
    result = explain_lead_score(values, weights)
    conn.execute(
        """insert into lead_scores (contact_id,score,priority_category,explanation,rule_version,calculated_at)
           values (%s,%s,%s,%s,%s,now()) on conflict (contact_id) do update set
             score=excluded.score,priority_category=excluded.priority_category,
             explanation=excluded.explanation,rule_version=excluded.rule_version,calculated_at=now()""",
        (
            contact_id,
            result["score"],
            result["category"],
            json.dumps(result),
            result["rule_version"],
        ),
    )
    return result


def save_property(
    conn: Connection, values: dict[str, Any], property_id: UUID | None = None
) -> dict[str, Any]:
    fields = (
        "project_name",
        "title",
        "property_type",
        "developer_or_seller",
        "location",
        "sub_location",
        "price",
        "currency",
        "minimum_negotiable_price",
        "bedrooms",
        "bathrooms",
        "floor_number",
        "area_sqm",
        "available_payment_plans",
        "payment_plans_verified",
        "construction_status",
        "delivery_date",
        "availability_status",
        "description",
        "internal_notes",
        "listing_source",
    )
    if property_id:
        assigns = ",".join(f"{field}=%({field})s" for field in fields)
        return conn.execute(
            f"update properties set {assigns},updated_at=now() where id=%(id)s returning *",
            {**values, "id": property_id},
        ).fetchone()
    columns = ",".join(fields)
    binds = ",".join(f"%({field})s" for field in fields)
    return conn.execute(
        f"insert into properties ({columns}) values ({binds}) returning *", values
    ).fetchone()


def get_property(conn: Connection, property_id: UUID) -> dict[str, Any] | None:
    return conn.execute("select * from properties where id=%s", (property_id,)).fetchone()


def update_availability(
    conn: Connection, property_id: UUID, status: str, user_id: int
) -> dict[str, Any] | None:
    status = status.upper()
    if status not in {"AVAILABLE", "RESERVED", "SOLD", "TEMPORARILY_UNAVAILABLE", "UNVERIFIED"}:
        raise ValueError("Unsupported property availability")
    old = get_property(conn, property_id)
    if not old:
        return None
    row = conn.execute(
        "update properties set availability_status=%s,updated_at=now() where id=%s returning *",
        (status, property_id),
    ).fetchone()
    if old["availability_status"] == "AVAILABLE" and status != "AVAILABLE":
        conn.execute(
            """update property_recommendations set recommendation_status='NO_LONGER_AVAILABLE',updated_at=now()
               where property_id=%s and recommendation_status in ('RECOMMENDED','VIEWED','INTERESTED')""",
            (property_id,),
        )
    conn.execute(
        """insert into audit_events (telegram_user_id,property_id,event_type,old_value,new_value)
           values (%s,%s,'PROPERTY_AVAILABILITY',%s,%s)""",
        (
            user_id,
            property_id,
            json.dumps({"availability_status": old["availability_status"]}),
            json.dumps({"availability_status": status}),
        ),
    )
    return row


def search_properties(
    conn: Connection, query: str = "", available_only: bool = True, limit: int = 20
) -> list[dict[str, Any]]:
    term = f"%{query.strip()[:100]}%"
    return conn.execute(
        """select id,title,project_name,property_type,location,sub_location,price,currency,
                  availability_status,construction_status,bedrooms,updated_at
           from properties where (%s=false or availability_status='AVAILABLE')
             and (%s='' or lower(title) like lower(%s) or lower(coalesce(project_name,'')) like lower(%s)
               or lower(coalesce(location,'')) like lower(%s) or lower(coalesce(sub_location,'')) like lower(%s))
           order by case availability_status when 'AVAILABLE' then 0 else 1 end,updated_at desc,id
           limit %s""",
        (available_only, query.strip(), term, term, term, term, min(max(limit, 1), 50)),
    ).fetchall()


def _profile_for_contact(conn: Connection, contact_id: UUID) -> dict[str, Any] | None:
    return conn.execute(
        "select * from contact_property_interests where contact_id=%s order by updated_at desc limit 1",
        (contact_id,),
    ).fetchone()


def find_matching_properties(
    conn: Connection,
    contact_id: UUID,
    *,
    limit: int = 10,
    allow_near_budget: bool = False,
    weights: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    eligibility = conn.execute(
        "select c.contact_eligibility,c.lead_status,c.conversion_state,q.qualification_status "
        "from contacts c left join lead_qualifications q on q.contact_id=c.id where c.id=%s",
        (contact_id,),
    ).fetchone()
    if (
        not eligibility
        or eligibility["contact_eligibility"] != "ELIGIBLE"
        or eligibility["lead_status"]
        in {"DO_NOT_CONTACT", "CONVERTED", "DISQUALIFIED", "NOT_INTERESTED"}
        or eligibility["conversion_state"] == "CONVERTED"
        or eligibility.get("qualification_status") == "DISQUALIFIED"
    ):
        return []
    buyer = _profile_for_contact(conn, contact_id)
    if not buyer:
        return []
    rows = conn.execute(
        """select * from properties where availability_status='AVAILABLE'
           and (%s is null or property_type=%s) and currency=%s
           order by updated_at desc,id limit 1000""",
        (buyer.get("property_type"), buyer.get("property_type"), buyer.get("currency") or "ETB"),
    ).fetchall()
    matches = []
    for prop in rows:
        result = match_property(buyer, prop, weights=weights, allow_near_budget=allow_near_budget)
        if result["rejected"]:
            continue
        matches.append({"property": prop, "match": result})
    matches.sort(
        key=lambda item: (
            item["match"]["score"],
            item["property"].get("updated_at") or datetime.min,
        ),
        reverse=True,
    )
    return matches[: min(max(limit, 1), 50)]


def find_matching_buyers(
    conn: Connection,
    property_id: UUID,
    *,
    limit: int = 10,
    allow_near_budget: bool = False,
    weights: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    prop = get_property(conn, property_id)
    if not prop or prop["availability_status"] != "AVAILABLE":
        return []
    candidates = conn.execute(
        """select c.id,c.name,c.phone_number,c.lead_status,c.interest_level,c.last_called_at,
                  c.next_follow_up_at,p.*,q.qualification_status,ls.score as lead_score
           from contacts c
           join lateral (select * from contact_property_interests x where x.contact_id=c.id
             order by x.updated_at desc limit 1) p on true
           left join lead_qualifications q on q.contact_id=c.id
           left join lead_scores ls on ls.contact_id=c.id
           where c.contact_eligibility='ELIGIBLE'
             and c.lead_status not in ('DO_NOT_CONTACT','CONVERTED','DISQUALIFIED','NOT_INTERESTED')
             and c.conversion_state<>'CONVERTED'
             and coalesce(q.qualification_status,'')<>'DISQUALIFIED'
             and (p.property_type=%s or p.property_type is null)
             and p.currency=%s
             and (%s='' or lower(p.preferred_location)=%s or %s=any(select lower(x) from unnest(p.accepted_locations) x))
           order by coalesce(ls.score,0) desc,c.updated_at desc,c.id limit 1000""",
        (
            prop["property_type"],
            prop["currency"],
            _normalized(prop.get("location")),
            _normalized(prop.get("location")),
            _normalized(prop.get("location")),
        ),
    ).fetchall()
    matches = []
    for buyer in candidates:
        result = match_property(buyer, prop, weights=weights, allow_near_budget=allow_near_budget)
        if result["rejected"]:
            continue
        matches.append({"buyer": buyer, "match": result})
    matches.sort(
        key=lambda item: (item["match"]["score"], item["buyer"].get("lead_score") or 0),
        reverse=True,
    )
    return matches[: min(max(limit, 1), 50)]


def save_recommendation(
    conn: Connection, contact_id: UUID, property_id: UUID, result: dict[str, Any]
) -> dict[str, Any]:
    return conn.execute(
        """insert into property_recommendations (contact_id,property_id,match_score,match_explanation)
           values (%s,%s,%s,%s) returning id""",
        (contact_id, property_id, result["score"], json.dumps(result)),
    ).fetchone()


def update_recommendation(
    conn: Connection,
    recommendation_id: UUID,
    status: str,
    decision: str | None = None,
    reason: str | None = None,
) -> bool:
    allowed = {"VIEWED", "INTERESTED", "VISIT_SCHEDULED", "REJECTED"}
    if status not in allowed:
        raise ValueError("Unsupported recommendation action")
    row = conn.execute(
        """update property_recommendations set recommendation_status=%s,user_decision=%s,
             rejection_reason=%s,updated_at=now() where id=%s returning id""",
        (status, decision, (reason or "")[:500] or None, recommendation_id),
    ).fetchone()
    return row is not None


def qualification(conn: Connection, contact_id: UUID) -> dict[str, Any]:
    row = (
        conn.execute(
            """select q.*,p.property_type,p.preferred_location,p.min_budget,p.max_budget,
                  p.expected_timeline,c.lead_status
           from contacts c left join lead_qualifications q on q.contact_id=c.id
           left join lateral (select * from contact_property_interests x where x.contact_id=c.id
             order by x.updated_at desc limit 1) p on true where c.id=%s""",
            (contact_id,),
        ).fetchone()
        or {}
    )
    values = {
        "property_requirement_known": bool(row.get("property_requirement_known"))
        or bool(row.get("property_type") or row.get("preferred_location")),
        "budget_disclosed": bool(row.get("budget_disclosed"))
        or row.get("min_budget") is not None
        or row.get("max_budget") is not None,
        "budget_declined": bool(row.get("budget_declined")),
        "timeline_confirmed": bool(row.get("timeline_confirmed") or row.get("expected_timeline")),
        "genuine_interest": bool(row.get("genuine_interest")),
        "next_action": row.get("next_action"),
        "requested_property_visit": bool(row.get("requested_property_visit")),
        "qualification_status": row.get("qualification_status"),
        "expected_timeline": row.get("expected_timeline"),
    }
    status = row.get("qualification_status") or derive_qualification(values)["status"]
    result = derive_qualification({**values, "qualification_status": status})
    return {**row, **values, **result}


def save_qualification(
    conn: Connection,
    contact_id: UUID,
    user_id: int,
    values: dict[str, Any],
    *,
    confirm_status: bool = False,
    manual_status: str | None = None,
    score_weights: dict[str, int] | None = None,
) -> dict[str, Any]:
    current = qualification(conn, contact_id)
    merged = {
        key: values.get(key, current.get(key))
        for key in (
            "property_requirement_known",
            "budget_disclosed",
            "budget_declined",
            "timeline_confirmed",
            "genuine_interest",
            "next_action",
            "requested_property_visit",
            "expected_timeline",
        )
    }
    derived = derive_qualification(merged)
    status = manual_status or (
        derived["status"] if confirm_status else current.get("qualification_status", "UNQUALIFIED")
    )
    if status not in {
        "UNQUALIFIED",
        "IN_PROGRESS",
        "QUALIFIED",
        "HIGH_PRIORITY_QUALIFIED",
        "DISQUALIFIED",
    }:
        raise ValueError("Unsupported qualification status")
    db_fields = {
        "property_requirement_known",
        "budget_disclosed",
        "budget_declined",
        "timeline_confirmed",
        "genuine_interest",
        "next_action",
        "requested_property_visit",
    }
    conn.execute(
        """insert into lead_qualifications (contact_id,property_requirement_known,budget_disclosed,
             budget_declined,timeline_confirmed,genuine_interest,next_action,requested_property_visit,
             qualification_status,updated_by,updated_at)
           values (%(contact_id)s,%(property_requirement_known)s,%(budget_disclosed)s,%(budget_declined)s,
             %(timeline_confirmed)s,%(genuine_interest)s,%(next_action)s,%(requested_property_visit)s,
             %(status)s,%(updated_by)s,now())
           on conflict (contact_id) do update set property_requirement_known=excluded.property_requirement_known,
             budget_disclosed=excluded.budget_disclosed,budget_declined=excluded.budget_declined,
             timeline_confirmed=excluded.timeline_confirmed,genuine_interest=excluded.genuine_interest,
             next_action=excluded.next_action,requested_property_visit=excluded.requested_property_visit,
             qualification_status=excluded.qualification_status,updated_by=excluded.updated_by,updated_at=now()""",
        {
            "contact_id": contact_id,
            **{key: merged[key] for key in db_fields},
            "status": status,
            "updated_by": user_id,
        },
    )
    conn.execute(
        """insert into audit_events (telegram_user_id,contact_id,event_type,new_value)
           values (%s,%s,'QUALIFICATION_UPDATED',%s)""",
        (user_id, contact_id, json.dumps({**merged, "qualification_status": status})),
    )
    recalculate_lead_score(conn, contact_id, score_weights)
    return {**merged, **derived, "status": status, "qualification_status": status}


def lead_priorities(
    conn: Connection, after_id: UUID | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    return conn.execute(
        """select c.id,c.name,c.phone_number,c.lead_status,c.interest_level,c.last_called_at,
                  c.next_follow_up_at,coalesce(ls.score,0) score,coalesce(ls.priority_category,'NEEDS_INFO') priority_category,
                  coalesce(ls.explanation,'{}'::jsonb) explanation,q.qualification_status,
                  p.property_type,p.preferred_location,p.min_budget,p.max_budget,p.currency
           from contacts c left join lead_scores ls on ls.contact_id=c.id
           left join lead_qualifications q on q.contact_id=c.id
           left join lateral (select * from contact_property_interests x where x.contact_id=c.id
             order by x.updated_at desc limit 1) p on true
           where c.contact_eligibility='ELIGIBLE'
             and c.lead_status not in ('DO_NOT_CONTACT','CONVERTED','DISQUALIFIED','NOT_INTERESTED')
             and coalesce(q.qualification_status,'')<>'DISQUALIFIED'
             and (%s::uuid is null or c.id>%s)
           order by coalesce(ls.score,0) desc,c.updated_at desc,c.id limit %s""",
        (after_id, after_id, min(max(limit, 1), 50)),
    ).fetchall()


def reporting_bounds(
    period: str,
    timezone: str,
    now: datetime | None = None,
    custom_start: date | None = None,
    custom_end: date | None = None,
) -> tuple[datetime, datetime, str, str]:
    from zoneinfo import ZoneInfo

    local_now = (now or datetime.now(ZoneInfo(timezone))).astimezone(ZoneInfo(timezone))
    today = local_now.date()
    if period == "custom":
        if custom_start is None or custom_end is None or custom_start > custom_end:
            raise ValueError("Custom report dates must be valid and ordered")
        start_day, end_day = custom_start, custom_end + timedelta(days=1)
    elif period == "yesterday":
        start_day, end_day = today - timedelta(days=1), today
    elif period == "last7":
        start_day, end_day = today - timedelta(days=6), today + timedelta(days=1)
    elif period == "last30":
        start_day, end_day = today - timedelta(days=29), today + timedelta(days=1)
    elif period == "month":
        start_day, end_day = today.replace(day=1), today + timedelta(days=1)
    elif period == "today":
        start_day, end_day = today, today + timedelta(days=1)
    else:
        raise ValueError("Unsupported reporting period")
    start = datetime.combine(start_day, datetime.min.time(), tzinfo=ZoneInfo(timezone)).astimezone(
        UTC
    )
    end = datetime.combine(end_day, datetime.min.time(), tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    return start, end, start_day.isoformat(), (end_day - timedelta(days=1)).isoformat()


def sales_intelligence_report(
    conn: Connection,
    period: str,
    timezone: str,
    *,
    custom_start: date | None = None,
    custom_end: date | None = None,
) -> dict[str, Any]:
    start, end, label_start, label_end = reporting_bounds(
        period, timezone, custom_start=custom_start, custom_end=custom_end
    )
    metrics = (
        conn.execute(
            """with calls as (
             select count(*) attempted,count(distinct contact_id) unique_contacted,
               count(*) filter (where connection_result='ANSWERED' or outcome_code='ANSWERED') answered,
               count(distinct contact_id) filter (
                 where connection_result='ANSWERED' or outcome_code='ANSWERED'
               ) answered_contacts,
               count(distinct contact_id) filter (where outcome_category='SALES_INTEREST') interested,
               count(distinct contact_id) filter (where outcome_code='MEETING_SCHEDULED') meetings,
               count(distinct contact_id) filter (where outcome_code='REQUESTED_PROPERTY_VISIT') visits,
               count(distinct contact_id) filter (where outcome_code='CONVERTED_TO_CUSTOMER') conversions
             from call_logs where created_at >= %s and created_at < %s
           ), followups as (
             select count(*) filter (where created_at >= %s and created_at < %s) scheduled,
               count(*) filter (where completed_at >= %s and completed_at < %s and status='COMPLETED') completed,
               count(*) filter (where updated_at >= %s and updated_at < %s and status='MISSED') missed,
               count(*) filter (where scheduled_at >= %s and scheduled_at < %s
                 and scheduled_at <= now() and status='PENDING') overdue
             from follow_ups
           ), qualifications as (
             select count(*) qualified from lead_qualifications
             where updated_at >= %s and updated_at < %s
               and qualification_status in ('QUALIFIED','HIGH_PRIORITY_QUALIFIED')
           ) select * from calls cross join followups cross join qualifications""",
            (start, end, start, end, start, end, start, end, start, end, start, end),
        ).fetchone()
        or {}
    )
    campaigns = conn.execute(
        """select ca.id,ca.name,count(l.id) calls,count(distinct l.contact_id) unique_contacted,
                  count(distinct l.contact_id) filter (where l.outcome_category='SALES_INTEREST') interested,
                  count(distinct q.contact_id) filter (where q.qualification_status in ('QUALIFIED','HIGH_PRIORITY_QUALIFIED')) qualified,
                  count(distinct l.contact_id) filter (where l.outcome_code='CONVERTED_TO_CUSTOMER') conversions
           from campaigns ca join call_logs l on l.campaign_id=ca.id
           left join lead_qualifications q on q.contact_id=l.contact_id and q.updated_at >= %s and q.updated_at < %s
           where l.created_at >= %s and l.created_at < %s group by ca.id,ca.name
           order by qualified desc,unique_contacted desc limit 20""",
        (start, end, start, end),
    ).fetchall()
    properties = conn.execute(
        """select p.id,p.title,p.availability_status,
                  count(r.id) recommendations,
                  count(r.id) filter (where r.recommendation_status in ('INTERESTED','VISIT_SCHEDULED')) interested,
                  count(r.id) filter (where r.recommendation_status='VISIT_SCHEDULED') visits
           from properties p left join property_recommendations r on r.property_id=p.id
             and r.recommended_at >= %s and r.recommended_at < %s
           group by p.id,p.title,p.availability_status
           order by interested desc,recommendations desc limit 20""",
        (start, end),
    ).fetchall()
    attempted = metrics.get("attempted", 0)
    answered = metrics.get("answered", 0)
    metrics.update(
        {
            "period": period,
            "start": label_start,
            "end": label_end,
            "answer_rate": round(answered * 100 / attempted, 1) if attempted else 0,
            "interest_rate": round(
                metrics.get("interested", 0) * 100 / metrics.get("answered_contacts", 0), 1
            )
            if metrics.get("answered_contacts", 0)
            else 0,
            "campaigns": campaigns,
            "properties": properties,
        }
    )
    for campaign in campaigns:
        volume = campaign.get("unique_contacted", 0)
        campaign["qualified_rate"] = (
            round(100 * campaign.get("qualified", 0) / volume, 1) if volume else 0
        )
        campaign["conversion_rate"] = (
            round(100 * campaign.get("conversions", 0) / volume, 1) if volume else 0
        )
    return metrics

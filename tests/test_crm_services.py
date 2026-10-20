from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock
from types import SimpleNamespace
from uuid import uuid4

import pytest

from realestate_crm.services import crm


class Cursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class CallConnection:
    def __init__(self, contact, duplicate=False, fail_update=False):
        self.contact = contact
        self.duplicate = duplicate
        self.fail_update = fail_update
        self.statements = []
        self.contact_update = None

    def execute(self, sql, params=()):
        normalized = " ".join(sql.lower().split())
        self.statements.append((normalized, params))
        if normalized.startswith("insert into call_logs"):
            return Cursor(row=None if self.duplicate else {"id": uuid4()})
        if normalized.startswith("select * from contacts"):
            return Cursor(row=self.contact)
        if normalized.startswith("update contacts set lead_status"):
            if self.fail_update:
                raise RuntimeError("database write failed")
            self.contact_update = params
        return Cursor()


def contact(status="NEW", eligibility="ELIGIBLE", interest="UNKNOWN", conversion="NOT_CONVERTED"):
    return {
        "id": uuid4(),
        "lead_status": status,
        "contact_eligibility": eligibility,
        "interest_level": interest,
        "conversion_state": conversion,
        "last_called_at": None,
    }


def save_result(conn, row, outcome, category="CONNECTION", update_id=1001):
    return crm.save_call_result(
        conn,
        contact_id=row["id"],
        campaign_id=None,
        session_id=None,
        user_id=77,
        update_id=update_id,
        category=category,
        outcome=outcome,
    )


def test_no_answer_records_attempt_without_erasing_interested_status():
    row = contact("INTERESTED", interest="HIGH")
    conn = CallConnection(row)

    assert save_result(conn, row, "NO_ANSWER") is True

    lead_status, eligibility, interest, conversion, last_outcome, first_at, last_at, count, _ = (
        conn.contact_update
    )
    assert (lead_status, eligibility, interest, conversion) == (
        "INTERESTED",
        "ELIGIBLE",
        "HIGH",
        "NOT_CONVERTED",
    )
    assert last_outcome == "NO_ANSWER"
    assert first_at is not None and last_at is not None
    assert count == 1
    assert any("insert into call_logs" in sql for sql, _ in conn.statements)


@pytest.mark.parametrize(
    ("outcome", "lead_status", "eligibility", "conversion"),
    [
        ("DO_NOT_CONTACT", "DO_NOT_CONTACT", "DO_NOT_CONTACT", "NOT_CONVERTED"),
        ("CONVERTED_TO_CUSTOMER", "CONVERTED", "ELIGIBLE", "CONVERTED"),
        ("INVALID_NUMBER", "DISQUALIFIED", "INVALID_NUMBER", "NOT_CONVERTED"),
    ],
)
def test_consequential_call_outcomes_update_separate_contact_fields(
    outcome, lead_status, eligibility, conversion
):
    row = contact()
    conn = CallConnection(row)

    assert save_result(conn, row, outcome) is True

    assert conn.contact_update[:4] == (lead_status, eligibility, "UNKNOWN", conversion)


def test_duplicate_callback_update_does_not_update_contact_twice():
    row = contact()
    conn = CallConnection(row, duplicate=True)

    assert save_result(conn, row, "NO_ANSWER", update_id=987) is False
    assert conn.contact_update is None
    assert [sql for sql, _ in conn.statements if sql.startswith("insert into call_logs")]
    assert not any(sql.startswith("update contacts") for sql, _ in conn.statements)


def test_concurrent_delivery_of_same_callback_records_one_call():
    row = contact()
    barrier = Barrier(2)
    lock = Lock()
    processed_update_ids = set()
    counts = {"logs": 0, "contact_updates": 0}

    class ConcurrentConnection:
        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            if normalized.startswith("insert into call_logs"):
                barrier.wait(timeout=5)
                with lock:
                    if params[-1] in processed_update_ids:
                        return Cursor()
                    processed_update_ids.add(params[-1])
                    counts["logs"] += 1
                return Cursor(row={"id": uuid4()})
            if normalized.startswith("select * from contacts"):
                return Cursor(row=row)
            if normalized.startswith("update contacts set lead_status"):
                with lock:
                    counts["contact_updates"] += 1
            return Cursor()

    def deliver():
        return save_result(ConcurrentConnection(), row, "NO_ANSWER", update_id=3001)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: deliver(), range(2)))

    assert sorted(results) == [False, True]
    assert counts == {"logs": 1, "contact_updates": 1}


def test_database_write_error_is_not_swallowed():
    row = contact()
    conn = CallConnection(row, fail_update=True)

    with pytest.raises(RuntimeError, match="database write failed"):
        save_result(conn, row, "NO_ANSWER")


def test_call_result_rejects_unknown_category_before_writing():
    row = contact()
    conn = CallConnection(row)

    with pytest.raises(ValueError, match="Unknown outcome category"):
        save_result(conn, row, "NO_ANSWER", category="UNCLASSIFIED")
    assert conn.statements == []


@pytest.mark.parametrize(
    ("status", "eligibility", "interest"),
    [
        ("CONVERTED", "ELIGIBLE", "HIGH"),
        ("DO_NOT_CONTACT", "DO_NOT_CONTACT", "MEDIUM"),
        ("NOT_INTERESTED", "ELIGIBLE", "LOW"),
        ("DISQUALIFIED", "ELIGIBLE", "UNKNOWN"),
    ],
)
def test_later_sales_outcomes_do_not_reopen_protected_status(status, eligibility, interest):
    row = contact(status=status, eligibility=eligibility, interest=interest)
    conn = CallConnection(row)

    assert save_result(conn, row, "MEETING_SCHEDULED", category="SALES_INTEREST") is True
    assert conn.contact_update[:3] == (status, eligibility, interest)


def test_qualified_outcome_sets_qualified_status_and_high_interest():
    row = contact()
    conn = CallConnection(row)

    assert save_result(conn, row, "QUALIFIED", category="SALES_INTEREST") is True
    assert conn.contact_update[0:3] == ("QUALIFIED", "ELIGIBLE", "HIGH")


class SessionConnection:
    def __init__(self, session):
        self.session = session
        self.statements = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.lower().split())
        self.statements.append((normalized, params))
        if normalized.startswith("select status from campaigns"):
            return Cursor(row={"status": "ACTIVE"})
        if normalized.startswith("select * from campaigns"):
            return Cursor(row={"status": "ACTIVE", "team_id": None})
        if normalized.startswith("select * from crm_users"):
            return Cursor(
                row={"telegram_user_id": 77, "role": "SUPER_ADMIN", "account_status": "ACTIVE"}
            )
        if normalized.startswith("select * from call_sessions"):
            return Cursor(row=self.session)
        if normalized.startswith("update call_sessions set campaign_id"):
            updated = dict(
                self.session, campaign_id=params[0], current_contact_id=None, status="ACTIVE"
            )
            return Cursor(row=updated)
        if normalized.startswith("insert into call_sessions"):
            return Cursor(row={"id": uuid4(), "campaign_id": params[1], "status": "ACTIVE"})
        return Cursor()


def test_session_resume_reuses_persisted_session_and_switches_campaign_explicitly():
    first_campaign, second_campaign = uuid4(), uuid4()
    session = {
        "id": uuid4(),
        "telegram_user_id": 77,
        "campaign_id": first_campaign,
        "current_contact_id": uuid4(),
        "status": "PAUSED",
    }
    conn = SessionConnection(session)

    resumed = crm.start_or_resume_session(conn, 77)
    assert resumed["id"] == session["id"]
    assert not any(sql.startswith("insert into call_sessions") for sql, _ in conn.statements)

    switched = crm.start_or_resume_session(conn, 77, second_campaign)
    assert switched["id"] == session["id"]
    assert switched["campaign_id"] == second_campaign
    assert switched["current_contact_id"] is None


class FollowupConnection:
    def __init__(self, previous=None, created_id=None):
        self.previous = previous or []
        self.created_id = created_id or uuid4()
        self.statements = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.lower().split())
        self.statements.append((normalized, params))
        if normalized.startswith("update follow_ups set status = 'rescheduled'"):
            return Cursor(rows=self.previous)
        if normalized.startswith("insert into follow_ups"):
            return Cursor(row={"id": self.created_id, "scheduled_at": params[3]})
        return Cursor()


def test_rescheduling_preserves_old_followup_and_links_it_to_new_one():
    old_id, contact_id, new_id = uuid4(), uuid4(), uuid4()
    scheduled = datetime(2026, 10, 20, 9, tzinfo=UTC)
    conn = FollowupConnection(previous=[{"id": old_id}], created_id=new_id)

    result = crm.schedule_followup(conn, contact_id, None, scheduled, "CALL_BACK")

    assert result["id"] == new_id
    previous_update = next(
        (params for sql, params in conn.statements if "rescheduled_to = %s" in sql), None
    )
    assert previous_update == (new_id, old_id)
    assert any(
        "set next_follow_up_at = %s, follow_up_state = 'pending'" in sql
        for sql, _ in conn.statements
    )


@pytest.mark.parametrize(
    ("next_at", "expected_state"),
    [
        (datetime.now(UTC) + timedelta(days=1), "PENDING"),
        (None, "NONE"),
    ],
)
def test_missed_followup_updates_contact_summary(next_at, expected_state):
    contact_id = uuid4()

    class MissedConnection:
        def __init__(self):
            self.updates = []

        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            if normalized.startswith("update follow_ups f set status='missed'"):
                return Cursor(row={"contact_id": contact_id})
            if normalized.startswith("select min(scheduled_at)"):
                return Cursor(row={"scheduled_at": next_at})
            if normalized.startswith("update contacts set next_follow_up_at"):
                self.updates.append(params)
            return Cursor()

    conn = MissedConnection()
    assert crm.mark_followup_missed(conn, uuid4()) is True
    assert conn.updates == [(next_at, expected_state, contact_id)]


def test_call_queue_query_is_bounded_and_excludes_ineligible_records():
    settings = SimpleNamespace(
        app_timezone="Africa/Addis_Ababa", default_daily_call_target=50, max_call_attempts=8
    )
    row = {"id": uuid4(), "campaign_id": None, "telegram_user_id": 77}

    class QueueConnection:
        def __init__(self):
            self.queries = []

        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            self.queries.append((normalized, params))
            if normalized.startswith("insert into user_settings"):
                return Cursor(
                    row={
                        "timezone": "Africa/Addis_Ababa",
                        "allow_call_during_future_followup": False,
                        "attempt_limit": 8,
                    }
                )
            return Cursor(row={"id": uuid4(), "name": "Alem"})

    conn = QueueConnection()
    assert crm._next_candidate(conn, row, settings)
    query, params = conn.queries[-1]
    assert "limit 1 for update of c skip locked" in query
    assert "c.contact_eligibility = 'eligible'" in query
    assert "'do_not_contact', 'converted', 'disqualified', 'not_interested'" in query
    assert "c.total_call_attempts < least(%s" in query
    assert "not exists (select 1 from session_contacts" in query
    assert "coalesce(q.qualification_status, '') <> 'disqualified'" in query
    assert "f.scheduled_at >= %s" in query
    assert "c.next_follow_up_at < %s" in query
    assert query.count("%s") == len(params)
    assert params[2] == 8


def test_search_uses_keyset_cursor_and_bounded_page():
    after_id = uuid4()

    class SearchConnection:
        def execute(self, sql, params=()):
            self.sql = " ".join(sql.lower().split())
            self.params = params
            return Cursor(rows=[])

    conn = SearchConnection()
    crm.search_filtered(conn, "status", "INTERESTED", 10, after_id, "Africa/Addis_Ababa")
    assert "c.id>%s" in conn.sql
    assert "order by c.id limit %s" in conn.sql
    assert "offset" not in conn.sql
    assert conn.params == ["INTERESTED", after_id, after_id, 10]


def test_export_filters_are_parameterized_and_bounded():
    campaign_id, after_id = uuid4(), uuid4()

    class ExportConnection:
        def execute(self, sql, params=()):
            self.sql = " ".join(sql.lower().split())
            self.params = params
            return Cursor(rows=[])

    conn = ExportConnection()
    crm.export_contacts(conn, "interested", campaign_id, 50000, after_id=after_id, page_size=1000)
    assert "lead_status in ('interested','qualified','negotiation')" in conn.sql
    assert "cc.campaign_id=%s" in conn.sql
    assert "c.id > %s" in conn.sql
    assert conn.sql.endswith("order by c.id limit %s")
    assert conn.params == [campaign_id, after_id, None, None, 1000]


def test_queue_export_stores_only_validated_request_fields():
    class ExportQueueConnection:
        def execute(self, sql, params=()):
            self.sql = " ".join(sql.lower().split())
            self.params = params
            return Cursor(row={"id": uuid4()})

    conn = ExportQueueConnection()
    result = crm.queue_export(
        conn,
        user_id=77,
        chat_id=77,
        export_format="csv",
        filter_name="interested",
        timezone="Africa/Addis_Ababa",
        date_from="2026-10-01",
        date_to_exclusive="2026-11-01",
    )
    assert result["id"]
    assert "insert into export_jobs" in conn.sql
    assert conn.params == (
        77,
        77,
        "csv",
        "interested",
        None,
        "2026-10-01",
        "2026-11-01",
        "Africa/Addis_Ababa",
    )

    with pytest.raises(ValueError, match="Unsupported export format"):
        crm.queue_export(
            conn,
            user_id=77,
            chat_id=77,
            export_format="json",
            filter_name="all",
            timezone="Africa/Addis_Ababa",
        )


def test_performance_report_uses_due_followups_for_completion_rate_and_counts_qualified():
    class ReportConnection:
        def execute(self, sql, params=()):
            self.sql = " ".join(sql.lower().split())
            self.params = params
            return Cursor(
                row={
                    "attempted": 10,
                    "unique_called": 8,
                    "answered": 4,
                    "no_answer": 3,
                    "interested": 2,
                    "qualified": 1,
                    "meetings": 1,
                    "visits": 0,
                    "converted": 1,
                    "scheduled": 12,
                    "due": 2,
                    "completed": 1,
                    "overdue": 3,
                }
            )

    conn = ReportConnection()
    result = crm.report(conn, "month", "Africa/Addis_Ababa")
    assert result["qualified"] == 1
    assert result["followup_completion_rate"] == 50
    assert "where f.scheduled_at >= %s and f.scheduled_at < %s" in conn.sql
    assert len(conn.params) == 16

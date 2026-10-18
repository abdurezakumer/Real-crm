from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from realestate_crm.services import team


class Cursor:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class TeamConnection:
    def __init__(self, users=None, *, contact_access=True, team_scope=True):
        self.users = users or {}
        self.contact_access = contact_access
        self.team_scope = team_scope
        self.statements = []

    def execute(self, sql, params=()):
        normalized = " ".join(sql.lower().split())
        self.statements.append((normalized, params))
        if normalized.startswith("select * from crm_users"):
            return Cursor(row=self.users.get(params[0]))
        if normalized.startswith("select 1 from crm_users"):
            return Cursor(
                row={"ok": 1}
                if params[0] in self.users and self.users[params[0]]["account_status"] == "ACTIVE"
                else None
            )
        if normalized.startswith("select allowed from crm_user_permissions"):
            return Cursor()
        if normalized.startswith("select 1 from crm_team_memberships theirs"):
            return Cursor(row={"ok": 1} if self.team_scope else None)
        if normalized.startswith("select crm_can_access_contact"):
            return Cursor(row={"allowed": self.contact_access})
        if normalized.startswith("select * from lead_assignment_jobs"):
            return Cursor(
                row={"id": params[0], "status": "PREVIEW", "team_id": uuid4(), "created_by": 99}
            )
        if normalized.startswith("select 1 from crm_teams t"):
            return Cursor(row={"ok": 1} if self.team_scope else None)
        if normalized.startswith("select assigned_telegram_user_id,ownership_restricted"):
            return Cursor(row={"assigned_telegram_user_id": 3, "ownership_restricted": False})
        if normalized.startswith("select * from lead_assignment_history"):
            return Cursor()
        return Cursor()


def user(user_id, role="SALESPERSON", status="ACTIVE"):
    return {"telegram_user_id": user_id, "role": role, "account_status": status}


def test_regular_member_cannot_promote_themselves_to_super_admin():
    conn = TeamConnection(users={88: user(88)})

    with pytest.raises(PermissionError):
        team.change_role(conn, 88, 88, "SUPER_ADMIN")

    assert not any(sql.startswith("update crm_users") for sql, _ in conn.statements)


def test_suspended_member_cannot_use_a_protected_permission():
    conn = TeamConnection(users={12: user(12, status="SUSPENDED")})

    with pytest.raises(PermissionError, match="inactive"):
        team.require_permission(conn, 12, "contacts.read_assigned")


def test_invitation_persists_only_a_hash_of_the_single_use_token():
    admin = user(77, "SUPER_ADMIN")
    invitation_id = uuid4()

    class InvitationConnection(TeamConnection):
        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            self.statements.append((normalized, params))
            if normalized.startswith("select * from crm_users"):
                return Cursor(row=self.users.get(params[0]))
            if normalized.startswith("select allowed from crm_user_permissions"):
                return Cursor()
            if normalized.startswith("insert into crm_invitations"):
                return Cursor(
                    row={
                        "id": invitation_id,
                        "role": params[1],
                        "team_id": params[2],
                        "expires_at": datetime.now(UTC) + timedelta(days=3),
                    }
                )
            return Cursor()

    conn = InvitationConnection(users={77: admin})
    row, token = team.create_invitation(conn, 77, "SALESPERSON")

    insert = next(
        params for sql, params in conn.statements if sql.startswith("insert into crm_invitations")
    )
    assert row["id"] == invitation_id
    assert len(token) >= 40
    assert insert[0] != token
    assert insert[0] == team.hashlib.sha256(token.encode("ascii")).hexdigest()


def test_expired_invitation_is_marked_expired_and_cannot_be_accepted():
    invitation = {
        "id": uuid4(),
        "status": "PENDING",
        "expires_at": datetime.now(UTC) - timedelta(seconds=1),
    }

    class ExpiredInvitationConnection(TeamConnection):
        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            self.statements.append((normalized, params))
            if normalized.startswith("select * from crm_invitations"):
                return Cursor(row=invitation)
            return Cursor()

    conn = ExpiredInvitationConnection()
    with pytest.raises(ValueError, match="invalid or has expired"):
        team.accept_invitation(conn, "a" * 40, 123)

    assert any(
        sql.startswith("update crm_invitations set status='expired'") for sql, _ in conn.statements
    )


def test_used_invitation_token_cannot_be_reused():
    class UsedInvitationConnection(TeamConnection):
        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            self.statements.append((normalized, params))
            if normalized.startswith("select * from crm_invitations"):
                return Cursor(row={"id": uuid4(), "status": "ACCEPTED"})
            return Cursor()

    with pytest.raises(ValueError, match="already been used"):
        team.accept_invitation(UsedInvitationConnection(), "a" * 40, 123)


def test_manager_cannot_reassign_a_lead_outside_contact_scope():
    conn = TeamConnection(
        users={1: user(1, "SALES_MANAGER"), 2: user(2)},
        contact_access=False,
        team_scope=True,
    )

    with pytest.raises(PermissionError, match="not assigned to you"):
        team.assign_contact(conn, 1, uuid4(), 2, reason="Workload redistribution")

    assert not any(
        sql.startswith("update contacts set assigned_telegram_user_id")
        for sql, _ in conn.statements
    )


def test_manager_cannot_confirm_an_assignment_preview_for_another_team():
    conn = TeamConnection(
        users={1: user(1, "SALES_MANAGER")},
        team_scope=False,
    )

    with pytest.raises(PermissionError, match="outside your authorized team scope"):
        team.apply_distribution(conn, 1, uuid4())

    assert not any(sql.startswith("update contacts") for sql, _ in conn.statements)


def test_non_ascii_invitation_token_is_rejected_before_database_access():
    conn = TeamConnection()

    with pytest.raises(ValueError, match="invalid or has expired"):
        team.accept_invitation(conn, "abcé123", 123)

    assert conn.statements == []


def test_team_capacity_does_not_accept_boolean_as_integer():
    conn = TeamConnection(users={77: user(77, "SUPER_ADMIN")})

    with pytest.raises(ValueError, match="capacity"):
        team.update_team_settings(conn, 77, {"max_active_leads_per_salesperson": True})

    assert not any(sql.startswith("update crm_team_settings") for sql, _ in conn.statements)


def test_assignment_idempotency_key_cannot_be_replayed_by_another_actor():
    contact_id = uuid4()

    class ReplayConnection(TeamConnection):
        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            self.statements.append((normalized, params))
            if normalized.startswith("select * from crm_users"):
                return Cursor(row=self.users.get(params[0]))
            if normalized.startswith("select * from lead_assignment_history"):
                return Cursor(
                    row={
                        "actor_telegram_user_id": 1,
                        "contact_id": contact_id,
                        "previous_owner_telegram_user_id": 2,
                        "new_owner_telegram_user_id": 3,
                    }
                )
            return Cursor()

    conn = ReplayConnection(
        users={1: user(1, "SUPER_ADMIN"), 2: user(2, "SUPER_ADMIN"), 3: user(3)}
    )
    with pytest.raises(PermissionError, match="belongs to another user"):
        team.assign_contact(
            conn,
            2,
            contact_id,
            3,
            reason="Workload redistribution",
            idempotency_key="shared-key",
        )


def test_campaign_calling_rules_reject_boolean_priority_as_integer():
    conn = TeamConnection(users={77: user(77, "SUPER_ADMIN")})

    with pytest.raises(ValueError, match="priority"):
        team.update_campaign_calling_rules(
            conn,
            77,
            uuid4(),
            starts_at=None,
            ends_at=None,
            calling_priority=True,
            max_call_attempts=8,
            retry_after_hours=24,
            performance_target=None,
        )

    assert not any(sql.startswith("update campaigns") for sql, _ in conn.statements)


def test_distribution_idempotency_key_cannot_replay_another_users_preview():
    team_id = uuid4()

    class ExistingJobConnection(TeamConnection):
        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            self.statements.append((normalized, params))
            if normalized.startswith("select * from crm_users"):
                return Cursor(row=self.users.get(params[0]))
            if normalized.startswith("select allowed from crm_user_permissions"):
                return Cursor()
            if normalized.startswith("select * from lead_assignment_jobs"):
                return Cursor(row={"created_by": 2, "team_id": team_id, "status": "PREVIEW"})
            return Cursor()

    conn = ExistingJobConnection(users={1: user(1, "SUPER_ADMIN")})
    with pytest.raises(PermissionError, match="belongs to another user"):
        team.preview_distribution(
            conn,
            1,
            team_id,
            idempotency_key="shared-preview-key",
        )


def test_distribution_idempotency_key_cannot_replay_another_teams_preview():
    team_id = uuid4()

    class ExistingJobConnection(TeamConnection):
        def execute(self, sql, params=()):
            normalized = " ".join(sql.lower().split())
            self.statements.append((normalized, params))
            if normalized.startswith("select * from crm_users"):
                return Cursor(row=self.users.get(params[0]))
            if normalized.startswith("select allowed from crm_user_permissions"):
                return Cursor()
            if normalized.startswith("select * from lead_assignment_jobs"):
                return Cursor(row={"created_by": 1, "team_id": uuid4(), "status": "PREVIEW"})
            return Cursor()

    conn = ExistingJobConnection(users={1: user(1, "SUPER_ADMIN")})
    with pytest.raises(ValueError, match="another team"):
        team.preview_distribution(
            conn,
            1,
            team_id,
            idempotency_key="shared-preview-key",
        )

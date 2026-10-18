"""Team accounts, permissions, invitations, assignments, and team goals."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from psycopg import Connection

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "SUPER_ADMIN": frozenset({"*"}),
    "SALES_MANAGER": frozenset(
        {
            "contacts.read_team",
            "contacts.manage_team",
            "leads.assign",
            "leads.distribute",
            "campaigns.call",
            "campaigns.team",
            "reports.team",
            "targets.manage_team",
            "audit.read_team",
            "members.read_team",
        }
    ),
    "SALESPERSON": frozenset(
        {
            "contacts.read_assigned",
            "contacts.manage_assigned",
            "campaigns.call",
            "calls.log",
            "followups.manage_assigned",
            "opportunities.manage_assigned",
            "reports.self",
            "targets.self",
            "commissions.self",
            "handover.request",
        }
    ),
    "MARKETING_ADMIN": frozenset(
        {"campaigns.manage", "imports.manage", "reports.campaign", "leads.distribute.preview"}
    ),
    "OBSERVER": frozenset({"contacts.read_team", "reports.team", "members.read_team"}),
    "CUSTOM": frozenset(),
}
ROLES = frozenset(ROLE_PERMISSIONS)
ACTIVE_LEAD_STATUSES = ("DO_NOT_CONTACT", "NOT_INTERESTED", "DISQUALIFIED", "CONVERTED")


def _audit(
    conn: Connection,
    actor_id: int,
    event_type: str,
    entity_type: str,
    *,
    entity_id: UUID | None = None,
    old_value: Any = None,
    new_value: Any = None,
    reason: str | None = None,
    request_key: str | None = None,
) -> None:
    conn.execute(
        """insert into audit_events(telegram_user_id,event_type,old_value,new_value,
             entity_type,entity_id,actor_telegram_user_id,reason,request_key)
           values (%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s)
           on conflict(request_key,event_type) where request_key is not null do nothing""",
        (
            actor_id,
            event_type,
            json.dumps(old_value, ensure_ascii=False, default=str)
            if old_value is not None
            else None,
            json.dumps(new_value, ensure_ascii=False, default=str)
            if new_value is not None
            else None,
            entity_type,
            entity_id,
            actor_id,
            reason,
            request_key,
        ),
    )


def ensure_bootstrap_admin(
    conn: Connection, user_id: int, display_name: str = ""
) -> dict[str, Any]:
    """Create a configured bootstrap administrator once; never promote an existing account here."""
    return conn.execute(
        """insert into crm_users(telegram_user_id,display_name,role,account_status,can_receive_leads)
           values (%s,%s,'SUPER_ADMIN','ACTIVE',false)
           on conflict(telegram_user_id) do update set
             display_name=case when crm_users.display_name='' then excluded.display_name
                               else crm_users.display_name end,
             updated_at=case when crm_users.display_name='' then now() else crm_users.updated_at end
           returning *""",
        (user_id, display_name[:120]),
    ).fetchone()


def bootstrap_legacy_ownership(conn: Connection, user_id: int, limit: int = 500) -> int:
    """Assign legacy unowned contacts in small, retry-safe batches to the original owner."""
    rows = conn.execute(
        """with batch as (
             select id from contacts where assigned_telegram_user_id is null
             order by id limit %s for update skip locked
           ), assigned as (
             update contacts c set assigned_telegram_user_id=%s,assigned_at=now(),
               assignment_source='BOOTSTRAP',updated_at=now()
             from batch b where c.id=b.id returning c.id
           ) insert into lead_assignment_history(contact_id,new_owner_telegram_user_id,
               actor_telegram_user_id,assignment_source,reason,idempotency_key)
             select id,%s,%s,'BOOTSTRAP','Existing contact assigned to the primary configured owner',
               'bootstrap:'||id::text from assigned on conflict(idempotency_key) do nothing
             returning contact_id""",
        (max(1, min(limit, 500)), user_id, user_id, user_id),
    ).fetchall()
    return len(rows)


def get_user(conn: Connection, user_id: int) -> dict[str, Any] | None:
    return conn.execute("select * from crm_users where telegram_user_id=%s", (user_id,)).fetchone()


def user_is_active(conn: Connection, user_id: int) -> bool:
    row = conn.execute(
        "select 1 from crm_users where telegram_user_id=%s and account_status='ACTIVE'",
        (user_id,),
    ).fetchone()
    return bool(row)


def permission_allowed(conn: Connection, user_id: int, permission: str) -> bool:
    user = get_user(conn, user_id)
    if not user or user["account_status"] != "ACTIVE":
        return False
    override = conn.execute(
        "select allowed from crm_user_permissions where telegram_user_id=%s and permission_code=%s",
        (user_id, permission),
    ).fetchone()
    if override:
        return bool(override["allowed"])
    permissions = ROLE_PERMISSIONS.get(user["role"], frozenset())
    return "*" in permissions or permission in permissions


def require_permission(conn: Connection, user_id: int, permission: str) -> dict[str, Any]:
    user = get_user(conn, user_id)
    if not user or user["account_status"] != "ACTIVE":
        raise PermissionError("This CRM account is inactive. Contact the administrator.")
    if not permission_allowed(conn, user_id, permission):
        raise PermissionError("You do not have permission to do that.")
    return user


def grant_permission(
    conn: Connection, actor_id: int, target_id: int, permission: str, allowed: bool
) -> None:
    require_permission(conn, actor_id, "users.manage")
    target = get_user(conn, target_id)
    if not target:
        raise ValueError("Team member was not found")
    if target["role"] == "SUPER_ADMIN":
        raise ValueError(
            "Super administrator permissions are managed through bootstrap configuration"
        )
    if (
        not permission
        or len(permission) > 80
        or not permission.replace(".", "").replace("_", "").isalnum()
    ):
        raise ValueError("Permission code is invalid")
    conn.execute(
        """insert into crm_user_permissions(telegram_user_id,permission_code,allowed,updated_by)
           values (%s,%s,%s,%s) on conflict(telegram_user_id,permission_code) do update
           set allowed=excluded.allowed,updated_by=excluded.updated_by,updated_at=now()""",
        (target_id, permission, allowed, actor_id),
    )
    _audit(
        conn,
        actor_id,
        "TEAM_PERMISSION_CHANGED",
        "CRM_USER",
        old_value={"telegram_user_id": target_id},
        new_value={"telegram_user_id": target_id, "permission": permission, "allowed": allowed},
    )


def create_invitation(
    conn: Connection,
    actor_id: int,
    role: str,
    *,
    team_id: UUID | None = None,
    expires_in: timedelta = timedelta(days=3),
) -> tuple[dict[str, Any], str]:
    require_permission(conn, actor_id, "users.manage")
    role = role.strip().upper()
    if role not in ROLES - {"SUPER_ADMIN"}:
        raise ValueError("Choose an inviteable role; super administrators must be bootstrapped")
    if expires_in <= timedelta(0) or expires_in > timedelta(days=14):
        raise ValueError("Invitation expiry must be between 1 minute and 14 days")
    if team_id:
        team = conn.execute("select active from crm_teams where id=%s", (team_id,)).fetchone()
        if not team or not team["active"]:
            raise ValueError("Choose an active team")
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    row = conn.execute(
        """insert into crm_invitations(token_hash,role,team_id,issued_by,expires_at)
           values (%s,%s,%s,%s,now()+%s) returning id,role,team_id,created_at,expires_at,status""",
        (digest, role, team_id, actor_id, expires_in),
    ).fetchone()
    _audit(
        conn,
        actor_id,
        "TEAM_INVITATION_CREATED",
        "INVITATION",
        entity_id=row["id"],
        new_value={"role": role, "team_id": team_id, "expires_at": row["expires_at"]},
    )
    return row, token


def accept_invitation(
    conn: Connection, token: str, telegram_user_id: int, display_name: str = ""
) -> dict[str, Any]:
    if (
        not token
        or len(token) > 128
        or not token.isascii()
        or not token.replace("_", "").replace("-", "").isalnum()
    ):
        raise ValueError("Invitation is invalid or has expired")
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    invitation = conn.execute(
        """select * from crm_invitations where token_hash=%s for update""", (digest,)
    ).fetchone()
    if not invitation:
        raise ValueError("Invitation is invalid or has expired")
    if invitation["status"] == "PENDING" and invitation["expires_at"] <= datetime.now(UTC):
        conn.execute("update crm_invitations set status='EXPIRED' where id=%s", (invitation["id"],))
        raise ValueError("Invitation is invalid or has expired")
    if invitation["status"] != "PENDING":
        raise ValueError("Invitation has already been used or revoked")
    existing = get_user(conn, telegram_user_id)
    if existing:
        raise ValueError("This Telegram account already has a CRM account")
    team_id = invitation["team_id"]
    user = conn.execute(
        """insert into crm_users(telegram_user_id,display_name,role,account_status,can_receive_leads,invited_by)
           values (%s,%s,%s,'ACTIVE',%s,%s) returning *""",
        (
            telegram_user_id,
            display_name[:120],
            invitation["role"],
            invitation["role"] == "SALESPERSON",
            invitation["issued_by"],
        ),
    ).fetchone()
    conn.execute(
        """insert into user_settings(telegram_user_id,timezone,daily_call_target,attempt_limit)
           select %s,timezone,daily_call_target,attempt_limit from user_settings
           where telegram_user_id=%s on conflict(telegram_user_id) do nothing""",
        (telegram_user_id, invitation["issued_by"]),
    )
    if team_id:
        conn.execute(
            """insert into crm_team_memberships(team_id,telegram_user_id,is_default,added_by)
               values (%s,%s,true,%s) on conflict(team_id,telegram_user_id) do update
               set active=true,is_default=true,added_by=excluded.added_by""",
            (team_id, telegram_user_id, invitation["issued_by"]),
        )
    conn.execute(
        """update crm_invitations set status='ACCEPTED',accepted_at=now(),
             accepted_telegram_user_id=%s where id=%s and status='PENDING'""",
        (telegram_user_id, invitation["id"]),
    )
    _audit(
        conn,
        telegram_user_id,
        "TEAM_INVITATION_ACCEPTED",
        "INVITATION",
        entity_id=invitation["id"],
        new_value={"telegram_user_id": telegram_user_id, "role": invitation["role"]},
    )
    return user


def list_invitations(conn: Connection, actor_id: int, limit: int = 20) -> list[dict[str, Any]]:
    require_permission(conn, actor_id, "users.manage")
    return conn.execute(
        """select i.id,i.role,i.team_id,t.name team_name,i.issued_by,i.created_at,i.expires_at,
                  i.status,i.accepted_at,i.accepted_telegram_user_id
           from crm_invitations i left join crm_teams t on t.id=i.team_id
           order by i.created_at desc limit %s""",
        (max(1, min(limit, 50)),),
    ).fetchall()


def revoke_invitation(conn: Connection, actor_id: int, invitation_id: UUID) -> None:
    require_permission(conn, actor_id, "users.manage")
    row = conn.execute(
        """update crm_invitations set status='REVOKED' where id=%s and status='PENDING'
           returning id""",
        (invitation_id,),
    ).fetchone()
    if not row:
        raise ValueError("Invitation is no longer pending")
    _audit(conn, actor_id, "TEAM_INVITATION_REVOKED", "INVITATION", entity_id=invitation_id)


def change_role(conn: Connection, actor_id: int, target_id: int, role: str) -> dict[str, Any]:
    require_permission(conn, actor_id, "users.manage")
    role = role.strip().upper()
    if role not in ROLES - {"SUPER_ADMIN"}:
        raise ValueError("Choose a supported non-administrator role")
    target = get_user(conn, target_id)
    if not target or target["role"] == "SUPER_ADMIN":
        raise ValueError("The bootstrap administrator role cannot be changed here")
    result = conn.execute(
        """update crm_users set role=%s,can_receive_leads=(%s='SALESPERSON'),updated_at=now()
           where telegram_user_id=%s returning *""",
        (role, role, target_id),
    ).fetchone()
    _audit(
        conn,
        actor_id,
        "TEAM_MEMBER_ROLE_CHANGED",
        "CRM_USER",
        old_value={"telegram_user_id": target_id, "role": target["role"]},
        new_value={"telegram_user_id": target_id, "role": role},
    )
    return result


def set_member_status(
    conn: Connection,
    actor_id: int,
    target_id: int,
    status: str,
    *,
    lead_handling: str | None = None,
    transfer_to: int | None = None,
) -> dict[str, Any]:
    require_permission(conn, actor_id, "users.manage")
    status = status.strip().upper()
    if status not in {"ACTIVE", "SUSPENDED"}:
        raise ValueError("Choose active or suspended")
    target = get_user(conn, target_id)
    if not target or target["role"] == "SUPER_ADMIN":
        raise ValueError("The bootstrap administrator cannot be suspended here")
    if status == "SUSPENDED":
        if lead_handling not in {"KEEP", "TRANSFER"}:
            raise ValueError("Choose whether assigned leads remain temporarily or are transferred")
        active_ids = conn.execute(
            """select c.id from contacts c where c.assigned_telegram_user_id=%s
                 and c.contact_eligibility='ELIGIBLE' and c.conversion_state<>'CONVERTED'
                 and c.lead_status not in ('DO_NOT_CONTACT','NOT_INTERESTED','DISQUALIFIED','CONVERTED')
               order by c.id limit 501""",
            (target_id,),
        ).fetchall()
        if lead_handling == "TRANSFER":
            if not transfer_to:
                raise ValueError("Choose a receiving salesperson before transferring leads")
            require_user_in_scope(conn, actor_id, transfer_to)
            if len(active_ids) > 500:
                raise ValueError(
                    "This member has over 500 active opportunities; use bounded assignment batches first"
                )
            for item in active_ids:
                assign_contact(
                    conn,
                    actor_id,
                    item["id"],
                    transfer_to,
                    reason="Member suspension handover",
                    source="HANDOVER",
                    idempotency_key=f"suspend:{target_id}:{item['id']}",
                    override_active_deal=get_user(conn, actor_id)["role"] == "SUPER_ADMIN",
                )
        elif lead_handling == "KEEP" and len(active_ids) > 500:
            # Ownership is unchanged; this only confirms the count for the admin workflow.
            pass
    result = conn.execute(
        """update crm_users set account_status=%s,
             can_receive_leads=case when %s='SUSPENDED' then false else role='SALESPERSON' end,
             updated_at=now() where telegram_user_id=%s returning *""",
        (status, status, target_id),
    ).fetchone()
    _audit(
        conn,
        actor_id,
        "TEAM_MEMBER_SUSPENDED" if status == "SUSPENDED" else "TEAM_MEMBER_REACTIVATED",
        "CRM_USER",
        old_value={"telegram_user_id": target_id, "status": target["account_status"]},
        new_value={"telegram_user_id": target_id, "status": status, "lead_handling": lead_handling},
    )
    return result


def create_team(
    conn: Connection, actor_id: int, name: str, description: str | None = None
) -> dict[str, Any]:
    require_permission(conn, actor_id, "teams.manage")
    name = name.strip()
    if not name or len(name) > 100 or (description and len(description) > 1000):
        raise ValueError("Team name or description is invalid")
    row = conn.execute(
        """insert into crm_teams(name,description,created_by) values (%s,%s,%s)
           returning *""",
        (name, description.strip() if description else None, actor_id),
    ).fetchone()
    _audit(conn, actor_id, "TEAM_CREATED", "TEAM", entity_id=row["id"], new_value={"name": name})
    return row


def rename_team(
    conn: Connection, actor_id: int, team_id: UUID, name: str, description: str | None = None
) -> dict[str, Any]:
    require_permission(conn, actor_id, "teams.manage")
    name = name.strip()
    if not name or len(name) > 100 or (description and len(description) > 1000):
        raise ValueError("Team name or description is invalid")
    old = conn.execute("select * from crm_teams where id=%s for update", (team_id,)).fetchone()
    if not old:
        raise ValueError("Team was not found")
    result = conn.execute(
        """update crm_teams set name=%s,description=coalesce(%s,description),updated_at=now()
           where id=%s returning *""",
        (name, description.strip() if description else None, team_id),
    ).fetchone()
    _audit(
        conn,
        actor_id,
        "TEAM_RENAMED",
        "TEAM",
        entity_id=team_id,
        old_value={"name": old["name"], "description": old["description"]},
        new_value={
            "name": name,
            "description": description.strip() if description else old["description"],
        },
    )
    return result


def get_team_settings(conn: Connection, actor_id: int) -> dict[str, Any]:
    require_permission(conn, actor_id, "users.manage")
    row = conn.execute("select * from crm_team_settings where singleton").fetchone()
    if not row:
        raise ValueError("Team settings are unavailable")
    return row


def list_audit_events(
    conn: Connection,
    actor_id: int,
    *,
    limit: int = 20,
    before_id: UUID | None = None,
    subject_user_id: int | None = None,
) -> list[dict[str, Any]]:
    actor = require_permission(conn, actor_id, "audit.read_team")
    params: list[Any] = []
    scope = (
        "true"
        if actor["role"] == "SUPER_ADMIN"
        else """(
        (coalesce(e.contact_id,case when e.entity_type='CONTACT' then e.entity_id end) is not null
          and crm_can_access_contact(
            coalesce(e.contact_id,case when e.entity_type='CONTACT' then e.entity_id end),%s))
        or (e.entity_type='CRM_USER' and e.new_value->>'telegram_user_id' ~ '^[0-9]+$'
          and exists(select 1 from crm_team_memberships mine
            join crm_team_memberships theirs on theirs.team_id=mine.team_id and theirs.active
            join crm_teams t on t.id=mine.team_id and t.active
            where mine.telegram_user_id=%s and mine.active
              and theirs.telegram_user_id=(e.new_value->>'telegram_user_id')::bigint
              and (t.manager_telegram_user_id=%s or exists(select 1 from crm_team_memberships mm
                where mm.team_id=t.id and mm.telegram_user_id=%s and mm.active))))"""
    )
    if actor["role"] != "SUPER_ADMIN":
        params.extend([actor_id, actor_id, actor_id, actor_id])
    if subject_user_id is not None:
        require_member_scope(conn, actor_id, subject_user_id)
        scope += " and coalesce(e.actor_telegram_user_id,e.telegram_user_id)=%s"
        params.append(subject_user_id)
    if before_id:
        scope += " and (e.created_at,e.id) < (select created_at,id from audit_events where id=%s)"
        params.append(before_id)
    params.append(max(1, min(limit, 50)))
    return conn.execute(
        f"""select e.id,e.event_type,e.entity_type,e.entity_id,e.reason,e.old_value,e.new_value,
                  e.created_at,coalesce(e.actor_telegram_user_id,e.telegram_user_id) actor_id,
                  u.display_name actor_name,c.name contact_name
           from audit_events e left join crm_users u
             on u.telegram_user_id=coalesce(e.actor_telegram_user_id,e.telegram_user_id)
           left join contacts c on c.id=coalesce(e.contact_id,
             case when e.entity_type='CONTACT' then e.entity_id end)
           where {scope} order by e.created_at desc,e.id desc limit %s""",
        tuple(params),
    ).fetchall()


def update_team_settings(
    conn: Connection, actor_id: int, changes: dict[str, Any]
) -> dict[str, Any]:
    require_permission(conn, actor_id, "users.manage")
    allowed = {
        "max_active_leads_per_salesperson": int,
        "unassigned_visible_to_salespeople": bool,
        "salespeople_can_request_reassignment": bool,
        "manager_can_override_ownership": bool,
        "auto_reassignment_enabled": bool,
        "active_negotiation_requires_super_admin": bool,
        "default_distribution_method": str,
    }
    if not changes or set(changes) - set(allowed):
        raise ValueError("One or more team settings are unsupported")
    for key, value in changes.items():
        if allowed[key] is bool and type(value) is not bool:
            raise ValueError("Boolean team settings must use true or false")
        if allowed[key] is int and (type(value) is not int or not 1 <= value <= 100000):
            raise ValueError("Lead capacity must be between 1 and 100000")
        if key == "default_distribution_method" and value not in {
            "ROUND_ROBIN",
            "LOWEST_WORKLOAD",
            "MANUAL",
        }:
            raise ValueError("Choose a supported lead distribution method")
    old = get_team_settings(conn, actor_id)
    assignments = ",".join(f"{key}=%s" for key in changes)
    values = tuple(changes.values())
    result = conn.execute(
        f"""update crm_team_settings set {assignments},updated_by=%s,updated_at=now()
           where singleton returning *""",
        (*values, actor_id),
    ).fetchone()
    _audit(
        conn,
        actor_id,
        "TEAM_SETTINGS_CHANGED",
        "TEAM_SETTINGS",
        old_value={key: old[key] for key in changes},
        new_value={key: result[key] for key in changes},
    )
    return result


def set_team_active(conn: Connection, actor_id: int, team_id: UUID, active: bool) -> None:
    require_permission(conn, actor_id, "teams.manage")
    team = conn.execute("select id from crm_teams where id=%s for update", (team_id,)).fetchone()
    if not team:
        raise ValueError("Team was not found")
    if not active:
        owned = conn.execute(
            """select 1 from contacts c join crm_team_memberships m
                 on m.telegram_user_id=c.assigned_telegram_user_id and m.team_id=%s and m.active
               where c.contact_eligibility='ELIGIBLE' and c.conversion_state<>'CONVERTED'
                 and c.lead_status not in ('DO_NOT_CONTACT','NOT_INTERESTED','DISQUALIFIED','CONVERTED')
               limit 1""",
            (team_id,),
        ).fetchone()
        if owned:
            raise ValueError("Reassign active leads before deactivating this team")
    conn.execute("update crm_teams set active=%s,updated_at=now() where id=%s", (active, team_id))
    if not active:
        conn.execute("update crm_team_memberships set active=false where team_id=%s", (team_id,))
    else:
        conn.execute(
            """update crm_team_memberships m set active=true from crm_users u
               where m.telegram_user_id=u.telegram_user_id and m.team_id=%s and u.account_status='ACTIVE'""",
            (team_id,),
        )
    _audit(
        conn,
        actor_id,
        "TEAM_ACTIVATED" if active else "TEAM_DEACTIVATED",
        "TEAM",
        entity_id=team_id,
        new_value={"active": active},
    )


def set_team_manager(
    conn: Connection, actor_id: int, team_id: UUID, manager_id: int | None
) -> None:
    require_permission(conn, actor_id, "teams.manage")
    if manager_id is not None:
        manager = get_user(conn, manager_id)
        if (
            not manager
            or manager["account_status"] != "ACTIVE"
            or manager["role"] != "SALES_MANAGER"
        ):
            raise ValueError("Choose an active sales manager")
        team = conn.execute("select active from crm_teams where id=%s", (team_id,)).fetchone()
        if not team or not team["active"]:
            raise ValueError("Choose an active team")
        conn.execute(
            """insert into crm_team_memberships(team_id,telegram_user_id,is_default,added_by)
               values (%s,%s,false,%s) on conflict(team_id,telegram_user_id) do update
               set active=true,added_by=excluded.added_by""",
            (team_id, manager_id, actor_id),
        )
    result = conn.execute(
        "update crm_teams set manager_telegram_user_id=%s,updated_at=now() where id=%s returning id",
        (manager_id, team_id),
    ).fetchone()
    if not result:
        raise ValueError("Team was not found")
    _audit(
        conn,
        actor_id,
        "TEAM_MANAGER_CHANGED",
        "TEAM",
        entity_id=team_id,
        new_value={"manager_telegram_user_id": manager_id},
    )


def add_team_member(
    conn: Connection,
    actor_id: int,
    team_id: UUID,
    telegram_user_id: int,
    *,
    make_default: bool = False,
) -> None:
    require_permission(conn, actor_id, "teams.manage")
    user = get_user(conn, telegram_user_id)
    team = conn.execute("select active from crm_teams where id=%s", (team_id,)).fetchone()
    if not user or user["account_status"] != "ACTIVE" or not team or not team["active"]:
        raise ValueError("Choose an active user and active team")
    if make_default:
        conn.execute(
            "update crm_team_memberships set is_default=false where telegram_user_id=%s",
            (telegram_user_id,),
        )
    conn.execute(
        """insert into crm_team_memberships(team_id,telegram_user_id,is_default,added_by)
           values (%s,%s,%s,%s) on conflict(team_id,telegram_user_id) do update
           set active=true,is_default=excluded.is_default,added_by=excluded.added_by""",
        (team_id, telegram_user_id, make_default, actor_id),
    )
    _audit(
        conn,
        actor_id,
        "TEAM_MEMBER_ADDED",
        "TEAM",
        entity_id=team_id,
        new_value={"telegram_user_id": telegram_user_id, "is_default": make_default},
    )


def list_teams(conn: Connection, actor_id: int) -> list[dict[str, Any]]:
    require_permission(conn, actor_id, "members.read_team")
    user = get_user(conn, actor_id)
    if user["role"] == "SUPER_ADMIN":
        return conn.execute(
            """select t.*,u.display_name manager_name,
                      count(m.telegram_user_id) filter(where m.active) member_count
               from crm_teams t left join crm_users u on u.telegram_user_id=t.manager_telegram_user_id
               left join crm_team_memberships m on m.team_id=t.id group by t.id,u.display_name
               order by t.name"""
        ).fetchall()
    return conn.execute(
        """select t.*,u.display_name manager_name,
                  count(m.telegram_user_id) filter(where m.active) member_count
           from crm_teams t join crm_team_memberships mine on mine.team_id=t.id
             and mine.telegram_user_id=%s and mine.active
           left join crm_users u on u.telegram_user_id=t.manager_telegram_user_id
           left join crm_team_memberships m on m.team_id=t.id group by t.id,u.display_name
           order by t.name""",
        (actor_id,),
    ).fetchall()


def list_members(conn: Connection, actor_id: int, limit: int = 50) -> list[dict[str, Any]]:
    require_permission(conn, actor_id, "members.read_team")
    actor = get_user(conn, actor_id)
    if actor["role"] == "SUPER_ADMIN":
        return conn.execute(
            """select u.telegram_user_id,u.display_name,u.role,u.account_status,u.can_receive_leads,
                      array_remove(array_agg(t.name),null) team_names,
                      count(distinct c.id) filter(where c.assigned_telegram_user_id=u.telegram_user_id) assigned_leads
               from crm_users u left join crm_team_memberships m on m.telegram_user_id=u.telegram_user_id and m.active
               left join crm_teams t on t.id=m.team_id left join contacts c on c.assigned_telegram_user_id=u.telegram_user_id
               group by u.telegram_user_id order by u.created_at desc limit %s""",
            (max(1, min(limit, 100)),),
        ).fetchall()
    if actor["role"] == "SALES_MANAGER":
        return conn.execute(
            """select distinct u.telegram_user_id,u.display_name,u.role,u.account_status,u.can_receive_leads,
                      count(distinct c.id) filter(where c.assigned_telegram_user_id=u.telegram_user_id) assigned_leads
               from crm_teams t join crm_team_memberships mine on mine.team_id=t.id
                 and mine.telegram_user_id=%s and mine.active
               join crm_team_memberships m on m.team_id=t.id and m.active
               join crm_users u on u.telegram_user_id=m.telegram_user_id
               left join contacts c on c.assigned_telegram_user_id=u.telegram_user_id
               group by u.telegram_user_id order by u.display_name limit %s""",
            (actor_id, max(1, min(limit, 100))),
        ).fetchall()
    return conn.execute(
        """select telegram_user_id,display_name,role,account_status,can_receive_leads
           from crm_users where telegram_user_id=%s""",
        (actor_id,),
    ).fetchall()


def require_member_scope(conn: Connection, actor_id: int, target_id: int) -> None:
    actor = require_permission(conn, actor_id, "members.read_team")
    if actor["role"] == "SUPER_ADMIN" or target_id == actor_id:
        return
    allowed = conn.execute(
        """select 1 from crm_team_memberships mine
           join crm_teams t on t.id=mine.team_id and t.active
           join crm_team_memberships theirs on theirs.team_id=t.id and theirs.active
           where mine.telegram_user_id=%s and mine.active and theirs.telegram_user_id=%s
             and (t.manager_telegram_user_id=%s or %s in ('SALES_MANAGER','OBSERVER','CUSTOM')) limit 1""",
        (actor_id, target_id, actor_id, actor["role"]),
    ).fetchone()
    if not allowed:
        raise PermissionError("This member is outside your authorized team scope")


def can_access_contact(conn: Connection, actor_id: int, contact_id: UUID) -> bool:
    return bool(
        conn.execute(
            "select crm_can_access_contact(%s,%s) allowed", (contact_id, actor_id)
        ).fetchone()["allowed"]
    )


def require_contact_access(conn: Connection, actor_id: int, contact_id: UUID) -> None:
    if not can_access_contact(conn, actor_id, contact_id):
        raise PermissionError("This lead is not assigned to you or your authorized team.")


def _record_contact_ids(conn: Connection, record_ids: list[UUID]) -> list[UUID]:
    if not record_ids:
        return []
    rows = conn.execute(
        """with ids as (select unnest(%s::uuid[]) id), found as (
             select c.id contact_id from contacts c join ids i on i.id=c.id
             union all select l.contact_id from call_logs l join ids i on i.id=l.id
             union all select f.contact_id from follow_ups f join ids i on i.id=f.id
             union all select o.contact_id from sales_opportunities o join ids i on i.id=o.id
             union all select v.contact_id from property_visits v join ids i on i.id=v.id
             union all select o.contact_id from negotiation_records n join sales_opportunities o on o.id=n.opportunity_id join ids i on i.id=n.id
             union all select o.contact_id from reservations r join sales_opportunities o on o.id=r.opportunity_id join ids i on i.id=r.id
             union all select s.buyer_contact_id from sale_records s join ids i on i.id=s.id
             union all select s.buyer_contact_id from commission_records cr join sale_records s on s.id=cr.sale_record_id join ids i on i.id=cr.id
             union all select r.contact_id from property_recommendations r join ids i on i.id=r.id
             union all select r.contact_id from reactivation_recommendations r join ids i on i.id=r.id
           ) select distinct contact_id from found""",
        (record_ids,),
    ).fetchall()
    return [row["contact_id"] for row in rows]


def authorize_callback(conn: Connection, actor_id: int, callback_data: str) -> None:
    """Shared callback gate for active membership, role permissions, and record scope."""
    actor = get_user(conn, actor_id)
    if not actor or actor["account_status"] != "ACTIVE":
        raise PermissionError("This CRM account is inactive. Contact the administrator.")
    parts = callback_data.split(":")
    prefix = parts[0] if parts else ""
    operation = parts[1] if len(parts) > 1 else ""
    if prefix == "p4":
        return
    if prefix == "p3":
        if operation in {"command", "report", "recent_sales"}:
            if actor["role"] == "SALES_MANAGER":
                raise PermissionError("Open Team Performance for your authorized team reports.")
            require_permission(conn, actor_id, "reports.team")
        if operation in {"commissions", "rule", "crnew", "cruleconfirm", "ruletoggle", "ruleyes"}:
            require_permission(conn, actor_id, "commissions.manage")
        if operation in {
            "commlist",
            "commission",
            "cmnew",
            "cmamount",
            "cmstatus",
            "cmyes",
            "cmpayyes",
            "cmoneyyes",
            "cmnewyes",
        }:
            if actor["role"] == "SALESPERSON":
                if operation not in {"commlist", "commission"}:
                    raise PermissionError(
                        "You can view your commission records but cannot change them."
                    )
                require_permission(conn, actor_id, "commissions.self")
            elif actor["role"] == "SALES_MANAGER":
                raise PermissionError(
                    "Commission details are not enabled for this manager account."
                )
            else:
                require_permission(conn, actor_id, "commissions.team")
        if actor["role"] == "OBSERVER":
            readable = {
                "home",
                "pipeline",
                "list",
                "closed",
                "op",
                "search",
                "visits",
                "v",
                "vtimeline",
                "negs",
                "reservations",
            }
            if operation not in readable:
                raise PermissionError("Observer accounts are read-only.")
    elif prefix == "m":
        if operation in {"import", "textimport"}:
            require_permission(conn, actor_id, "imports.manage")
        if operation == "export":
            require_permission(conn, actor_id, "contacts.export_team")
        if operation == "perf":
            require_permission(
                conn,
                actor_id,
                "reports.self" if actor["role"] == "SALESPERSON" else "reports.team",
            )
        if operation in {"settings"}:
            if actor["role"] not in {"SUPER_ADMIN", "SALESPERSON", "SALES_MANAGER"}:
                raise PermissionError("Settings are not enabled for this account.")
        if actor["role"] == "OBSERVER" and operation not in {
            "home",
            "dashboard",
            "search",
            "campaigns",
        }:
            raise PermissionError("Observer accounts are read-only.")
    elif prefix in {"ef", "ex", "excampaign"}:
        require_permission(conn, actor_id, "contacts.export_team")
    elif prefix == "perf":
        require_permission(
            conn,
            actor_id,
            "reports.self" if actor["role"] == "SALESPERSON" else "reports.team",
        )
    elif prefix == "camp":
        if operation == "new":
            require_permission(conn, actor_id, "campaigns.manage")
        else:
            require_permission(conn, actor_id, "campaigns.call")
    elif actor["role"] == "OBSERVER":
        raise PermissionError("Observer accounts are read-only.")

    # P3, P2, and legacy contact buttons carry business UUIDs. Resolve known
    # records to their contact and enforce assignment/team access before routing.
    record_ids = []
    for token in parts[1:]:
        try:
            record_id = UUID(token)
        except (ValueError, TypeError):
            continue
        record_ids.append(record_id)
        if (
            prefix == "p3"
            and operation
            in {
                "commlist",
                "commission",
                "cmnew",
                "cmamount",
                "cmstatus",
                "cmyes",
                "cmpayyes",
                "cmoneyyes",
                "cmnewyes",
            }
            and actor["role"] == "SALESPERSON"
        ):
            commission = conn.execute(
                """select coalesce(cr.responsible_telegram_user_id,s.responsible_telegram_user_id) owner_id
                   from commission_records cr join sale_records s on s.id=cr.sale_record_id where cr.id=%s
                   union all select s.responsible_telegram_user_id from sale_records s where s.id=%s""",
                (record_id, record_id),
            ).fetchone()
            if commission and commission["owner_id"] != actor_id:
                raise PermissionError("You can only view your own commission information.")
    for contact_id in _record_contact_ids(conn, record_ids):
        if contact_id:
            require_contact_access(conn, actor_id, contact_id)

    is_navigation = operation in {"home", "menu", "cancel", "dashboard"} or callback_data in {
        "m:home",
        "m:dashboard",
    }
    state = (
        conn.execute(
            """select contact_id from user_input_state where telegram_user_id=%s
           and expires_at>now() and contact_id is not null""",
            (actor_id,),
        ).fetchone()
        if not is_navigation
        else None
    )
    if state:
        require_contact_access(conn, actor_id, state["contact_id"])


def authorize_message(
    conn: Connection,
    actor_id: int,
    *,
    is_import: bool = False,
    is_read_only_command: bool = False,
) -> dict[str, Any]:
    actor = get_user(conn, actor_id)
    if not actor or actor["account_status"] != "ACTIVE":
        raise PermissionError("This CRM account is inactive. Contact the administrator.")
    if is_import:
        require_permission(conn, actor_id, "imports.manage")
    if actor["role"] == "OBSERVER" and not is_import and not is_read_only_command:
        raise PermissionError("Observer accounts are read-only.")
    state = (
        conn.execute(
            """select contact_id from user_input_state where telegram_user_id=%s
           and expires_at>now() and contact_id is not null""",
            (actor_id,),
        ).fetchone()
        if not is_read_only_command
        else None
    )
    if state:
        require_contact_access(conn, actor_id, state["contact_id"])
    return actor


def require_user_in_scope(conn: Connection, actor_id: int, target_id: int) -> None:
    actor = require_permission(conn, actor_id, "leads.assign")
    target = get_user(conn, target_id)
    if not target or target["role"] != "SALESPERSON" or target["account_status"] != "ACTIVE":
        raise ValueError("Choose an active salesperson")
    if actor["role"] == "SUPER_ADMIN":
        return
    allowed = conn.execute(
        """select 1 from crm_team_memberships theirs
           join crm_team_memberships mine on mine.team_id=theirs.team_id and mine.active
           join crm_teams t on t.id=mine.team_id and t.active
           where theirs.telegram_user_id=%s and theirs.active and mine.telegram_user_id=%s
             and (t.manager_telegram_user_id=%s or exists (
               select 1 from crm_team_memberships mm where mm.team_id=t.id
                 and mm.telegram_user_id=%s and mm.active)) limit 1""",
        (target_id, actor_id, actor_id, actor_id),
    ).fetchone()
    if not allowed:
        raise PermissionError("The salesperson is outside your team scope")


def assign_contact(
    conn: Connection,
    actor_id: int,
    contact_id: UUID,
    new_owner_id: int | None,
    *,
    reason: str,
    source: str = "MANUAL",
    idempotency_key: str | None = None,
    override_active_deal: bool = False,
    override_capacity: bool = False,
    decision_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_permission(conn, actor_id, "leads.assign")
    if new_owner_id is None:
        require_permission(conn, actor_id, "users.manage")
    else:
        require_user_in_scope(conn, actor_id, new_owner_id)
    if not reason or len(reason.strip()) > 500:
        raise ValueError("Record a reason of up to 500 characters")
    if source not in {"MANUAL", "ROUND_ROBIN", "LOWEST_WORKLOAD", "BULK", "HANDOVER", "BOOTSTRAP"}:
        raise ValueError("Assignment source is not supported")
    if idempotency_key:
        previous = conn.execute(
            "select * from lead_assignment_history where idempotency_key=%s",
            (idempotency_key,),
        ).fetchone()
        if previous:
            if previous["actor_telegram_user_id"] != actor_id:
                raise PermissionError("Assignment request key belongs to another user")
            if previous["contact_id"] != contact_id:
                raise ValueError("Assignment request key was already used for another lead")
            require_contact_access(conn, actor_id, contact_id)
            return previous
    contact = conn.execute(
        """select assigned_telegram_user_id,ownership_restricted,contact_eligibility,
                  normalized_phone,lead_status,conversion_state
           from contacts where id=%s for update""",
        (contact_id,),
    ).fetchone()
    if not contact:
        raise ValueError("Contact was not found")
    require_contact_access(conn, actor_id, contact_id)
    previous_owner = contact["assigned_telegram_user_id"]
    if previous_owner == new_owner_id:
        return {
            "contact_id": contact_id,
            "previous_owner_telegram_user_id": previous_owner,
            "new_owner_telegram_user_id": new_owner_id,
            "unchanged": True,
        }
    actor = get_user(conn, actor_id)
    counts_as_active = (
        contact["contact_eligibility"] == "ELIGIBLE"
        and contact["normalized_phone"] is not None
        and contact["lead_status"] not in ACTIVE_LEAD_STATUSES
        and contact["conversion_state"] != "CONVERTED"
    )
    if new_owner_id is not None and previous_owner != new_owner_id and counts_as_active:
        capacity_row = conn.execute(
            "select max_active_leads_per_salesperson from crm_team_settings where singleton"
        ).fetchone()
        capacity = capacity_row["max_active_leads_per_salesperson"] if capacity_row else 500
        owned = conn.execute(
            """select count(*) amount from contacts where assigned_telegram_user_id=%s
               and contact_eligibility='ELIGIBLE' and normalized_phone is not null
               and lead_status not in ('DO_NOT_CONTACT','NOT_INTERESTED','DISQUALIFIED','CONVERTED')
               and conversion_state<>'CONVERTED'""",
            (new_owner_id,),
        ).fetchone()["amount"]
        if owned >= capacity:
            if not override_capacity:
                raise ValueError("Receiving salesperson is at the configured active-lead capacity")
            require_permission(conn, actor_id, "users.manage")
    if contact["ownership_restricted"] and actor["role"] != "SUPER_ADMIN":
        raise PermissionError("This lead has an ownership restriction")
    if previous_owner is not None and actor["role"] != "SUPER_ADMIN":
        settings = conn.execute(
            "select manager_can_override_ownership from crm_team_settings where singleton"
        ).fetchone()
        manager_override = bool(settings and settings["manager_can_override_ownership"])
        if actor["role"] != "SALES_MANAGER" or not manager_override:
            require_permission(conn, actor_id, "leads.override_ownership")
    active_deal = conn.execute(
        """select exists(select 1 from negotiation_records n join sales_opportunities o on o.id=n.opportunity_id
                 where o.contact_id=%s and o.final_outcome is null
                   and n.negotiation_status in ('BUYER_OFFER_RECEIVED','SELLER_COUNTEROFFER_RECORDED','NEGOTIATING',
                                    'PRICE_AGREED','AWAITING_DOCUMENTS','ACCEPTED_RECORDED'))
               or exists(select 1 from reservations r join sales_opportunities o on o.id=r.opportunity_id
                 where o.contact_id=%s and r.reservation_status not in ('CANCELLED','EXPIRED')) as active""",
        (contact_id, contact_id),
    ).fetchone()["active"]
    if active_deal:
        policy = conn.execute(
            """select active_negotiation_requires_super_admin,manager_can_override_ownership
               from crm_team_settings where singleton"""
        ).fetchone()
        actor_may_override = (actor["role"] == "SUPER_ADMIN" and override_active_deal) or (
            actor["role"] == "SALES_MANAGER"
            and policy
            and not policy["active_negotiation_requires_super_admin"]
            and policy["manager_can_override_ownership"]
        )
        if not actor_may_override:
            raise ValueError(
                "This lead has an active negotiation or reservation; super-admin approval is required"
            )
        if actor["role"] == "SUPER_ADMIN" and override_active_deal:
            require_permission(conn, actor_id, "users.manage")
    conn.execute(
        """update contacts set assigned_telegram_user_id=%s,assigned_at=now(),assignment_source=%s,
             updated_at=now() where id=%s""",
        (new_owner_id, source, contact_id),
    )
    conn.execute(
        """update sales_opportunities set assigned_telegram_user_id=%s,updated_at=now()
           where contact_id=%s and final_outcome is null""",
        (new_owner_id, contact_id),
    )
    if previous_owner is not None:
        if new_owner_id is None:
            conn.execute(
                """delete from reminder_deliveries d using follow_ups f
                   where f.id=d.follow_up_id and f.contact_id=%s and f.status='PENDING'
                     and d.status<>'SENT'""",
                (contact_id,),
            )
        else:
            conn.execute(
                """update reminder_deliveries d set telegram_user_id=%s,status='PENDING',attempts=0,
                     last_error=null,updated_at=now() from follow_ups f
                   where f.id=d.follow_up_id and f.contact_id=%s and f.status='PENDING'
                     and d.status<>'SENT'""",
                (new_owner_id, contact_id),
            )
        conn.execute(
            """update notification_deliveries d set status='FAILED',
                 last_error='Lead ownership changed',updated_at=now()
               where d.telegram_user_id=%s and d.status in ('QUEUED','SENDING') and (
                 (d.related_entity_type='FOLLOW_UP' and exists(select 1 from follow_ups f
                    where f.id=d.related_entity_id and f.contact_id=%s))
                 or (d.related_entity_type='PROPERTY_VISIT' and exists(select 1 from property_visits v
                    where v.id=d.related_entity_id and v.contact_id=%s))
                 or (d.related_entity_type='OPPORTUNITY' and exists(select 1 from sales_opportunities o
                    where o.id=d.related_entity_id and o.contact_id=%s))
                 or (d.related_entity_type='NEGOTIATION' and exists(select 1 from negotiation_records n
                    join sales_opportunities o on o.id=n.opportunity_id
                    where n.id=d.related_entity_id and o.contact_id=%s))
                 or (d.related_entity_type='RESERVATION' and exists(select 1 from reservations r
                    join sales_opportunities o on o.id=r.opportunity_id
                    where r.id=d.related_entity_id and o.contact_id=%s)))""",
            (previous_owner, contact_id, contact_id, contact_id, contact_id, contact_id),
        )
    history = conn.execute(
        """insert into lead_assignment_history(contact_id,previous_owner_telegram_user_id,
             new_owner_telegram_user_id,actor_telegram_user_id,assignment_source,reason,decision_facts,idempotency_key)
           values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s) returning *""",
        (
            contact_id,
            previous_owner,
            new_owner_id,
            actor_id,
            source,
            reason.strip(),
            json.dumps(decision_facts or {}, ensure_ascii=False, default=str),
            idempotency_key,
        ),
    ).fetchone()
    conn.execute(
        """insert into audit_events(telegram_user_id,contact_id,event_type,old_value,new_value,
             entity_type,entity_id,actor_telegram_user_id,reason,request_key)
           values (%s,%s,'LEAD_ASSIGNED',%s::jsonb,%s::jsonb,'CONTACT',%s,%s,%s,%s)""",
        (
            actor_id,
            contact_id,
            json.dumps({"owner": previous_owner}),
            json.dumps({"owner": new_owner_id, "source": source}),
            contact_id,
            actor_id,
            reason.strip(),
            idempotency_key,
        ),
    )
    return history


def _distribution_candidates(
    conn: Connection, team_id: UUID, *, limit: int = 500
) -> list[dict[str, Any]]:
    candidates = conn.execute(
        """select u.telegram_user_id,u.display_name,
                  coalesce(load.active_leads,0) active_leads,coalesce(load.workload,0) workload
           from crm_team_memberships m join crm_users u on u.telegram_user_id=m.telegram_user_id
           left join lateral (
             select count(*) active_leads,coalesce(sum(workload),0) workload from (
               select c.id,max(case when o.current_stage in ('BOOKED','RESERVATION_IN_PROGRESS') then 4
                                    when o.current_stage='NEGOTIATION' then 3
                                    when c.lead_status in ('QUALIFIED','FOLLOW_UP_REQUIRED') then 2 else 1 end) workload
                 from contacts c left join sales_opportunities o on o.contact_id=c.id and o.final_outcome is null
                where c.assigned_telegram_user_id=u.telegram_user_id
                  and c.contact_eligibility='ELIGIBLE'
                  and c.normalized_phone is not null and not c.ownership_restricted
                  and c.lead_status not in ('DO_NOT_CONTACT','NOT_INTERESTED','DISQUALIFIED','CONVERTED')
                  and c.conversion_state<>'CONVERTED'
                group by c.id
             ) owned
           ) load on true
           where m.team_id=%s and m.active and u.account_status='ACTIVE' and u.can_receive_leads
             and u.role='SALESPERSON'
           order by u.telegram_user_id limit %s""",
        (team_id, max(1, min(limit, 500))),
    ).fetchall()
    return candidates


def _select_unassigned(
    conn: Connection, filters: dict[str, Any], limit: int
) -> list[dict[str, Any]]:
    allowed = {
        "campaign_id",
        "import_batch_id",
        "created_after",
        "created_before",
        "lead_status",
        "interest_level",
        "lead_source",
        "priority_category",
        "qualification_status",
        "property_type",
        "location",
        "min_budget",
        "max_budget",
    }
    if set(filters) - allowed:
        raise ValueError("One or more lead filters are not supported")
    budget_bounds: dict[str, Decimal] = {}
    for key in ("min_budget", "max_budget"):
        if filters.get(key) is not None:
            try:
                amount = Decimal(str(filters[key]))
            except InvalidOperation as exc:
                raise ValueError(f"{key} must be a valid amount") from exc
            if not amount.is_finite() or amount < 0:
                raise ValueError(f"{key} must be a nonnegative amount")
            budget_bounds[key] = amount
    if budget_bounds.get("min_budget", Decimal(0)) > budget_bounds.get(
        "max_budget", Decimal("Infinity")
    ):
        raise ValueError("Minimum budget cannot exceed maximum budget")
    params: list[Any] = []
    clauses = [
        "c.assigned_telegram_user_id is null",
        "c.contact_eligibility='ELIGIBLE'",
        "c.normalized_phone is not null",
        "not c.ownership_restricted",
        "c.conversion_state<>'CONVERTED'",
        "c.lead_status not in ('DO_NOT_CONTACT','NOT_INTERESTED','DISQUALIFIED','CONVERTED')",
    ]
    if filters.get("campaign_id"):
        clauses.append(
            "exists(select 1 from campaign_contacts cc join campaigns ca on ca.id=cc.campaign_id and ca.status='ACTIVE' and (ca.starts_at is null or ca.starts_at<=now()) and (ca.ends_at is null or ca.ends_at>now()) where cc.contact_id=c.id and cc.campaign_id=%s and cc.campaign_status='ACTIVE')"
        )
        params.append(UUID(str(filters["campaign_id"])))
    if filters.get("import_batch_id"):
        clauses.append("c.import_batch_id=%s")
        params.append(UUID(str(filters["import_batch_id"])))
    if filters.get("lead_status"):
        clauses.append("c.lead_status::text=%s")
        params.append(str(filters["lead_status"]).upper())
    if filters.get("interest_level"):
        clauses.append("c.interest_level::text=%s")
        params.append(str(filters["interest_level"]).upper())
    if filters.get("lead_source"):
        clauses.append(
            "(c.lead_source ilike %s or exists(select 1 from campaign_contacts cc join campaigns ca on ca.id=cc.campaign_id where cc.contact_id=c.id and ca.source ilike %s))"
        )
        source_pattern = f"%{str(filters['lead_source'])[:80]}%"
        params.extend([source_pattern, source_pattern])
    if filters.get("priority_category"):
        clauses.append(
            "exists(select 1 from lead_scores ls where ls.contact_id=c.id and ls.priority_category=%s)"
        )
        params.append(str(filters["priority_category"]).upper())
    if filters.get("created_after"):
        try:
            after = datetime.fromisoformat(str(filters["created_after"]))
        except ValueError as exc:
            raise ValueError("created_after must be an ISO date or timestamp") from exc
        clauses.append("c.created_at >= %s")
        params.append(after)
    if filters.get("created_before"):
        try:
            before = datetime.fromisoformat(str(filters["created_before"]))
        except ValueError as exc:
            raise ValueError("created_before must be an ISO date or timestamp") from exc
        clauses.append("c.created_at < %s")
        params.append(before)
    if filters.get("qualification_status"):
        clauses.append(
            "exists(select 1 from lead_qualifications q where q.contact_id=c.id and q.qualification_status=%s)"
        )
        params.append(str(filters["qualification_status"]).upper())
    if filters.get("property_type"):
        clauses.append(
            "exists(select 1 from contact_property_interests i where i.contact_id=c.id and i.property_type=%s)"
        )
        params.append(str(filters["property_type"]).upper())
    if filters.get("location"):
        clauses.append(
            "exists(select 1 from contact_property_interests i where i.contact_id=c.id and i.preferred_location ilike %s)"
        )
        params.append(f"%{str(filters['location'])[:80]}%")
    if filters.get("min_budget") is not None:
        clauses.append(
            "exists(select 1 from contact_property_interests i where i.contact_id=c.id and coalesce(i.max_budget,i.min_budget)>=%s)"
        )
        params.append(budget_bounds["min_budget"])
    if filters.get("max_budget") is not None:
        clauses.append(
            "exists(select 1 from contact_property_interests i where i.contact_id=c.id and coalesce(i.min_budget,i.max_budget)<=%s)"
        )
        params.append(budget_bounds["max_budget"])
    params.append(limit)
    return conn.execute(
        f"""select c.id,c.name,c.lead_status,c.created_at from contacts c where {" and ".join(clauses)}
             order by c.created_at,c.id limit %s""",
        tuple(params),
    ).fetchall()


def preview_distribution(
    conn: Connection,
    actor_id: int,
    team_id: UUID,
    *,
    method: str = "ROUND_ROBIN",
    filters: dict[str, Any] | None = None,
    limit: int = 500,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    require_permission(conn, actor_id, "leads.distribute")
    actor = get_user(conn, actor_id)
    if actor["role"] != "SUPER_ADMIN":
        allowed_team = conn.execute(
            """select 1 from crm_teams t where t.id=%s and t.active and (
                 t.manager_telegram_user_id=%s or exists(select 1 from crm_team_memberships m
                   where m.team_id=t.id and m.telegram_user_id=%s and m.active))""",
            (team_id, actor_id, actor_id),
        ).fetchone()
        if not allowed_team:
            raise PermissionError("Team is outside your authorized assignment scope")
    if idempotency_key:
        existing = conn.execute(
            "select * from lead_assignment_jobs where idempotency_key=%s", (idempotency_key,)
        ).fetchone()
        if existing:
            if existing["created_by"] != actor_id:
                raise PermissionError("Assignment request key belongs to another user")
            if existing["team_id"] != team_id:
                raise ValueError("Assignment request key was already used for another team")
            return existing
    method = method.upper()
    if method not in {"ROUND_ROBIN", "LOWEST_WORKLOAD"}:
        raise ValueError(
            "Choose round robin or lowest workload; manual allocation uses Assign Leads"
        )
    limit = max(1, min(int(limit), 500))
    team = conn.execute("select active from crm_teams where id=%s", (team_id,)).fetchone()
    if not team or not team["active"]:
        raise ValueError("Choose an active team")
    candidates = _distribution_candidates(conn, team_id)
    if not candidates:
        raise ValueError("This team has no active salespeople eligible for leads")
    setting = conn.execute(
        "select max_active_leads_per_salesperson from crm_team_settings where singleton"
    ).fetchone()
    capacity = setting["max_active_leads_per_salesperson"] if setting else 500
    candidates = [c for c in candidates if c["active_leads"] < capacity]
    if not candidates:
        raise ValueError("All eligible salespeople are at configured lead capacity")
    leads = _select_unassigned(conn, filters or {}, limit)
    if not leads:
        raise ValueError("No eligible unassigned leads match these filters")
    state = conn.execute(
        "select last_assigned_telegram_user_id from team_distribution_state where team_id=%s",
        (team_id,),
    ).fetchone()
    assignments: list[dict[str, Any]] = []
    workloads = {int(c["telegram_user_id"]): int(c["workload"]) for c in candidates}
    active_counts = {int(c["telegram_user_id"]): int(c["active_leads"]) for c in candidates}
    cursor = state["last_assigned_telegram_user_id"] if state else None
    for lead in leads:
        eligible_candidates = [
            {
                "owner_id": int(candidate["telegram_user_id"]),
                "workload": workloads[int(candidate["telegram_user_id"])],
                "active_leads": active_counts[int(candidate["telegram_user_id"])],
            }
            for candidate in candidates
        ]
        if method == "ROUND_ROBIN":
            eligible_ids = [int(c["telegram_user_id"]) for c in candidates]
            index = (
                (eligible_ids.index(cursor) + 1) % len(eligible_ids)
                if cursor in eligible_ids
                else 0
            )
            chosen = candidates[index]
            cursor = int(chosen["telegram_user_id"])
        elif method == "LOWEST_WORKLOAD":
            chosen = min(
                candidates,
                key=lambda c: (workloads[int(c["telegram_user_id"])], int(c["telegram_user_id"])),
            )
        else:
            raise ValueError("Manual distribution requires individual assignments")
        owner = int(chosen["telegram_user_id"])
        assignments.append(
            {
                "contact_id": str(lead["id"]),
                "owner_id": owner,
                "owner_name": chosen["display_name"],
                "workload_before": workloads[owner],
                "reason": method,
                "eligible_candidates": eligible_candidates,
            }
        )
        active_counts[owner] += 1
        workloads[owner] += 1
        if active_counts[owner] >= capacity:
            candidates = [c for c in candidates if int(c["telegram_user_id"]) != owner]
            if not candidates:
                break
    if len(assignments) != len(leads):
        leads = leads[: len(assignments)]
    job_key = idempotency_key or secrets.token_urlsafe(18)
    job = conn.execute(
        """insert into lead_assignment_jobs(created_by,team_id,method,filters,preview_assignments,
             selected_count,idempotency_key) values (%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s) returning *""",
        (
            actor_id,
            team_id,
            method,
            json.dumps(filters or {}, default=str),
            json.dumps(assignments),
            len(assignments),
            job_key,
        ),
    ).fetchone()
    return job


def _require_distribution_job_scope(conn: Connection, actor_id: int, team_id: UUID | None) -> None:
    actor = require_permission(conn, actor_id, "leads.distribute")
    if actor["role"] == "SUPER_ADMIN":
        return
    allowed = conn.execute(
        """select 1 from crm_teams t where t.id=%s and t.active and (
             t.manager_telegram_user_id=%s or exists(select 1 from crm_team_memberships m
               where m.team_id=t.id and m.telegram_user_id=%s and m.active))""",
        (team_id, actor_id, actor_id),
    ).fetchone()
    if not allowed:
        raise PermissionError("Assignment job is outside your authorized team scope")


def apply_distribution(conn: Connection, actor_id: int, job_id: UUID) -> dict[str, Any]:
    require_permission(conn, actor_id, "leads.distribute")
    job = conn.execute(
        "select * from lead_assignment_jobs where id=%s for update", (job_id,)
    ).fetchone()
    if not job:
        raise ValueError("Assignment preview was not found")
    _require_distribution_job_scope(conn, actor_id, job["team_id"])
    if job["status"] == "COMPLETED":
        return job
    if job["status"] != "PREVIEW":
        raise ValueError("Assignment preview is no longer available")
    team_row = conn.execute(
        "select active from crm_teams where id=%s for update", (job["team_id"],)
    ).fetchone()
    if not team_row or not team_row["active"]:
        raise ValueError("Team is no longer active")
    state = conn.execute(
        """select last_assigned_telegram_user_id from team_distribution_state where team_id=%s for update""",
        (job["team_id"],),
    ).fetchone()
    last_owner = state["last_assigned_telegram_user_id"] if state else None
    applied = 0
    capacity_row = conn.execute(
        "select max_active_leads_per_salesperson from crm_team_settings where singleton"
    ).fetchone()
    capacity = capacity_row["max_active_leads_per_salesperson"] if capacity_row else 500
    for proposed in job["preview_assignments"]:
        contact_id = UUID(proposed["contact_id"])
        owner_id = int(proposed["owner_id"])
        eligible_owner = conn.execute(
            """select 1 from crm_team_memberships m join crm_users u
                 on u.telegram_user_id=m.telegram_user_id
               where m.team_id=%s and m.telegram_user_id=%s and m.active
                 and u.account_status='ACTIVE' and u.role='SALESPERSON' and u.can_receive_leads""",
            (job["team_id"], owner_id),
        ).fetchone()
        if not eligible_owner or not permission_allowed(conn, owner_id, "contacts.read_assigned"):
            continue
        current = conn.execute(
            """select assigned_telegram_user_id,contact_eligibility,lead_status,conversion_state
               from contacts where id=%s for update""",
            (contact_id,),
        ).fetchone()
        if (
            not current
            or current["assigned_telegram_user_id"] is not None
            or current["contact_eligibility"] != "ELIGIBLE"
            or current["lead_status"] in ACTIVE_LEAD_STATUSES
            or current["conversion_state"] == "CONVERTED"
        ):
            continue
        owned = conn.execute(
            """select count(*) amount from contacts where assigned_telegram_user_id=%s
               and contact_eligibility='ELIGIBLE'
               and normalized_phone is not null
               and lead_status not in ('DO_NOT_CONTACT','NOT_INTERESTED','DISQUALIFIED','CONVERTED')
               and conversion_state<>'CONVERTED'""",
            (owner_id,),
        ).fetchone()["amount"]
        if owned >= capacity:
            continue
        assign_contact(
            conn,
            actor_id,
            contact_id,
            owner_id,
            reason="Confirmed smart lead distribution",
            source=job["method"],
            idempotency_key=f"{job['id']}:{contact_id}",
            decision_facts={
                "job_id": str(job["id"]),
                "method": job["method"],
                "workload_before": proposed["workload_before"],
                "eligible_candidates": proposed.get("eligible_candidates", []),
            },
        )
        applied += 1
        last_owner = owner_id
    if job["team_id"] and last_owner is not None:
        conn.execute(
            """insert into team_distribution_state(team_id,last_assigned_telegram_user_id)
               values (%s,%s) on conflict(team_id) do update
               set last_assigned_telegram_user_id=excluded.last_assigned_telegram_user_id,updated_at=now()""",
            (job["team_id"], last_owner),
        )
    return conn.execute(
        """update lead_assignment_jobs set status='COMPLETED',applied_count=%s,
             completed_at=now(),updated_at=now() where id=%s returning *""",
        (applied, job_id),
    ).fetchone()


def cancel_distribution(conn: Connection, actor_id: int, job_id: UUID) -> dict[str, Any]:
    require_permission(conn, actor_id, "leads.distribute")
    job = conn.execute(
        "select * from lead_assignment_jobs where id=%s for update", (job_id,)
    ).fetchone()
    if not job:
        raise ValueError("Assignment preview was not found")
    _require_distribution_job_scope(conn, actor_id, job["team_id"])
    if job["status"] != "PREVIEW":
        raise ValueError("Only an unconfirmed preview can be cancelled")
    cancelled = conn.execute(
        """update lead_assignment_jobs set status='CANCELLED',updated_at=now()
           where id=%s and status='PREVIEW' returning *""",
        (job_id,),
    ).fetchone()
    _audit(
        conn,
        actor_id,
        "LEAD_DISTRIBUTION_CANCELLED",
        "ASSIGNMENT_JOB",
        entity_id=job_id,
        old_value={"status": "PREVIEW"},
        new_value={"status": "CANCELLED"},
    )
    return cancelled


HANDOVER_REASONS = {
    "UNAVAILABLE",
    "LEAVING_TEAM",
    "SPECIALIST",
    "WORKLOAD",
    "CUSTOMER_REQUEST",
    "MANAGER_INTERVENTION",
    "OTHER",
}


def create_handover_request(
    conn: Connection, actor_id: int, contact_id: UUID, reason: str, notes: str | None = None
) -> dict[str, Any]:
    require_permission(conn, actor_id, "handover.request")
    reason = reason.strip().upper()
    if reason not in HANDOVER_REASONS or (notes and len(notes.strip()) > 500):
        raise ValueError("Choose a handover reason and keep notes under 500 characters")
    settings = conn.execute(
        "select salespeople_can_request_reassignment from crm_team_settings where singleton"
    ).fetchone()
    if not settings or not settings["salespeople_can_request_reassignment"]:
        raise PermissionError("Lead reassignment requests are disabled by team settings")
    require_contact_access(conn, actor_id, contact_id)
    contact = conn.execute(
        "select assigned_telegram_user_id from contacts where id=%s for update", (contact_id,)
    ).fetchone()
    if not contact or contact["assigned_telegram_user_id"] != actor_id:
        raise PermissionError("You can request a handover only for a lead assigned to you")
    snapshot = conn.execute(
        """select c.id,c.lead_status,c.conversion_state,
             (select count(*) from follow_ups f where f.contact_id=c.id and f.status='PENDING') pending_followups,
             (select count(*) from sales_opportunities o where o.contact_id=c.id and o.final_outcome is null) active_opportunities,
             (select count(*) from property_visits v where v.contact_id=c.id and v.visit_status in ('SCHEDULED','BUYER_ARRIVED')) upcoming_visits,
             (select count(*) from negotiation_records n join sales_opportunities o on o.id=n.opportunity_id
                where o.contact_id=c.id and o.final_outcome is null and n.negotiation_status not in ('REJECTED','EXPIRED','CANCELLED')) active_negotiations,
             (select count(*) from reservations r join sales_opportunities o on o.id=r.opportunity_id
                where o.contact_id=c.id and r.reservation_status not in ('CANCELLED','EXPIRED')) active_reservations
           from contacts c where c.id=%s""",
        (contact_id,),
    ).fetchone()
    row = conn.execute(
        """insert into lead_handover_requests(contact_id,requested_by,current_owner,reason,notes,task_snapshot)
           values (%s,%s,%s,%s,%s,%s::jsonb) returning *""",
        (
            contact_id,
            actor_id,
            actor_id,
            reason,
            notes.strip() if notes else None,
            json.dumps(snapshot, default=str),
        ),
    ).fetchone()
    _audit(
        conn,
        actor_id,
        "LEAD_HANDOVER_REQUESTED",
        "HANDOVER_REQUEST",
        entity_id=row["id"],
        new_value={"contact_id": contact_id, "reason": reason, "task_snapshot": snapshot},
    )
    return row


def list_handover_requests(
    conn: Connection, actor_id: int, limit: int = 20
) -> list[dict[str, Any]]:
    require_permission(conn, actor_id, "leads.assign")
    return conn.execute(
        """select r.*,c.name contact_name,u.display_name requester_name
           from lead_handover_requests r join contacts c on c.id=r.contact_id
           join crm_users u on u.telegram_user_id=r.requested_by
           where r.status='PENDING' and crm_can_access_contact(c.id,%s)
           order by r.created_at,r.id limit %s""",
        (actor_id, max(1, min(limit, 50))),
    ).fetchall()


def review_handover_request(
    conn: Connection,
    actor_id: int,
    request_id: UUID,
    *,
    approve: bool,
    new_owner_id: int | None = None,
) -> dict[str, Any]:
    require_permission(conn, actor_id, "leads.assign")
    request = conn.execute(
        "select * from lead_handover_requests where id=%s for update", (request_id,)
    ).fetchone()
    if not request or request["status"] != "PENDING":
        raise ValueError("Handover request is no longer pending")
    require_contact_access(conn, actor_id, request["contact_id"])
    if not approve:
        row = conn.execute(
            """update lead_handover_requests set status='REJECTED',reviewed_by=%s,updated_at=now()
               where id=%s returning *""",
            (actor_id, request_id),
        ).fetchone()
        _audit(
            conn,
            actor_id,
            "LEAD_HANDOVER_REJECTED",
            "HANDOVER_REQUEST",
            entity_id=request_id,
            old_value={"status": "PENDING"},
            new_value={"status": "REJECTED"},
            reason=request["reason"],
        )
        return row
    if new_owner_id is None:
        raise ValueError("Choose an eligible salesperson for this handover")
    current = conn.execute(
        "select assigned_telegram_user_id from contacts where id=%s for update",
        (request["contact_id"],),
    ).fetchone()
    if not current or current["assigned_telegram_user_id"] != request["current_owner"]:
        raise ValueError("Lead ownership changed after this request; review the current assignment")
    history = assign_contact(
        conn,
        actor_id,
        request["contact_id"],
        new_owner_id,
        reason=f"Handover: {request['reason'].replace('_', ' ').title()}",
        source="HANDOVER",
        idempotency_key=f"handover:{request_id}",
        override_active_deal=get_user(conn, actor_id)["role"] == "SUPER_ADMIN",
        override_capacity=get_user(conn, actor_id)["role"] == "SUPER_ADMIN",
        decision_facts={
            "handover_request_id": str(request_id),
            "task_snapshot": request["task_snapshot"],
        },
    )
    row = conn.execute(
        """update lead_handover_requests set status='APPROVED',assigned_to=%s,reviewed_by=%s,updated_at=now()
           where id=%s returning *""",
        (new_owner_id, actor_id, request_id),
    ).fetchone()
    _audit(
        conn,
        actor_id,
        "LEAD_HANDOVER_APPROVED",
        "HANDOVER_REQUEST",
        entity_id=request_id,
        old_value={"owner": request["current_owner"]},
        new_value={"owner": new_owner_id, "assignment_id": history.get("id")},
        reason=request["reason"],
    )
    return row


def require_campaign_scope(conn: Connection, actor_id: int, campaign_id: UUID) -> dict[str, Any]:
    actor = get_user(conn, actor_id)
    if not actor or actor["account_status"] != "ACTIVE":
        raise PermissionError("This CRM account is inactive")
    campaign = conn.execute("select * from campaigns where id=%s", (campaign_id,)).fetchone()
    if not campaign:
        raise ValueError("Campaign was not found")
    if actor["role"] in {"SUPER_ADMIN", "MARKETING_ADMIN"}:
        return campaign
    require_permission(conn, actor_id, "campaigns.call")
    allowed = conn.execute(
        """select 1 where exists(select 1 from campaign_memberships m
               where m.campaign_id=%s and m.telegram_user_id=%s and m.active)
           or exists(select 1 from campaign_contacts cc join contacts c on c.id=cc.contact_id
               where cc.campaign_id=%s and crm_can_access_contact(c.id,%s))
           or exists(select 1 from crm_team_memberships m
               join crm_teams t on t.id=m.team_id and t.active
               where m.team_id=%s and m.telegram_user_id=%s and m.active)""",
        (campaign_id, actor_id, campaign_id, actor_id, campaign["team_id"], actor_id),
    ).fetchone()
    if not allowed:
        raise PermissionError("Campaign is outside your authorized team scope")
    return campaign


def set_campaign_team(
    conn: Connection, actor_id: int, campaign_id: UUID, team_id: UUID | None
) -> dict[str, Any]:
    require_permission(conn, actor_id, "campaigns.manage")
    if team_id is not None:
        selected_team = conn.execute(
            "select id,active from crm_teams where id=%s", (team_id,)
        ).fetchone()
        if not selected_team or not selected_team["active"]:
            raise ValueError("Choose an active team")
    require_campaign_scope(conn, actor_id, campaign_id)
    old = conn.execute("select * from campaigns where id=%s for update", (campaign_id,)).fetchone()
    if not old:
        raise ValueError("Campaign was not found")
    result = conn.execute(
        """update campaigns set team_id=%s,updated_by=%s,updated_at=now()
           where id=%s returning *""",
        (team_id, actor_id, campaign_id),
    ).fetchone()
    if team_id is not None:
        conn.execute(
            """insert into campaign_memberships(campaign_id,telegram_user_id,active,added_by)
               select %s,m.telegram_user_id,true,%s from crm_team_memberships m
                 join crm_users u on u.telegram_user_id=m.telegram_user_id
               where m.team_id=%s and m.active and u.account_status='ACTIVE' and u.role='SALESPERSON'
               on conflict(campaign_id,telegram_user_id) do update
                 set active=true,added_by=excluded.added_by""",
            (campaign_id, actor_id, team_id),
        )
    _audit(
        conn,
        actor_id,
        "CAMPAIGN_TEAM_CHANGED",
        "CAMPAIGN",
        entity_id=campaign_id,
        old_value={"team_id": old["team_id"]},
        new_value={"team_id": team_id},
    )
    return result


def update_campaign_calling_rules(
    conn: Connection,
    actor_id: int,
    campaign_id: UUID,
    *,
    starts_at: datetime | None,
    ends_at: datetime | None,
    calling_priority: int,
    max_call_attempts: int,
    retry_after_hours: int,
    performance_target: Decimal | None,
) -> dict[str, Any]:
    require_permission(conn, actor_id, "campaigns.manage")
    if type(calling_priority) is not int or not 0 <= calling_priority <= 100:
        raise ValueError("Calling priority must be from 0 to 100")
    if type(max_call_attempts) is not int or not 1 <= max_call_attempts <= 50:
        raise ValueError("Maximum call attempts must be from 1 to 50")
    if type(retry_after_hours) is not int or not 0 <= retry_after_hours <= 720:
        raise ValueError("Retry delay must be from 0 to 720 hours")
    if starts_at and starts_at.tzinfo is None or ends_at and ends_at.tzinfo is None:
        raise ValueError("Campaign dates must include a timezone")
    if starts_at and ends_at and ends_at <= starts_at:
        raise ValueError("Campaign end must be after its start")
    target = Decimal(str(performance_target)) if performance_target is not None else None
    if target is not None and (
        not target.is_finite() or target < 0 or target > Decimal("99999999999999.99")
    ):
        raise ValueError("Campaign performance target must be a supported nonnegative amount")
    require_campaign_scope(conn, actor_id, campaign_id)
    old = conn.execute("select * from campaigns where id=%s for update", (campaign_id,)).fetchone()
    if not old:
        raise ValueError("Campaign was not found")
    result = conn.execute(
        """update campaigns set starts_at=%s,ends_at=%s,calling_priority=%s,
             max_call_attempts=%s,retry_after_hours=%s,performance_target=%s,
             updated_by=%s,updated_at=now() where id=%s returning *""",
        (
            starts_at,
            ends_at,
            calling_priority,
            max_call_attempts,
            retry_after_hours,
            target,
            actor_id,
            campaign_id,
        ),
    ).fetchone()
    fields = (
        "starts_at",
        "ends_at",
        "calling_priority",
        "max_call_attempts",
        "retry_after_hours",
        "performance_target",
    )
    _audit(
        conn,
        actor_id,
        "CAMPAIGN_CALLING_RULES_CHANGED",
        "CAMPAIGN",
        entity_id=campaign_id,
        old_value={key: old[key] for key in fields},
        new_value={key: result[key] for key in fields},
    )
    return result


def set_campaign_status(
    conn: Connection, actor_id: int, campaign_id: UUID, status: str
) -> dict[str, Any]:
    status = status.strip().upper()
    if status not in {"DRAFT", "READY", "ACTIVE", "PAUSED", "COMPLETED", "CANCELLED"}:
        raise ValueError("Campaign status is not supported")
    current_actor = get_user(conn, actor_id)
    if not current_actor or current_actor["account_status"] != "ACTIVE":
        raise PermissionError("This CRM account is inactive")
    actor = require_permission(
        conn,
        actor_id,
        "campaigns.team" if current_actor["role"] == "SALES_MANAGER" else "campaigns.manage",
    )
    old = conn.execute("select * from campaigns where id=%s for update", (campaign_id,)).fetchone()
    if not old:
        raise ValueError("Campaign was not found")
    if actor["role"] == "SALES_MANAGER":
        require_campaign_scope(conn, actor_id, campaign_id)
        if (old["status"], status) not in {("ACTIVE", "PAUSED"), ("PAUSED", "ACTIVE")}:
            raise PermissionError("Managers may only pause or resume campaigns in their team")
    allowed = {
        "DRAFT": {"READY", "CANCELLED"},
        "READY": {"ACTIVE", "CANCELLED"},
        "ACTIVE": {"PAUSED", "COMPLETED", "CANCELLED"},
        "PAUSED": {"ACTIVE", "COMPLETED", "CANCELLED"},
    }
    if status not in allowed.get(old["status"], set()):
        raise ValueError("This campaign can no longer change to that status")
    result = conn.execute(
        """update campaigns set status=%s,updated_by=%s,updated_at=now()
           where id=%s returning *""",
        (status, actor_id, campaign_id),
    ).fetchone()
    contact_status = {"PAUSED": "PAUSED", "COMPLETED": "COMPLETED", "CANCELLED": "COMPLETED"}.get(
        status
    )
    if contact_status:
        conn.execute(
            "update campaign_contacts set campaign_status=%s where campaign_id=%s and campaign_status='ACTIVE'",
            (contact_status, campaign_id),
        )
    elif status == "ACTIVE" and old["status"] == "PAUSED":
        conn.execute(
            "update campaign_contacts set campaign_status='ACTIVE' where campaign_id=%s and campaign_status='PAUSED'",
            (campaign_id,),
        )
    _audit(
        conn,
        actor_id,
        "CAMPAIGN_STATUS_CHANGED",
        "CAMPAIGN",
        entity_id=campaign_id,
        old_value={"status": old["status"]},
        new_value={"status": status},
    )
    return result


def campaign_performance(
    conn: Connection, actor_id: int, campaign_id: UUID | None = None
) -> list[dict[str, Any]]:
    user = get_user(conn, actor_id)
    if not user or user["account_status"] != "ACTIVE":
        raise PermissionError("This CRM account is inactive")
    permission = "reports.campaign" if user["role"] == "MARKETING_ADMIN" else "reports.team"
    require_permission(conn, actor_id, permission)
    if user["role"] == "SALESPERSON":
        raise PermissionError("Campaign-wide reports are not available for this account")
    campaign_filter = "and c.id=%s" if campaign_id else ""
    return conn.execute(
        f"""select c.id,c.name,c.description,c.source,c.status,c.starts_at,c.ends_at,
            c.calling_priority,c.max_call_attempts,c.retry_after_hours,c.performance_target,t.name team_name,
            (select count(*) from campaign_contacts cc join contacts x on x.id=cc.contact_id
              where cc.campaign_id=c.id and cc.campaign_status<>'COMPLETED'
                and x.contact_eligibility='ELIGIBLE' and x.conversion_state<>'CONVERTED'
                and x.lead_status not in ('DO_NOT_CONTACT','NOT_INTERESTED','DISQUALIFIED','CONVERTED')) total_eligible_leads,
            (select count(*) from campaign_contacts cc join contacts x on x.id=cc.contact_id
              where cc.campaign_id=c.id and x.assigned_telegram_user_id is not null
                and x.contact_eligibility='ELIGIBLE' and x.lead_status not in ('DO_NOT_CONTACT','NOT_INTERESTED','DISQUALIFIED','CONVERTED')) assigned_leads,
            (select count(distinct l.contact_id) from call_logs l where l.campaign_id=c.id) leads_contacted,
            (select count(*) from call_logs l where l.campaign_id=c.id) calls_logged,
            (select count(distinct l.contact_id) from call_logs l where l.campaign_id=c.id
              and (l.connection_result='ANSWERED' or (l.outcome_category='CONNECTION' and l.outcome_code='ANSWERED'))) unique_contacts_reached,
            (select count(distinct l.contact_id) from call_logs l where l.campaign_id=c.id
              and l.outcome_code in ('QUALIFIED','HIGHLY_INTERESTED')) qualified_buyers,
            (select count(*) from follow_ups f where f.campaign_id=c.id and f.status<>'CANCELLED') followups_scheduled,
            (select count(*) from sales_opportunities o where o.campaign_id=c.id) opportunities_created,
            (select count(*) from property_visits v join sales_opportunities o on o.id=v.opportunity_id
              where o.campaign_id=c.id and v.visit_status in ('SCHEDULED','BUYER_ARRIVED')) visits_scheduled,
            (select count(*) from property_visits v join sales_opportunities o on o.id=v.opportunity_id
              where o.campaign_id=c.id and v.visit_status='COMPLETED') visits_completed,
            (select count(*) from sale_records s join sales_opportunities o on o.id=s.opportunity_id
              where o.campaign_id=c.id and s.verification_status='VERIFIED') verified_sales
           from campaigns c left join crm_teams t on t.id=c.team_id
           where c.status<>'ARCHIVED' and (
             %s in (select u.telegram_user_id from crm_users u where u.telegram_user_id=%s
                    and u.role in ('SUPER_ADMIN','MARKETING_ADMIN'))
             or exists(select 1 from campaign_memberships m where m.campaign_id=c.id
                        and m.telegram_user_id=%s and m.active)
             or exists(select 1 from campaign_contacts cc join contacts x on x.id=cc.contact_id
                        where cc.campaign_id=c.id and crm_can_access_contact(x.id,%s))
             or exists(select 1 from crm_team_memberships m
                        join crm_teams active_team on active_team.id=m.team_id and active_team.active
                        where m.team_id=c.team_id and m.telegram_user_id=%s and m.active)
           ) {campaign_filter}
           order by c.created_at desc limit 30""",
        (actor_id, actor_id, actor_id, actor_id, actor_id, *([campaign_id] if campaign_id else [])),
    ).fetchall()


def create_target(
    conn: Connection,
    actor_id: int,
    *,
    target_type: str,
    target_amount: int | float,
    period_type: str,
    starts_at: datetime,
    ends_at: datetime,
    telegram_user_id: int | None = None,
    team_id: UUID | None = None,
    campaign_id: UUID | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    require_permission(conn, actor_id, "targets.manage_team")
    if starts_at.tzinfo is None or ends_at.tzinfo is None or starts_at >= ends_at:
        raise ValueError("Target dates must be timezone-aware and increasing")
    if float(target_amount) < 0:
        raise ValueError("Target amount must be zero or greater")
    target_type = target_type.upper()
    period_type = period_type.upper()
    supported_targets = {
        "CALLS_LOGGED",
        "UNIQUE_CONTACTS_REACHED",
        "QUALIFIED_BUYERS",
        "FOLLOWUPS_COMPLETED",
        "VISITS_COMPLETED",
        "OPPORTUNITIES_CREATED",
        "RESERVATIONS_RECORDED",
        "VERIFIED_SALES_COUNT",
        "VERIFIED_SALES_VALUE",
    }
    supported_periods = {"DAILY", "WEEKLY", "MONTHLY", "CUSTOM", "CAMPAIGN"}
    if target_type not in supported_targets or period_type not in supported_periods:
        raise ValueError("Target type or measurement period is not supported")
    if (period_type == "CAMPAIGN") != (campaign_id is not None):
        raise ValueError("Campaign measurement periods require a campaign target")
    if sum(value is not None for value in (telegram_user_id, team_id, campaign_id)) != 1:
        raise ValueError("Choose exactly one salesperson, team, or campaign target scope")
    if telegram_user_id is not None:
        require_user_in_scope(conn, actor_id, telegram_user_id)
    if team_id is not None and get_user(conn, actor_id)["role"] != "SUPER_ADMIN":
        allowed_team = conn.execute(
            """select 1 from crm_team_memberships where team_id=%s and telegram_user_id=%s and active""",
            (team_id, actor_id),
        ).fetchone()
        if not allowed_team:
            raise PermissionError("Team is outside your target-management scope")
    if campaign_id is not None:
        require_permission(conn, actor_id, "campaigns.manage")
        if not conn.execute("select 1 from campaigns where id=%s", (campaign_id,)).fetchone():
            raise ValueError("Campaign was not found")
    if currency and (
        len(currency.strip()) != 3
        or not currency.strip().isascii()
        or not currency.strip().isalpha()
    ):
        raise ValueError("Currency must be a three-letter code")
    if target_type == "VERIFIED_SALES_VALUE" and not currency:
        raise ValueError("Verified sales value targets require a currency")
    row = conn.execute(
        """insert into team_targets(target_type,target_amount,currency,period_type,telegram_user_id,
             team_id,campaign_id,starts_at,ends_at,created_by)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
        (
            target_type.upper(),
            target_amount,
            currency.upper() if currency else None,
            period_type.upper(),
            telegram_user_id,
            team_id,
            campaign_id,
            starts_at.astimezone(UTC),
            ends_at.astimezone(UTC),
            actor_id,
        ),
    ).fetchone()
    conn.execute(
        """insert into team_target_history(target_id,new_value,changed_by,reason)
           values (%s,%s::jsonb,%s,'Target created')""",
        (
            row["id"],
            json.dumps({"target_amount": str(target_amount), "target_type": target_type}),
            actor_id,
        ),
    )
    _audit(
        conn,
        actor_id,
        "TEAM_TARGET_CREATED",
        "TEAM_TARGET",
        entity_id=row["id"],
        new_value={
            "target_type": target_type,
            "target_amount": str(target_amount),
            "period_type": period_type,
        },
    )
    return row


def update_target(
    conn: Connection,
    actor_id: int,
    target_id: UUID,
    *,
    target_amount: int | float,
    starts_at: datetime,
    ends_at: datetime,
    reason: str,
) -> dict[str, Any]:
    require_permission(conn, actor_id, "targets.manage_team")
    if not reason.strip() or len(reason) > 500:
        raise ValueError("Record a short reason for the target change")
    if starts_at.tzinfo is None or ends_at.tzinfo is None or starts_at >= ends_at:
        raise ValueError("Target dates must be timezone-aware and increasing")
    if float(target_amount) < 0:
        raise ValueError("Target amount must be zero or greater")
    old = conn.execute("select * from team_targets where id=%s for update", (target_id,)).fetchone()
    if not old:
        raise ValueError("Target was not found")
    if old["telegram_user_id"] is not None:
        require_user_in_scope(conn, actor_id, old["telegram_user_id"])
    if old["team_id"] is not None and get_user(conn, actor_id)["role"] != "SUPER_ADMIN":
        allowed = conn.execute(
            "select 1 from crm_team_memberships where team_id=%s and telegram_user_id=%s and active",
            (old["team_id"], actor_id),
        ).fetchone()
        if not allowed:
            raise PermissionError("Target is outside your team scope")
    result = conn.execute(
        """update team_targets set target_amount=%s,starts_at=%s,ends_at=%s,updated_at=now()
           where id=%s returning *""",
        (target_amount, starts_at.astimezone(UTC), ends_at.astimezone(UTC), target_id),
    ).fetchone()
    conn.execute(
        """insert into team_target_history(target_id,previous_value,new_value,changed_by,reason)
           values (%s,%s::jsonb,%s::jsonb,%s,%s)""",
        (
            target_id,
            json.dumps(
                {
                    "target_amount": str(old["target_amount"]),
                    "starts_at": old["starts_at"],
                    "ends_at": old["ends_at"],
                },
                default=str,
            ),
            json.dumps(
                {"target_amount": str(target_amount), "starts_at": starts_at, "ends_at": ends_at},
                default=str,
            ),
            actor_id,
            reason.strip(),
        ),
    )
    _audit(
        conn,
        actor_id,
        "TEAM_TARGET_UPDATED",
        "TEAM_TARGET",
        entity_id=target_id,
        old_value={
            "target_amount": str(old["target_amount"]),
            "starts_at": old["starts_at"],
            "ends_at": old["ends_at"],
        },
        new_value={"target_amount": str(target_amount), "starts_at": starts_at, "ends_at": ends_at},
        reason=reason.strip(),
    )
    return result


def _target_users(conn: Connection, target: dict[str, Any]) -> list[int]:
    if target["telegram_user_id"] is not None:
        return [int(target["telegram_user_id"])]
    if target["team_id"] is not None:
        rows = conn.execute(
            """select m.telegram_user_id from crm_team_memberships m join crm_users u using(telegram_user_id)
               where m.team_id=%s and m.active and u.role='SALESPERSON' and u.account_status='ACTIVE'""",
            (target["team_id"],),
        ).fetchall()
    else:
        rows = conn.execute(
            """select m.telegram_user_id from campaign_memberships m join crm_users u using(telegram_user_id)
               where m.campaign_id=%s and m.active and u.role='SALESPERSON' and u.account_status='ACTIVE'""",
            (target["campaign_id"],),
        ).fetchall()
    return [int(row["telegram_user_id"]) for row in rows]


def _target_actual(conn: Connection, target: dict[str, Any], user_ids: list[int]) -> Any:
    if not user_ids:
        return 0
    start, end = target["starts_at"], target["ends_at"]
    campaign = target["campaign_id"]
    queries = {
        "CALLS_LOGGED": """select count(*) amount from call_logs l join call_sessions s on s.id=l.call_session_id
          where s.telegram_user_id=any(%s) and l.created_at>=%s and l.created_at<%s and (%s::uuid is null or l.campaign_id=%s)""",
        "UNIQUE_CONTACTS_REACHED": """select count(distinct l.contact_id) amount from call_logs l join call_sessions s on s.id=l.call_session_id
          where s.telegram_user_id=any(%s) and l.created_at>=%s and l.created_at<%s
            and (l.connection_result='ANSWERED' or (l.outcome_category='CONNECTION' and l.outcome_code='ANSWERED'))
            and (%s::uuid is null or l.campaign_id=%s)""",
        "QUALIFIED_BUYERS": """select count(distinct l.contact_id) amount from call_logs l join call_sessions s on s.id=l.call_session_id
          where s.telegram_user_id=any(%s) and l.created_at>=%s and l.created_at<%s
            and l.outcome_code in ('QUALIFIED','HIGHLY_INTERESTED') and (%s::uuid is null or l.campaign_id=%s)""",
        "FOLLOWUPS_COMPLETED": """select count(*) amount from follow_ups f where f.completed_by=any(%s)
          and f.status='COMPLETED' and f.completed_at>=%s and f.completed_at<%s and (%s::uuid is null or f.campaign_id=%s)""",
        "VISITS_COMPLETED": """select count(*) amount from property_visits v where v.created_by=any(%s)
          and v.visit_status='COMPLETED' and v.updated_at>=%s and v.updated_at<%s""",
        "OPPORTUNITIES_CREATED": """select count(*) amount from sales_opportunities o where o.created_by=any(%s)
          and o.created_at>=%s and o.created_at<%s and (%s::uuid is null or o.campaign_id=%s)""",
        "RESERVATIONS_RECORDED": """select count(*) amount from reservations r where r.created_by=any(%s)
          and r.created_at>=%s and r.created_at<%s""",
        "VERIFIED_SALES_COUNT": """select count(*) amount from sale_records s where s.responsible_telegram_user_id=any(%s)
          and s.verification_status='VERIFIED' and s.verified_at>=%s and s.verified_at<%s""",
        "VERIFIED_SALES_VALUE": """select coalesce(sum(s.recorded_sale_price),0) amount from sale_records s
          where s.responsible_telegram_user_id=any(%s) and s.verification_status='VERIFIED'
            and s.verified_at>=%s and s.verified_at<%s and s.currency=%s""",
    }
    query = queries[target["target_type"]]
    params: tuple[Any, ...]
    if target["target_type"] == "VERIFIED_SALES_VALUE":
        params = (user_ids, start, end, target["currency"])
    elif target["target_type"] in {
        "CALLS_LOGGED",
        "UNIQUE_CONTACTS_REACHED",
        "QUALIFIED_BUYERS",
        "FOLLOWUPS_COMPLETED",
        "OPPORTUNITIES_CREATED",
    }:
        params = (user_ids, start, end, campaign, campaign)
    else:
        params = (user_ids, start, end)
    row = conn.execute(query, params).fetchone()
    return row["amount"] if row else 0


def list_targets(
    conn: Connection, actor_id: int, user_id: int | None = None
) -> list[dict[str, Any]]:
    actor = get_user(conn, actor_id)
    if not actor or actor["account_status"] != "ACTIVE":
        raise PermissionError("This CRM account is inactive")
    if actor["role"] == "SUPER_ADMIN":
        scope_sql = "true"
        params: tuple[Any, ...] = ()
    elif actor["role"] == "SALES_MANAGER":
        require_permission(conn, actor_id, "targets.manage_team")
        scope_sql = """(exists(select 1 from crm_team_memberships mine where mine.telegram_user_id=%s
             and mine.active and mine.team_id=t.team_id)
           or exists(select 1 from crm_team_memberships mine join crm_team_memberships member
             on member.team_id=mine.team_id and member.active where mine.telegram_user_id=%s and mine.active
               and t.telegram_user_id=member.telegram_user_id))"""
        params = (actor_id, actor_id)
    else:
        require_permission(conn, actor_id, "targets.self")
        scope_sql = "t.telegram_user_id=%s"
        params = (actor_id,)
    can_view_financial = permission_allowed(
        conn, actor_id, "commissions.team"
    ) or permission_allowed(conn, actor_id, "commissions.self")
    rows = conn.execute(
        f"""select t.*,u.display_name,team.name team_name from team_targets t
           left join crm_users u on u.telegram_user_id=t.telegram_user_id
           left join crm_teams team on team.id=t.team_id
           where t.status='ACTIVE' and ({scope_sql})
             and (t.target_type<>'VERIFIED_SALES_VALUE' or %s)
           order by t.ends_at,t.created_at limit 100""",
        (*params, can_view_financial),
    ).fetchall()
    for row in rows:
        row["actual"] = _target_actual(conn, row, _target_users(conn, row))
    return rows


def team_performance(
    conn: Connection,
    actor_id: int,
    start: datetime,
    end: datetime,
    *,
    user_id: int | None = None,
    team_id: UUID | None = None,
) -> list[dict[str, Any]]:
    actor = get_user(conn, actor_id)
    if not actor or actor["account_status"] != "ACTIVE":
        raise PermissionError("This CRM account is inactive")
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("Report range must use timezone-aware, increasing dates")
    if actor["role"] == "SALESPERSON":
        require_permission(conn, actor_id, "reports.self")
        user_id = actor_id
        team_id = None
    elif user_id is not None and actor["role"] != "SUPER_ADMIN":
        require_permission(conn, actor_id, "reports.team")
        require_permission(conn, actor_id, "members.read_team")
        require_user_in_scope(conn, actor_id, user_id)
        team_id = None
    elif team_id is not None and actor["role"] != "SUPER_ADMIN":
        require_permission(conn, actor_id, "reports.team")
        allowed = conn.execute(
            """select 1 from crm_team_memberships m join crm_teams t on t.id=m.team_id and t.active
               where m.team_id=%s and m.telegram_user_id=%s and m.active""",
            (team_id, actor_id),
        ).fetchone()
        if not allowed:
            raise PermissionError("Team is outside your manager scope")
    else:
        require_permission(conn, actor_id, "reports.team")
        if actor["role"] != "SUPER_ADMIN":
            raise PermissionError("Choose an authorized team before viewing team performance")
    rows = conn.execute(
        """select u.telegram_user_id,u.display_name,
             count(l.id) calls_logged,
             count(l.id) filter(where l.connection_result='ANSWERED'
               or (l.outcome_category='CONNECTION' and l.outcome_code='ANSWERED')) answered_calls,
             count(distinct l.contact_id) filter(where l.connection_result='ANSWERED'
               or (l.outcome_category='CONNECTION' and l.outcome_code='ANSWERED')) unique_contacts_reached,
             count(distinct l.contact_id) filter(where l.outcome_code in ('QUALIFIED','HIGHLY_INTERESTED')) qualified_buyers,
             (select count(*) from follow_ups f where f.completed_by=u.telegram_user_id and f.status='COMPLETED'
               and f.completed_at>=%s and f.completed_at<%s) followups_completed,
             (select count(*) from property_visits v where v.created_by=u.telegram_user_id and v.visit_status='COMPLETED'
               and v.updated_at>=%s and v.updated_at<%s) visits_completed,
             (select count(*) from sales_opportunities o where o.created_by=u.telegram_user_id
               and o.created_at>=%s and o.created_at<%s) opportunities_created,
             (select count(*) from sale_records s where s.responsible_telegram_user_id=u.telegram_user_id
               and s.verification_status='VERIFIED' and s.verified_at>=%s and s.verified_at<%s) verified_sales,
             (select coalesce(jsonb_object_agg(currency,amount), '{}'::jsonb) from (
                select s.currency,sum(s.recorded_sale_price) amount from sale_records s
                 where s.responsible_telegram_user_id=u.telegram_user_id and s.verification_status='VERIFIED'
                   and s.verified_at>=%s and s.verified_at<%s group by s.currency
              ) totals) verified_sales_by_currency
           from crm_users u
           left join call_sessions cs on cs.telegram_user_id=u.telegram_user_id
           left join call_logs l on l.call_session_id=cs.id and l.created_at>=%s and l.created_at<%s
           where u.account_status='ACTIVE' and u.role='SALESPERSON'
             and (%s::bigint is null or u.telegram_user_id=%s)
             and (%s::uuid is null or exists(select 1 from crm_team_memberships m where m.team_id=%s
                and m.telegram_user_id=u.telegram_user_id and m.active))
             and (%s::uuid is null or exists(select 1 from crm_team_memberships m where m.team_id=%s
                and m.telegram_user_id=%s and m.active))
           group by u.telegram_user_id order by qualified_buyers desc,verified_sales desc,calls_logged desc""",
        (
            start.astimezone(UTC),
            end.astimezone(UTC),
            start.astimezone(UTC),
            end.astimezone(UTC),
            start.astimezone(UTC),
            end.astimezone(UTC),
            start.astimezone(UTC),
            end.astimezone(UTC),
            start.astimezone(UTC),
            end.astimezone(UTC),
            start.astimezone(UTC),
            end.astimezone(UTC),
            user_id,
            user_id,
            team_id,
            team_id,
            team_id,
            team_id,
            actor_id,
        ),
    ).fetchall()
    if not (
        permission_allowed(conn, actor_id, "commissions.team")
        or permission_allowed(conn, actor_id, "commissions.self")
    ):
        for row in rows:
            row["verified_sales_by_currency"] = {}
    return rows

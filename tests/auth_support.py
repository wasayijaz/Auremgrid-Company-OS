from __future__ import annotations

from typing import Any


def issue_identity(
    os: Any,
    organization_id: str,
    person_id: str,
    workspace_id: str | None = None,
    actor_id: str | None = None,
) -> tuple[str, Any]:
    person = os.company.get_person(organization_id, person_id)
    email = person.email if person and person.email else f"{person_id}@auth.test"
    principal = os.auth.create_principal(organization_id, person_id, email)
    session = os.auth.create_session(principal["id"])
    base_identity = os.auth.authenticate_session(session["token"])
    if workspace_id and actor_id:
        os.auth.bind_actor(base_identity, workspace_id, actor_id)
    identity = os.auth.authenticate_session(session["token"], workspace_id=workspace_id)
    return session["token"], identity

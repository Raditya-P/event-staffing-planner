"""Who is calling: turn a signed-in identity into an Actor with a workspace.

A new user gets their own workspace, pre-filled with the demo theme park, so
anyone can try the tool without seeing anyone else's data.
"""

from __future__ import annotations

import logging
import secrets

from sqlalchemy import select

from .config import settings
from .db import Membership, User, Workspace, session_scope, utcnow
from .services import Actor

log = logging.getLogger(__name__)


def local_actor(channel: str) -> Actor:
    """Single-user mode (AUTH_MODE=none)."""
    return Actor(settings.default_actor, settings.default_workspace, channel)


def actor_for_identity(issuer: str, subject: str, email: str | None, name: str | None, channel: str) -> Actor:
    """Find or create the user and their workspace. Called on every authenticated request."""
    from .seed import ensure_demo_data

    created_workspace = None
    with session_scope() as s:
        user = s.scalars(select(User).where(User.issuer == issuer, User.subject == subject)).first()
        if user is None:
            user = User(issuer=issuer, subject=subject, email=email, name=name)
            s.add(user)
            s.flush()
            log.info("new user %s", user.id)
        else:
            user.last_seen_at = utcnow()
            if email and user.email != email:
                user.email = email
            if name and user.name != name:
                user.name = name
        membership = s.scalars(
            select(Membership).where(Membership.user_id == user.id).order_by(Membership.created_at)
        ).first()
        if membership is None:
            workspace_id = f"ws_{secrets.token_hex(6)}"
            label = email or name or "My"
            s.add(Workspace(id=workspace_id, name=f"{label} workspace"))
            s.flush()
            s.add(Membership(user_id=user.id, workspace_id=workspace_id, role="owner"))
            created_workspace = workspace_id
        else:
            workspace_id = membership.workspace_id
        display = user.email or user.name or user.id
        user_id = user.id

    if created_workspace:
        ensure_demo_data(created_workspace, actor="system", create_workspace=False)
    return Actor(display, workspace_id, channel, user_id=user_id)

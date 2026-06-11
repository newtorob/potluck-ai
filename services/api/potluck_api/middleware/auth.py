from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from potluck_api.db.database import get_db

# Forward reference — imported at call time to avoid circular imports at module load.
# The actual Member model lives in potluck_api.models.member.
# Full implementation in Task 6 (auth service).


async def get_current_member(
    potluck_session: Annotated[str | None, Cookie()] = None,
    db: AsyncSession = Depends(get_db),
) -> object:
    """FastAPI dependency that validates the session cookie and returns a Member.

    The session cookie value is an opaque random token. We SHA-256 hash it and
    look up the matching row in the sessions table. A full implementation is in
    Task 6 (auth service). This scaffold raises 501 until that task is complete.

    Expected cookie name: ``potluck_session``

    Raises:
        HTTPException 401 — missing or expired session.
        HTTPException 501 — not yet implemented (scaffold only).
    """
    if potluck_session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    # TODO (Task 6): replace with real session lookup:
    #   token_hash = hashlib.sha256(potluck_session.encode()).hexdigest()
    #   session = await db.scalar(
    #       select(Session).where(
    #           Session.token_hash == token_hash,
    #           Session.expires_at > datetime.now(timezone.utc),
    #       )
    #   )
    #   if not session:
    #       raise HTTPException(status_code=401, detail="Session expired or invalid")
    #   member = await db.get(Member, session.member_id)
    #   if not member:
    #       raise HTTPException(status_code=401, detail="Member not found")
    #   return member

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Auth middleware not yet implemented — see Task 6",
    )


async def require_admin(
    member: Annotated[object, Depends(get_current_member)],
) -> object:
    """Dependency that further asserts the authenticated member is an admin.

    Fail-closed: if the member is not an admin, raise 403. This must stay a real
    check (not a passthrough) so that when ``get_current_member`` is wired up in
    Task 6, admin routes are not silently exposed to every authenticated member.
    Until then ``get_current_member`` raises 501 and this is never reached, but
    the assertion is in place now so the gate cannot be forgotten.
    """
    if not getattr(member, "is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return member

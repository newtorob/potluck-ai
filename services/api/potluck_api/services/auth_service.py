"""Magic link generation, JWT signing, session management.

Full implementation spec: guide Section 5.5.
Task: 6

Responsibilities:
- create_magic_link(email) -> str  — signs a JWT, returns the full callback URL
- verify_magic_link(token) -> tuple[Member, str]  — decodes JWT, finds/creates
  member, creates session row, returns (member, opaque session token)
- validate_session(session_token) -> Member | None  — hashes the opaque token,
  looks up unexpired session row, returns Member
"""
from __future__ import annotations


class AuthService:
    pass

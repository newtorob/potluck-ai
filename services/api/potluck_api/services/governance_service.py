"""Governance — proposals and member voting.

Task: 9

Responsibilities:
- list_active() -> list[Proposal]
- get_proposal(proposal_id) -> Proposal | None
- cast_vote(member, proposal_id, choice) -> Vote
  - idempotent: updates existing vote if member already voted
  - validates choice against proposal.allowed_choices
"""
from __future__ import annotations


class GovernanceService:
    pass

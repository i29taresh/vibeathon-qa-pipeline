"""Deterministic mock outcome tables for the Phase 3 placeholder agents.

These are lookup tables, not randomness: the same scenario name always
produces the same sequence of pass/fail results. nodes/qa_agent.py and
nodes/retest_agent.py read their outcome from here instead of deciding it
themselves, so the two files stay purely about orchestration/state updates.
"""

from __future__ import annotations

from typing import Optional, TypedDict


class _Scenario(TypedDict):
    description: str
    initial_qa_passes: bool
    retest_passes_at_attempt: Optional[int]


SCENARIOS: dict[str, _Scenario] = {
    "initial_pass": {
        "description": "QA passes immediately; no ticket is ever created.",
        "initial_qa_passes": True,
        "retest_passes_at_attempt": None,
    },
    "fix_success": {
        "description": "Initial QA fails; the first retest (attempt 1) passes.",
        "initial_qa_passes": False,
        "retest_passes_at_attempt": 1,
    },
    "retry_success": {
        "description": "Initial QA fails; retest fails once, then passes on attempt 2.",
        "initial_qa_passes": False,
        "retest_passes_at_attempt": 2,
    },
    "always_fail": {
        "description": "Initial QA fails; every retest fails until max_attempts is hit.",
        "initial_qa_passes": False,
        "retest_passes_at_attempt": None,
    },
}


def initial_qa_passes(scenario: str) -> bool:
    """Whether the very first (pre-fix) QA run passes for this scenario."""
    return SCENARIOS[scenario]["initial_qa_passes"]


def retest_passes(scenario: str, attempt_count: int) -> bool:
    """Whether a retest at this attempt number passes for this scenario."""
    target = SCENARIOS[scenario]["retest_passes_at_attempt"]
    if target is None:
        return False
    return attempt_count >= target

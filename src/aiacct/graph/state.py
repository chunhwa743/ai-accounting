"""State carried between pipeline nodes.

This is an in-memory dictionary, not a prompt. Passing data between nodes costs
nothing, and only what a node puts into a prompt costs tokens - which is why
each node assembles its own small prompt from the slice it needs rather than
carrying one growing context around. A ReAct agent cannot make that separation,
because its state *is* its context window.

The database remains the source of truth. This state holds ids and working
values so a resumed run does not have to reconstruct them.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict


def _merge(existing: list | None, incoming: list | None) -> list:
    return [*(existing or []), *(incoming or [])]


class PipelineState(TypedDict, total=False):
    # ---- identity ----
    run_id: int
    client_id: int
    user_id: int | None
    file_paths: list[str]

    # ---- phase 1 ----
    document_ids: list[int]
    statement_ids: list[int]
    supporting_ids: list[int]
    extraction_attempts: dict[str, int]
    # Blocking problems that stop the run at gate 1: an unreadable identifier,
    # a statement that will not reconcile after retries, an unclassifiable file.
    extraction_issues: Annotated[list[dict[str, Any]], _merge]
    gate1_required: bool
    gate1_resolved: bool

    # ---- phase 2 ----
    transaction_ids: list[int]
    resolved_by_rule: int
    resolved_by_llm: int
    document_matches: dict[str, Any]
    allocation_ids: list[int]

    # ---- review ----
    gate2_required: bool
    review_complete: bool

    # ---- reporting ----
    llm_calls: int
    input_tokens: int
    output_tokens: int
    log: Annotated[list[str], _merge]
    error: str | None


def new_state(run_id: int, client_id: int, file_paths: list[str], user_id: int | None) -> PipelineState:
    return PipelineState(
        run_id=run_id,
        client_id=client_id,
        user_id=user_id,
        file_paths=file_paths,
        document_ids=[],
        statement_ids=[],
        supporting_ids=[],
        extraction_attempts={},
        extraction_issues=[],
        gate1_required=False,
        gate1_resolved=False,
        transaction_ids=[],
        resolved_by_rule=0,
        resolved_by_llm=0,
        document_matches={},
        allocation_ids=[],
        gate2_required=False,
        review_complete=False,
        llm_calls=0,
        input_tokens=0,
        output_tokens=0,
        log=[],
        error=None,
    )

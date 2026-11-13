import sqlite3


class TransitionError(Exception):
    pass


class StaleStateError(TransitionError):
    pass


PROSPECT_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("discovered", "scored"): frozenset({"system"}),
    ("discovered", "filtered_out"): frozenset({"system"}),
    ("scored", "drafted"): frozenset({"system", "human"}),
    ("scored", "no_evidence"): frozenset({"system"}),
    ("scored", "filtered_out"): frozenset({"system", "human"}),
    ("no_evidence", "drafted"): frozenset({"human"}),
    ("drafted", "drafted"): frozenset({"system", "human"}),
    ("drafted", "rejected"): frozenset({"human"}),
    ("drafted", "queued"): frozenset({"human"}),
    ("drafted", "filtered_out"): frozenset({"system"}),
    ("rejected", "drafted"): frozenset({"human"}),
}

OUTREACH_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("approved", "sending"): frozenset({"system"}),
    ("approved", "blocked"): frozenset({"system"}),
    ("approved", "cancelled"): frozenset({"human"}),
    ("cancelled", "approved"): frozenset({"human"}),
    ("sending", "sent"): frozenset({"system"}),
    ("sending", "blocked"): frozenset({"system"}),
    ("sending", "failed"): frozenset({"system"}),
    ("sending", "unknown"): frozenset({"system"}),
    ("sending", "approved"): frozenset({"system"}),
    ("failed", "approved"): frozenset({"human"}),
    ("blocked", "approved"): frozenset({"human"}),
    ("unknown", "sent"): frozenset({"system", "human"}),
    ("unknown", "approved"): frozenset({"human"}),
    ("sent", "replied"): frozenset({"system", "human"}),
}

OUTREACH_EXTRA_FIELDS = frozenset(
    {"x_dm_event_id", "x_dm_conversation_id", "attempt_started_at", "sent_at"}
)
PROSPECT_EXTRA_FIELDS = frozenset(
    {"score", "fit_reasons", "segment", "role", "caution"}
)


def transition(
    conn: sqlite3.Connection,
    entity: str,
    entity_id: int,
    from_state: str,
    to_state: str,
    actor: str,
    now: str,
    reason: str | None = None,
    **fields,
) -> None:
    if entity == "prospect":
        table = "prospects"
        transitions = PROSPECT_TRANSITIONS
        allowed_extras = PROSPECT_EXTRA_FIELDS
    elif entity == "outreach":
        table = "outreach"
        transitions = OUTREACH_TRANSITIONS
        allowed_extras = OUTREACH_EXTRA_FIELDS
    else:
        raise TransitionError(f"Unknown entity: {entity}")

    pair = (from_state, to_state)
    if pair not in transitions:
        raise TransitionError(
            f"Transition {from_state} -> {to_state} not allowed for {entity}"
        )

    allowed_actors = transitions[pair]
    if actor not in allowed_actors:
        raise TransitionError(
            f"Actor '{actor}' not allowed for {entity} transition {from_state} -> {to_state}"
        )

    if entity == "outreach" and from_state == "sending" and to_state == "approved":
        if reason != "rate_limited":
            raise TransitionError(
                "outreach transition sending -> approved requires reason 'rate_limited'"
            )

    disallowed_fields = set(fields.keys()) - allowed_extras
    if disallowed_fields:
        raise TransitionError(
            f"Disallowed extra fields for {entity}: {disallowed_fields}"
        )

    params: list = [to_state, reason, now]
    extra_clauses = []

    if entity == "outreach" and to_state == "sending":
        extra_clauses.append("attempts = attempts + 1")

    for col in sorted(fields.keys()):
        extra_clauses.append(f"{col} = ?")
        params.append(fields[col])

    extra_sql = (", " + ", ".join(extra_clauses)) if extra_clauses else ""
    params.extend([entity_id, from_state])
    sql = f"UPDATE {table} SET state = ?, state_reason = ?, updated_at = ?{extra_sql} WHERE id = ? AND state = ?"
    cur = conn.execute(sql, params)
    if cur.rowcount != 1:
        raise StaleStateError(
            f"Expected {entity} {entity_id} to be in state '{from_state}', but update affected {cur.rowcount} rows"
        )

    conn.execute(
        "INSERT INTO audit_log (entity, entity_id, from_state, to_state, actor, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entity, entity_id, from_state, to_state, actor, reason, now),
    )


def record_insert(
    conn: sqlite3.Connection,
    entity: str,
    entity_id: int,
    to_state: str,
    actor: str,
    now: str,
    reason: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (entity, entity_id, from_state, to_state, actor, reason, created_at) "
        "VALUES (?, ?, NULL, ?, ?, ?, ?)",
        (entity, entity_id, to_state, actor, reason, now),
    )

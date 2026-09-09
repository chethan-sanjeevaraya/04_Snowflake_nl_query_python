"""Session memory + automatic table shortlisting for the text-to-SQL Lambda.

Kept in its own module so it can be unit-tested without Snowflake, Bedrock or
DynamoDB, and so lambda_function.py stays readable.

WHAT IS STORED IN DYNAMODB (for data-governance review)
-------------------------------------------------------
Per session: session_id, database, schema, created_at, updated_at, ttl.
Per turn:    the user's question, the generated SQL, the tables used,
             row_count, total_matching_rows, timestamp.

RESULT ROWS ARE NEVER PERSISTED. There is deliberately no parameter, flag or
code path in this module that writes query results to DynamoDB -- `append_turn`
does not accept rows at all, so it cannot happen by configuration mistake.
Every row stays inside Snowflake, where the existing access controls apply.

A result-referencing follow-up ("what was the second one about?") is served by
RE-RUNNING the stored SQL for the current turn and passing those freshly
fetched rows to the model transiently, via `requery_block`. Those rows exist in
memory for the duration of one request and are never written to the session
store.

Note that the re-queried rows are still sent to Bedrock as prompt content for
that turn, exactly as sampled column values already are. If prompt content is
itself in scope for review, that is the place to look -- not the session store.
"""

import json
import os
import re
import time
import uuid

import boto3

SESSION_TABLE = os.environ.get("SESSION_TABLE", "")
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", str(8 * 3600)))
# Turns kept in the record (and therefore available to the prompt).
SESSION_MAX_TURNS = int(os.environ.get("SESSION_MAX_TURNS", "8"))
# DynamoDB's hard item limit is 400 KB. Stay under it with headroom -- eight
# turns of generated SQL plus questions is small, but a pathological query can
# still be long.
SESSION_MAX_ITEM_BYTES = int(os.environ.get("SESSION_MAX_ITEM_BYTES", "350000"))

# Rows from a re-query that get shown to the model for the CURRENT turn only.
# Transient: never stored. Kept small because they are prompt tokens.
REQUERY_ROW_PREVIEW = int(os.environ.get("REQUERY_ROW_PREVIEW", "10"))

_ddb = None


def _client():
    """Lazy so importing this module needs no credentials (and tests can
    monkeypatch the module-level _ddb)."""
    global _ddb
    if _ddb is None:
        _ddb = boto3.client("dynamodb", region_name=os.environ.get(
            "region", os.environ.get("AWS_REGION", "eu-central-1")))
    return _ddb


# --------------------------------------------------------------------------
# Follow-up detection
# --------------------------------------------------------------------------

# Markers that a question continues the previous one rather than starting fresh.
# A heuristic on purpose: an LLM classification call would add a round trip and
# defeat the point of reusing the previous turn's tables.
_FOLLOWUP_MARKERS = (
    "those", "them", "these", "the same", "same but", "same for",
    "also", "instead", "as well", "what about", "how about", "drill down",
    "narrow", "refine", "only the", "just the", "of these", "from those",
    "previous", "last query", "earlier", "second", "third", "which of",
)
# A leading continuation word ("now ...", "and ...") is a strong signal, but
# only at the start -- "show events now open" is not a follow-up.
_FOLLOWUP_PREFIXES = ("now ", "and ", "but ", "then ", "ok now ", "okay now ")


def looks_like_followup(question):
    """True when the question probably refines the previous turn.

    Asymmetric by design. A false negative costs one extra table-selection call
    (slower, still correct). A false positive reuses the wrong tables, so the
    caller logs the decision on every turn to make this tunable from real
    transcripts.
    """
    q = (question or "").strip().lower()
    if any(q.startswith(p) for p in _FOLLOWUP_PREFIXES):
        return True
    return any(m in q for m in _FOLLOWUP_MARKERS)


# Word-boundary matched: 'the one' must not fire on 'the ones', which is a
# refinement ("only the ones after 2024") rather than a reference to a specific
# returned row.
_RESULT_REF_RE = re.compile(
    r"\b(?:"
    r"second|third|fourth|first one|the one|that one|this one|"
    r"which of|that row|this row|row \d+|of these|of those|listed above"
    r")\b"
)


def references_results(question):
    """True when the follow-up is about the returned ROWS rather than the query
    shape -- the case that requires re-running the stored SQL."""
    return bool(_RESULT_REF_RE.search((question or "").lower()))


# --------------------------------------------------------------------------
# Stage 0: lexical table shortlist (no LLM call)
# --------------------------------------------------------------------------

_STOPWORDS = {
    "a", "all", "am", "an", "and", "any", "are", "as", "at", "be", "been", "by",
    "can", "count", "did", "do", "does", "each", "every", "for", "from", "get",
    "give", "has", "have", "how", "i", "in", "is", "it", "its", "list", "many",
    "me", "much", "my", "of", "on", "only", "or", "our", "please", "show",
    "that", "the", "their", "them", "then", "there", "these", "this", "those",
    "to", "was", "we", "were", "what", "when", "where", "which", "who", "why",
    "will", "with", "you", "your", "sys", "v", "c",
}


def _tokens(text):
    """Lowercase alphanumeric tokens, minus stopwords, plus a crude singular
    form so 'events' in the question matches the EM_EVENT table."""
    raw = [t for t in re.split(r"[^a-zA-Z0-9]+", (text or "").lower()) if t]
    out = set()
    for t in raw:
        if t in _STOPWORDS or len(t) < 2:
            continue
        out.add(t)
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            out.add(t[:-1])
    return out


# A table-name hit is far more informative than a column hit.
_NAME_WEIGHT = 3
# Cap the column contribution: a 250-column Vault table would otherwise
# outrank a well-named narrow table purely by surface area.
_MAX_COLUMN_HITS = 10


def _score(question_tokens, table, cols):
    name_hits = len(question_tokens & _tokens(table))
    col_tokens = set()
    for c, _ in cols:
        col_tokens |= _tokens(c)
    col_hits = min(len(question_tokens & col_tokens), _MAX_COLUMN_HITS)
    return name_hits * _NAME_WEIGHT + col_hits


def shortlist_tables(question, catalog, k=25):
    """Return up to k candidate table names, best first.

    Bounds the stage-1 prompt to a constant size no matter how large the schema
    is. Without this, _compact_catalog_text emits every table with every column
    name -- tens of thousands of identifiers for a full Vault schema.
    """
    if len(catalog) <= k:
        return list(catalog)

    q_tokens = _tokens(question)
    if not q_tokens:
        return list(catalog)[:k]

    scored = [(_score(q_tokens, t, cols), t) for t, cols in catalog.items()]
    # Deterministic: score desc, then name asc, so identical scores don't
    # reorder between invocations.
    scored.sort(key=lambda st: (-st[0], st[1]))
    return [t for _, t in scored[:k]]


def shortlist_debug(question, catalog, k=25):
    """(score, table) pairs for logging -- makes a miss visible instead of
    silent when the right table falls outside the shortlist."""
    q_tokens = _tokens(question)
    rows = [(_score(q_tokens, t, cols), t) for t, cols in catalog.items()]
    rows.sort(key=lambda st: (-st[0], st[1]))
    return rows[:k]


# --------------------------------------------------------------------------
# Prompt blocks
# --------------------------------------------------------------------------

def history_block(turns, max_turns=None):
    """Render prior turns for the prompt: question, tables, SQL, row counts.

    Deliberately includes the previous SQL -- a refinement should start from it
    and change only what the user asked. Rewriting from scratch is how filters
    get silently dropped.

    Contains no result data, because none is stored.
    """
    if not turns:
        return ""
    limit = max_turns or SESSION_MAX_TURNS
    recent = turns[-limit:]

    lines = ["Conversation so far (most recent last):"]
    for t in recent:
        lines.append(f"  Turn {t.get('turn')}  Q: {t.get('question')!r}")
        if t.get("tables"):
            lines.append(f"          tables: {', '.join(t['tables'])}")
        if t.get("sql"):
            lines.append(f"          SQL: {t['sql']}")
        total = t.get("total_matching_rows")
        count = t.get("row_count")
        if count is not None:
            lines.append(
                f"          -> {count} row(s)"
                + (f" of {total} matching" if total is not None and total != count else "")
            )
    return "\n".join(lines)


def requery_block(rows, sql):
    """Render freshly re-queried rows for a result-referencing follow-up.

    These rows were fetched during THIS request by re-running the stored SQL.
    They are prompt content for one turn and are not persisted anywhere.
    """
    if not rows:
        return ""
    preview = rows[:REQUERY_ROW_PREVIEW]
    return (
        "Rows from the previous query, re-run just now (the user is referring "
        f"to these; they are numbered from 1):\n  SQL: {sql}\n"
        + "\n".join(
            f"  {i}. {json.dumps(r, default=str)}" for i, r in enumerate(preview, 1)
        )
    )


# --------------------------------------------------------------------------
# DynamoDB session store
# --------------------------------------------------------------------------

def enabled():
    """Memory is inert unless a table name is configured, so the Lambda keeps
    working (statelessly) if SESSION_TABLE is unset."""
    return bool(SESSION_TABLE)


def new_session(database, schema):
    now = int(time.time())
    return {
        "session_id": str(uuid.uuid4()),
        "database": database,
        "schema": schema,
        "created_at": now,
        "updated_at": now,
        "turns": [],
        "pending": None,
    }


def load_session(session_id):
    """Return the session dict, or None if absent/expired/unreadable.

    Never raises: a memory failure must degrade to a stateless answer rather
    than fail the user's request.
    """
    if not enabled() or not session_id:
        return None
    try:
        resp = _client().get_item(
            TableName=SESSION_TABLE,
            Key={"session_id": {"S": session_id}},
            ConsistentRead=True,
        )
    except Exception:  # noqa: BLE001
        return None

    item = resp.get("Item")
    if not item:
        return None

    # DynamoDB TTL deletion is asynchronous and can lag by hours, so an expired
    # item may still be returned -- check it ourselves.
    ttl = int(item.get("ttl", {}).get("N", "0") or 0)
    if ttl and ttl < time.time():
        return None

    try:
        turns = json.loads(item.get("turns_json", {}).get("S", "[]"))
    except (ValueError, TypeError):
        turns = []

    try:
        pending = json.loads(item.get("pending_json", {}).get("S", "null"))
    except (ValueError, TypeError):
        pending = None

    return {
        "session_id": item["session_id"]["S"],
        "database": item.get("database", {}).get("S", ""),
        "schema": item.get("schema", {}).get("S", ""),
        "created_at": int(item.get("created_at", {}).get("N", "0") or 0),
        "updated_at": int(item.get("updated_at", {}).get("N", "0") or 0),
        "turns": turns if isinstance(turns, list) else [],
        "pending": pending if isinstance(pending, dict) else None,
    }


def append_turn(session, question, sql, tables, row_count, total_matching_rows):
    """Append a turn to the session.

    There is intentionally no `rows` parameter. Result rows are not stored, so
    row caching cannot be re-enabled by configuration -- it would take a code
    change, which is the point.
    """
    turn = {
        "turn": len(session["turns"]) + 1,
        "question": question,
        "sql": sql,
        "tables": list(tables or []),
        "row_count": row_count,
        "total_matching_rows": total_matching_rows,
        "ts": int(time.time()),
    }
    session["turns"].append(turn)
    session["turns"] = session["turns"][-SESSION_MAX_TURNS:]
    session["updated_at"] = int(time.time())
    return turn


def _serialize_turns(turns):
    """JSON-encode turns, shedding oldest turns until the payload fits under
    SESSION_MAX_ITEM_BYTES. Returns (json_string, notes)."""
    notes = []
    working = [dict(t) for t in turns]

    def size(obj):
        return len(json.dumps(obj, default=str).encode("utf-8"))

    while len(working) > 1 and size(working) > SESSION_MAX_ITEM_BYTES:
        dropped = working.pop(0)
        notes.append(f"dropped turn {dropped.get('turn')} (item size)")

    # A single turn that still does not fit means one enormous SQL string.
    # Truncate it rather than failing the write and losing the session.
    if size(working) > SESSION_MAX_ITEM_BYTES and working:
        keep = max(1000, SESSION_MAX_ITEM_BYTES // 2)
        original = len(working[-1].get("sql") or "")
        working[-1]["sql"] = (working[-1].get("sql") or "")[:keep]
        notes.append(f"truncated oversized SQL on turn {working[-1].get('turn')} "
                     f"({original} -> {keep} chars)")

    return json.dumps(working, default=str), notes


def save_session(session):
    """Persist the session. Returns (ok, notes). Never raises."""
    if not enabled():
        return False, ["session table not configured"]

    turns_json, notes = _serialize_turns(session.get("turns", []))
    pending_json = json.dumps(session.get("pending"), default=str)
    now = int(time.time())
    try:
        _client().put_item(
            TableName=SESSION_TABLE,
            Item={
                "session_id": {"S": session["session_id"]},
                "database": {"S": session.get("database", "")},
                "schema": {"S": session.get("schema", "")},
                "created_at": {"N": str(session.get("created_at", now))},
                "updated_at": {"N": str(now)},
                "ttl": {"N": str(now + SESSION_TTL_SECONDS)},
                "turns_json": {"S": turns_json},
                "pending_json": {"S": pending_json},
            },
        )
        return True, notes
    except Exception as exc:  # noqa: BLE001
        return False, notes + [f"put_item failed: {exc}"]


# --------------------------------------------------------------------------
# Pending clarification: set when the Lambda couldn't confidently resolve a
# table and asked the user a follow-up question instead of erroring out. The
# NEXT turn on this session is checked against it before anything else, so a
# short reply like "the events one" or "2" answers the question rather than
# being treated as a brand-new (and probably unresolvable) query.
# --------------------------------------------------------------------------

def set_pending(session, question, candidates):
    """Record that the NEXT turn on this session should be interpreted as the
    answer to a clarification question, not a fresh question."""
    session["pending"] = {
        "question": question,
        "candidates": list(candidates or []),
        "ts": int(time.time()),
    }


def get_pending(session):
    return (session or {}).get("pending")


def clear_pending(session):
    if session is not None:
        session["pending"] = None


# Words that show up specifically in a clarification REPLY ("the events ONE",
# "I MEAN accounts", "the accounts TABLE please") but, unlike _STOPWORDS,
# aren't excluded from a normal question because they can matter there. Kept
# separate rather than folded into _STOPWORDS for that reason.
_CLARIFICATION_FILLER_WORDS = {"one", "ones", "mean", "meant", "yes", "no", "just", "table", "tables"}


def is_bare_table_pick(answer, matched_table):
    """True when `answer` is basically just pointing at a table -- a number,
    its name, an alias, a plural/singular variant, or that plus filler words
    -- rather than restating or extending the original question.

    Matters because `resolve_clarification_answer` matches on ANY reply that
    names a real table, including a full sentence that happens to name one
    ("show me EM_EVENT events from GB, closed only"). Answering the ORIGINAL
    (pre-clarification) question in that case would silently drop the filter
    the user just added. So: a bare pick re-asks the original question against
    the picked table; anything else is treated as the real question in its
    own right, with that table already resolved.

    A heuristic, like looks_like_followup above -- a false negative here just
    means the reply is treated as a fresh question (correct if it truly is
    one; at worst a slightly odd standalone query if it wasn't). A false
    positive would silently drop a filter the user just typed, so this errs
    toward FALSE (treat it as a real question) whenever a word in the reply
    isn't accounted for by the table's own name.
    """
    answer = (answer or "").strip()
    if not answer:
        return True
    if re.match(r"^\s*#?\d+\s*\.?\s*$", answer):
        return True

    table_tokens = _tokens(matched_table or "")
    words = [w for w in re.split(r"[^a-zA-Z0-9]+", answer.lower()) if w]
    for w in words:
        if w in _STOPWORDS or w in _CLARIFICATION_FILLER_WORDS or len(w) < 2:
            continue
        singular = w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
        if w in table_tokens or singular in table_tokens:
            continue
        return False  # a real content word the table's own name doesn't explain
    return True


def previous_tables(session):
    """Tables resolved by the most recent turn -- reused by a refinement so the
    stage-1 Bedrock call can be skipped entirely."""
    for t in reversed(session.get("turns", []) if session else []):
        if t.get("tables"):
            return list(t["tables"])
    return []


def previous_sql(session):
    """SQL from the most recent turn -- the basis for a refinement, and what
    gets re-run for a result-referencing follow-up."""
    for t in reversed(session.get("turns", []) if session else []):
        if t.get("sql"):
            return t["sql"]
    return ""

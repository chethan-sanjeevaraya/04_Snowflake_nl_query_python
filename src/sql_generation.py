"""
Natural-language -> SQL generation: table selection, query-type
classification, and the two Bedrock prompts (initial generation and
error-driven repair). Depends on glossary.py for business knowledge blocks
and config.py for the shared Bedrock client -- has no direct Snowflake
connection logic of its own beyond what's passed in.
"""

import json
import re
from collections import OrderedDict

from imcm_commons import imcm_logger as logger

from config import (
    MAX_ROWS,
    MAX_TABLES,
    MODEL_ID,
    TABLE_SELECTION_CACHE_SIZE,
    TABLE_SHORTLIST_K,
    bedrock,
)
from glossary import (
    TABLE_ALIAS_MAP,
    column_descriptions_block,
    default_detail_columns_block,
    few_shot_block,
    relationships_block,
)
from session_memory import shortlist_debug

# Stage 1 (table selection) result cache, keyed on (normalized question,
# catalog fingerprint) so a schema change naturally invalidates stale
# entries. Repeated/near-identical questions (e.g. a BI dashboard re-issuing
# the same question) skip the table-selection Bedrock call on a warm hit.
_TABLE_SELECTION_CACHE = OrderedDict()


def resolve_tables(names, catalog):
    """Fuzzy-match a list of names against real table names (exact ->
    case-insensitive -> alias map -> substring). Returns only real matches."""
    resolved = []
    lower = {t.lower(): t for t in catalog}
    for name in names or []:
        if not name:
            continue
        if name in catalog and name not in resolved:
            resolved.append(name)
        elif name.lower() in lower and lower[name.lower()] not in resolved:
            resolved.append(lower[name.lower()])
        elif name.lower() in TABLE_ALIAS_MAP and TABLE_ALIAS_MAP[name.lower()] in catalog:
            aliased = TABLE_ALIAS_MAP[name.lower()]
            if aliased not in resolved:
                resolved.append(aliased)
        else:
            for t in catalog:
                if (name.lower() in t.lower() or t.lower() in name.lower()) and t not in resolved:
                    resolved.append(t)
                    break
    return resolved


def compact_catalog_text(catalog):
    """Table + column NAMES only (no types) -- kept small even at 100+ tables,
    used purely to let the model pick which table(s) are relevant."""
    return "\n".join(
        f"{t}: {', '.join(c for c, _ in cols)}" for t, cols in catalog.items()
    )


def detailed_schema_text(catalog, table_names):
    """Full 'TABLE(col TYPE, ...)' description, but ONLY for the given tables."""
    return "\n".join(
        f"{t}({', '.join(f'{c} {ty}' for c, ty in catalog[t])})"
        for t in table_names
        if t in catalog
    )


def select_relevant_tables(query, catalog):
    """Stage 1: ask the model which table(s) are needed for this question.
    Cached per (normalized question, catalog fingerprint) -- see
    _TABLE_SELECTION_CACHE above.

    Stage 0 runs first: a lexical shortlist bounds this prompt to a constant
    size. Without it, compact_catalog_text emits EVERY table with EVERY column
    name -- measured at ~200,000 characters (~50,000 tokens) for a 300-table
    Vault schema, versus ~14,000 after shortlisting. That matters now that
    callers no longer pass `Table` and this call runs on every new question.
    """
    cache_key = (query.strip().lower(), hash(compact_catalog_text(catalog)))
    cached = _TABLE_SELECTION_CACHE.get(cache_key)
    if cached is not None:
        _TABLE_SELECTION_CACHE.move_to_end(cache_key)
        return list(cached)

    if len(catalog) > TABLE_SHORTLIST_K:
        ranked = shortlist_debug(query, catalog, k=TABLE_SHORTLIST_K)
        logger.log_info(
            f"Table shortlist: {len(catalog)} table(s) -> {len(ranked)} candidate(s); "
            f"top scores: {ranked[:8]}"
        )
        if ranked and ranked[0][0] == 0:
            logger.log_info(
                "Table shortlist WARNING: top candidate scored 0 -- the question shares "
                "no vocabulary with any table or column name, so the correct table may "
                "be outside the shortlist. Consider a glossary alias."
            )
        catalog = {t: catalog[t] for _, t in ranked}

    prompt = (
        "You are choosing which database table(s) are needed to answer a question.\n"
        f"There are {len(catalog)} available tables. For each, the table name and its "
        "column names are listed (no data types, just to help you pick):\n\n"
        f"{compact_catalog_text(catalog)}\n\n"
        "Return ONLY a JSON object: { \"tables\": [\"<exact table name>\", ...] }\n"
        f"- List at most {MAX_TABLES} tables -- only the ones actually needed "
        "(include more than one only if the question clearly needs a join).\n"
        "- Use EXACT table names from the list above.\n\n"
        f"Question: {query}"
    )
    resp = bedrock.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 300, "temperature": 0},
    )
    raw = resp["output"]["message"]["content"][0]["text"].strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):raw.rfind("}") + 1]
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = {}
    resolved = resolve_tables(parsed.get("tables"), catalog)[:MAX_TABLES]

    _TABLE_SELECTION_CACHE[cache_key] = resolved
    _TABLE_SELECTION_CACHE.move_to_end(cache_key)
    if len(_TABLE_SELECTION_CACHE) > TABLE_SELECTION_CACHE_SIZE:
        _TABLE_SELECTION_CACHE.popitem(last=False)
    return list(resolved)


# --------------------------------------------------------------------------
# F. Cheap query classification. A heuristic (not an extra LLM call, to keep
# cost/latency down) that routes prompt guidance: aggregates don't need a
# LIMIT and shouldn't get default-detail-column suggestions; joins should
# lean hard on the relationships block; lookups benefit most from the
# default-detail-columns block. Table count already tells us "join" for
# free; only "aggregate vs lookup" needs a text heuristic.
# --------------------------------------------------------------------------
_AGGREGATE_HINTS = (
    "count", "how many", "average", "avg", "sum", "total", "rank", "ranked",
    "per country", "per rep", "per account", "group by", "distribution",
    "breakdown", "aggregate",
)


def classify_query_type(query, table_names):
    if len(table_names) > 1:
        return "join"
    low = query.lower()
    if any(h in low for h in _AGGREGATE_HINTS):
        return "aggregate"
    return "lookup"


def _extract_sql(resp):
    sql = resp["output"]["message"]["content"][0]["text"].strip()
    if sql.startswith("```"):
        sql = re.sub(r"^```[a-zA-Z]*", "", sql).strip().strip("`").strip()
    return sql.rstrip(";").strip()


def generate_sql(query, schema_text, database, schema, value_samples_text="", table_names=None,
                 conn=None, history_text="", requery_text=""):
    table_names = table_names or []
    samples_section = (
        f"\nActual data values observed (IMPORTANT -- match these exactly, "
        f"including case/underscores/suffixes):\n{value_samples_text}\n"
        if value_samples_text else ""
    )
    default_detail_section = default_detail_columns_block(table_names)
    column_notes_section = column_descriptions_block(table_names)
    relationships_section = relationships_block(conn, database, schema, table_names) if conn else ""
    few_shot_section = few_shot_block()
    query_type = classify_query_type(query, table_names)

    # Conversation context goes AFTER the schema/glossary/samples so the stable,
    # cacheable part of the prompt stays at the front -- prompt caching is a
    # prefix match, so a byte change early invalidates everything after it.
    history_section = f"\n{history_text}\n" if history_text else ""
    requery_section = f"\n{requery_text}\n" if requery_text else ""
    followup_rules = (
        "- This question continues the conversation above. If it REFINES the "
        "previous question, start from the previous SQL and change ONLY what the "
        "user asked to change -- keep every other filter exactly as it was. "
        "Silently dropping a filter the user still expects produces a query that "
        "runs fine and returns the wrong rows, which is the worst outcome here.\n"
        "- If the question instead starts a new topic, ignore the previous SQL.\n"
        if history_text else ""
    )

    type_guidance = {
        "aggregate": (
            "This question is an AGGREGATE query (count/sum/average/group-by-style). "
            "Do not add a LIMIT unless the question also asks to see individual rows."
        ),
        "join": (
            "This question needs a JOIN across the tables above. Use ONLY the join "
            "keys from the 'Known relationships' block below -- do not invent a join "
            "key that isn't listed there."
        ),
        "lookup": (
            "This question is a LOOKUP query. If a 'Default detail-view columns' "
            "block is provided below, prefer that column set."
        ),
    }[query_type]

    prompt = (
        "You are a Snowflake SQL expert. Write ONE single read-only SELECT query "
        "that answers the user's question, using only the tables/columns below.\n\n"
        f"Database: {database}\nSchema: {schema}\n\n"
        f"Tables:\n{schema_text}\n"
        f"{samples_section}"
        f"{column_notes_section}"
        f"{relationships_section}"
        f"{default_detail_section}"
        f"{few_shot_section}\n"
        f"Query type guidance: {type_guidance}\n\n"
        "Rules:\n"
        "- Output ONLY the SQL statement, no markdown fences, no explanation.\n"
        "- SELECT statements only. Never write INSERT/UPDATE/DELETE/DDL.\n"
        "- Do NOT use SELECT * -- list explicit column names instead. Vault/Veeva "
        "tables often have 100+ columns (many internal system fields like SYS_*, "
        "BPH_*, VEMCO_*, *_VPRO__C attachments), and a wide '*' or 'nearly every "
        "column' result is unreadable. Select AT MOST 10-12 columns: (a) a stable "
        "identifier column if one exists (e.g. ID, NAME__V, EVENT_IDENTIFIER__V), "
        "(b) the columns used in the WHERE/filter conditions, and (c) only the "
        "handful of other columns the question is directly asking about. Do not "
        "add extra columns 'just in case' -- when unsure whether a column belongs, "
        "leave it out. Only select every column if the user explicitly asks for "
        "'all columns'/'all fields'/'everything'.\n"
        "- When a column's actual values are shown above, use those exact values "
        "verbatim -- never substitute a display-style guess (e.g. 'Active' when "
        "the observed value is 'active__v').\n"
        "- You may JOIN across the tables given, use GROUP BY/ORDER BY/aggregates, "
        "and comparison operators (=, <, >, BETWEEN, IN, ILIKE for text).\n"
        f"- ALWAYS fully qualify EVERY table reference as {database}.{schema}.TABLE_NAME "
        "-- in single-table queries AND in every table of a JOIN, not just the first one. "
        "Some table names are also SQL reserved words (e.g. USER) -- when referencing "
        "such a table, ALWAYS double-quote just the table name part, e.g. "
        f"FROM {database}.{schema}.\"USER\" U, never FROM USER U or FROM {database}.{schema}.USER U.\n"
        f"- Always include a LIMIT of at most {MAX_ROWS} rows unless it is an aggregate.\n"
        "- Match the term the user actually used and do NOT broaden it to adjacent "
        "values. If they say 'closed', filter on the single value that means closed "
        "-- do not also include 'canceled__v' or 'completed__v'. Use IN(...) with "
        "several values only when the question itself names several.\n"
        "- Only filter on a column whose real values are shown above, or whose "
        "meaning is unambiguous from its name. Never invent a filter on an opaque "
        "surrogate-key column (values like 'V0V000000002060') to stand in for a "
        "concept such as a country, region or currency. If you cannot tell which "
        "column holds a value the question mentions, omit that filter rather than "
        "guessing a column.\n"
        f"{followup_rules}"
        f"{history_section}"
        f"{requery_section}\n"
        f"Question: {query}"
    )
    logger.log_info(
        f"SQL generation prompt: {len(prompt)} chars (~{len(prompt)//4} tokens) "
        f"[schema={len(schema_text)} samples={len(value_samples_text)} "
        f"history={len(history_text)} requery={len(requery_text)}]"
    )
    resp = bedrock.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 700, "temperature": 0},
    )
    usage = resp.get("usage") or {}
    metrics = resp.get("metrics") or {}
    logger.log_info(
        f"SQL generation Bedrock usage: input={usage.get('inputTokens')} "
        f"output={usage.get('outputTokens')} server_latency_ms={metrics.get('latencyMs')} "
        f"stop_reason={resp.get('stopReason')}"
    )
    return _extract_sql(resp)


# --------------------------------------------------------------------------
# D. Self-correction loop. If the generated SQL fails to execute, feed the
# actual Snowflake error message back to the model (with the same schema/
# glossary/relationship context) and ask for a corrected query, bounded by
# SQL_REPAIR_MAX_ATTEMPTS total attempts in the handler's retry loop. This
# recovers a meaningful fraction of syntax/casing/type-mismatch failures
# without a person in the loop -- typically the single highest-ROI accuracy
# enhancement for a text-to-SQL system.
# --------------------------------------------------------------------------

def repair_sql(query, schema_text, database, schema, value_samples_text, table_names, conn, failed_sql, error_message):
    relationships_section = relationships_block(conn, database, schema, table_names) if conn else ""
    column_notes_section = column_descriptions_block(table_names)
    samples_section = (
        f"\nActual data values observed:\n{value_samples_text}\n" if value_samples_text else ""
    )
    prompt = (
        "You are a Snowflake SQL expert. Your previous SQL failed to execute. "
        "Fix it based on the exact error message below. Output ONLY the corrected "
        "SQL statement, no markdown fences, no explanation.\n\n"
        f"Database: {database}\nSchema: {schema}\n\n"
        f"Tables:\n{schema_text}\n"
        f"{samples_section}"
        f"{column_notes_section}"
        f"{relationships_section}\n"
        f"Original question: {query}\n\n"
        f"Failed SQL:\n{failed_sql}\n\n"
        f"Snowflake error message:\n{error_message}\n\n"
        "Common causes to check: unquoted reserved-word table names (e.g. USER, "
        "ACCOUNT), missing database.schema qualification, case-sensitive column "
        "identifiers not matching the catalog exactly, wrong picklist value "
        "encoding, or an invalid/missing join key.\n"
        "Rules: SELECT/WITH only, single statement, no DDL/DML, explicit column "
        f"list (no SELECT *), LIMIT {MAX_ROWS} unless it's an aggregate."
    )
    resp = bedrock.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 700, "temperature": 0},
    )
    return _extract_sql(resp)

"""
Hand-curated business/schema knowledge injected into the SQL-generation
prompt. This module is deliberately separated from engineering logic --
it's the one file a teammate who isn't deep in the Lambda internals should
be able to open and extend (new join path, new ambiguous column, new
synonym) without touching connection/safety code.
"""

from imcm_commons import imcm_logger as logger  # noqa: F401 (kept for parity/future use)

import snowflake_client

# --------------------------------------------------------------------------
# E. Table/column synonym matching. A precomputed alias map (business terms
# that don't lexically overlap with the real name) checked before the
# lexical substring fallback. A true embedding-based match (e.g. Bedrock
# Titan Embeddings, comparing the term against cached table/column-name
# vectors) would generalize further, but adds a Bedrock call + latency per
# request for a schema this size where the term list is small and stable --
# the static map covers the known cases at zero extra cost/latency; revisit
# with embeddings if the table count grows large enough that synonyms
# outpace what's hand-maintainable.
# --------------------------------------------------------------------------
TABLE_ALIAS_MAP = {
    "events": "EM_EVENT",
    "event": "EM_EVENT",
    "studies": "EM_EVENT",
    "meetings": "EM_EVENT",
    "accounts": "ACCOUNT",
    "hcps": "ACCOUNT",
    "hcos": "ACCOUNT",
    "customers": "ACCOUNT",
    "reps": "USER",
    "sales reps": "USER",
    "salesreps": "USER",
    "users": "USER",
    "addresses": "ADDRESS",
    "venues": "ADDRESS",
}

# --------------------------------------------------------------------------
# B. Join-hint injection: most Vault/Veeva-provisioned Snowflake schemas
# don't declare real FK constraints (they're ELT'd from a source system, not
# enforced relationally), so INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS is
# checked best-effort (see snowflake_client.get_declared_relationships) but
# this hand-curated fallback carries the real weight. This directly targets
# the single biggest multi-table accuracy risk: the model guessing join keys
# with no relationship context at all.
# --------------------------------------------------------------------------
JOIN_HINTS = {
    # table: [(local_column, target_table, target_column, note), ...]
    "EM_EVENT": [
        ("ACCOUNT__V", "ACCOUNT", "ID", "the account/HCO-HCP the event was held for"),
        ("ADDRESS__V", "ADDRESS", "ID", "the venue address of the event"),
        ("OWNERID__V", "USER", "ID", "the event owner (sales rep)"),
        ("ASSIGNED_HOST__V", "USER", "ID", "the assigned host, distinct from OWNERID__V"),
    ],
    "ACCOUNT": [
        ("OWNERID__V", "USER", "ID", "the account owner (sales rep)"),
        ("CREATED_BY__V", "USER", "ID", "who created the account record, distinct from OWNERID__V"),
    ],
    "ADDRESS": [
        ("ACCOUNT__V", "ACCOUNT", "ID", "the account this address belongs to"),
    ],
}

# --------------------------------------------------------------------------
# C. Column-level business glossary. Opaque column names (BPH_*, or columns
# where the name alone doesn't disambiguate meaning, like EM_EVENT's three
# status-like columns) get a short human description here. This reduces
# reliance on value sampling alone for WHERE-clause accuracy, and is exactly
# the kind of tribal knowledge that caused the STATE__V vs EM_EVENT_STATUS__V
# mix-up seen in production. Extend as you discover more ambiguous columns.
# --------------------------------------------------------------------------
COLUMN_DESCRIPTIONS = {
    ("EM_EVENT", "STATUS__V"): "the record's active/inactive flag -- NOT the event lifecycle stage.",
    ("EM_EVENT", "EM_EVENT_STATUS__V"): "the event's lifecycle stage (e.g. closed/draft/approved) -- use this for 'closed', 'completed', 'in progress', etc.",
    ("EM_EVENT", "STATE__V"): "a separate field from both STATUS__V and EM_EVENT_STATUS__V -- do not conflate.",
    ("EM_EVENT", "COUNTRY__V"): "may not store a plain country code on this object -- verify with sampled values before assuming it's a display-ready country string; SYS_TENANT is often the more reliable country/tenant field.",
    ("EM_EVENT", "SYS_DATA_SOURCE"): "the ingestion/infrastructure source, e.g. 'VAULT_CRM_EC2' -- use this for questions about 'EC2 cluster' or 'data source'.",
    ("ACCOUNT", "CUSTOMER_MASTER_STATUS__V"): "MDM/master-data match status (e.g. matched/unmatched), distinct from STATUS__V (active/inactive).",
}

# Optional, hand-curated "default detail view" per table: the column set to
# use when a question is a generic lookup ("give me details on X", "show me
# event Y") rather than a specifically filtered/aggregated question. Keeps
# repeated generic lookups on the same table consistent instead of leaving
# the model to invent its own notion of "details" every time. Populate this
# per-table as needed (e.g. DEFAULT_DETAIL_COLUMNS["EM_EVENT"] = [...]).
# Empty/omitted tables fall back to the model's own judgment (default).
DEFAULT_DETAIL_COLUMNS = {}

# --------------------------------------------------------------------------
# A. Few-shot examples: curated (question, SQL) pairs anchoring the model to
# this org's naming conventions (__V picklists, identifier columns,
# fully-qualified/quoted table refs). Kept small and schema-agnostic in
# phrasing so they generalize rather than overfitting to one table.
# --------------------------------------------------------------------------
FEW_SHOT_EXAMPLES = [
    {
        "question": "Show all closed, in-person events for the EC2 data source in GB.",
        "sql": (
            'SELECT "ID", "NAME__V", "START_DATE__V", "EM_EVENT_STATUS__V" '
            'FROM PHC__SP__VAULT_CRM__DEV.PROVISION."EM_EVENT" '
            "WHERE SYS_DATA_SOURCE = 'VAULT_CRM_EC2' AND EVENT_FORMAT__V = 'in_person__v' "
            "AND EM_EVENT_STATUS__V = 'closed__v' AND SYS_TENANT = 'GB' LIMIT 200"
        ),
    },
    {
        "question": "List all active sales reps and their profile and role.",
        "sql": (
            'SELECT "NAME__V", "PROFILE_NAME__V", "USER_TYPE__V" '
            'FROM PHC__SP__VAULT_CRM__DEV.PROVISION."USER" '
            "WHERE ISACTIVE__V = TRUE LIMIT 200"
        ),
    },
    {
        "question": "Count of accounts owned per sales rep.",
        "sql": (
            'SELECT U."NAME__V" AS rep_name, COUNT(A."ID") AS account_count '
            'FROM PHC__SP__VAULT_CRM__DEV.PROVISION."ACCOUNT" A '
            'JOIN PHC__SP__VAULT_CRM__DEV.PROVISION."USER" U ON A.OWNERID__V = U.ID '
            "GROUP BY U.NAME__V ORDER BY account_count DESC"
        ),
    },
]


def relationships_block(conn, database, schema, table_names):
    """Builds the 'Known relationships' prompt block from declared FKs (if
    any) plus JOIN_HINTS, restricted to relationships touching at least one
    of the selected tables so the block stays small."""
    selected_upper = {t.upper() for t in table_names}
    lines = []

    declared = snowflake_client.get_declared_relationships(conn, database, schema, table_names)
    for child_table, child_col, parent_table, parent_col, note in declared:
        if child_table.upper() in selected_upper or parent_table.upper() in selected_upper:
            lines.append(f"{child_table}.{child_col} = {parent_table}.{parent_col} ({note})")

    for t in table_names:
        for local_col, target_table, target_col, note in JOIN_HINTS.get(t, []):
            line = f"{t}.{local_col} = {target_table}.{target_col} ({note})"
            if line not in lines:
                lines.append(line)

    if not lines:
        return ""
    return "\nKnown relationships (use these join keys, don't guess others):\n" + "\n".join(lines) + "\n"


def default_detail_columns_block(table_names):
    """For tables with a curated DEFAULT_DETAIL_COLUMNS entry, tell the model
    to use that fixed set for generic/unfiltered-beyond-identifier lookups,
    instead of inventing its own notion of 'details' each time."""
    parts = []
    for t in table_names:
        cols = DEFAULT_DETAIL_COLUMNS.get(t)
        if cols:
            parts.append(f"{t}: {', '.join(cols)}")
    if not parts:
        return ""
    return (
        "\nDefault detail-view columns (use these for generic lookup questions "
        "like 'give me details on X' / 'show me event Y' that don't name specific "
        "fields -- keeps repeated lookups consistent instead of guessing a "
        "different subset each time; still fine to add 1-2 more if the question "
        "clearly asks for something specific beyond this set):\n" + "\n".join(parts) + "\n"
    )


def column_descriptions_block(table_names):
    parts = []
    for t in table_names:
        for (tbl, col), desc in COLUMN_DESCRIPTIONS.items():
            if tbl == t:
                parts.append(f"{t}.{col}: {desc}")
    if not parts:
        return ""
    return "\nColumn notes (disambiguates columns that look similar or opaque):\n" + "\n".join(parts) + "\n"


def few_shot_block():
    parts = []
    for ex in FEW_SHOT_EXAMPLES:
        parts.append(f"Question: {ex['question']}\nSQL: {ex['sql']}")
    return "\nExamples of correctly-formed queries for this schema:\n" + "\n\n".join(parts) + "\n"

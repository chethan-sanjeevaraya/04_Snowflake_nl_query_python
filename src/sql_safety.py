"""
Deterministic SQL safety and post-processing. Nothing here calls Bedrock or
Snowflake -- pure functions over a SQL string, catalog, and table list. This
is the module that never trusts the model's output, on principle: every
function here exists because we caught the model doing something wrong
(SELECT *, unquoted reserved words, missing qualification, case-sensitive
identifier mismatches) and decided not to depend on it not doing that again.
"""

import re

from config import MAX_ROWS, MAX_STAR_COLUMNS, _FORBIDDEN


def is_safe_select(sql):
    low = sql.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return False, "Only SELECT/WITH queries are allowed."
    if ";" in sql:
        return False, "Multiple statements are not allowed."
    tokens = set(re.findall(r"[a-z_]+", low))
    bad = tokens & _FORBIDDEN
    if bad:
        return False, f"Disallowed keyword(s): {', '.join(sorted(bad))}."
    return True, ""


def qualify_and_quote_table_refs(sql, database, schema, table_names):
    """Always rewrite every FROM/JOIN reference to one of the SELECTED tables
    into a fully-qualified, quoted form: {database}.{schema}."TABLE" (exact
    catalog case). This fixes two related classes of failure at once, rather
    than depending on the model to get either one right:

    1. Reserved-word table names (e.g. this schema's ACCOUNT, USER objects)
       that fail with "Object 'X' does not exist" when referenced unquoted.
    2. Missing database.schema qualification -- the model reliably qualifies
       single-table queries but sometimes drops qualification in JOINs,
       which fails the same way if the Snowflake session's default
       database/schema isn't this request's target.

    Idempotent: matches the table whether the model wrote it bare, quoted,
    or already fully/partially qualified, and replaces the whole reference
    with the canonical form -- so it's always correct regardless of what the
    model produced, and running it twice is a no-op."""
    for t in table_names:
        # Optional up to two "schema-ish" segments (quoted or not) before the
        # table name, e.g. DB.SCHEMA.TABLE, SCHEMA.TABLE, "DB"."SCHEMA".TABLE, or bare TABLE.
        prefix = r'(?:"?\w+"?\.){0,2}'
        # Alternation (not an ambiguous optional trailing quote) so a table the
        # model already quoted, e.g. "USER", matches its closing quote correctly
        # instead of leaving a stray quote behind in the rewritten SQL.
        name_alt = rf'(?:"{re.escape(t)}"|{re.escape(t)}\b)'
        pattern = re.compile(rf'\b(FROM|JOIN)\s+{prefix}{name_alt}', re.IGNORECASE)
        qualified = f'{database}.{schema}."{t}"'
        sql = pattern.sub(lambda m, q=qualified: f'{m.group(1)} {q}', sql)
    return sql


def enforce_limit(sql):
    if re.search(r"\blimit\b", sql, re.IGNORECASE):
        return sql
    return f"{sql}\nLIMIT {MAX_ROWS}"


# Column-name substrings that mark a field as internal/system/attachment-style
# rather than something a human reading results cares about by default.
_LOW_PRIORITY_COLUMN_SUBSTRINGS = (
    "SYS_", "__SYS", "BPH_", "VEMCO_", "VPRO__C", "_VPRO", "MOBILE_", "STUB_",
    "URL", "PATH", "UPLOADED", "FIELDS__V",
)
# Column-name substrings likely to be a useful identifier/label, always kept first.
_IDENTIFIER_COLUMN_HINTS = ("ID", "NAME__V", "IDENTIFIER", "EXTERNAL_ID")


def normalize_select_list(sql, catalog, table_names):
    """Safety net covering two related problems in the generated SELECT list,
    for a plain single-table `SELECT <list> FROM <table> ...` query:

    1. READABILITY: the model may emit `SELECT *`, or -- as seen in practice --
       ignore the "keep it narrow" prompt guidance and enumerate nearly every
       column explicitly anyway. Either way, if the resolved column count
       exceeds MAX_STAR_COLUMNS, trim to: identifier-ish columns first, then
       columns referenced in the WHERE/ORDER/GROUP clause, then remaining
       non-internal columns, up to the cap.

    2. CORRECTNESS: Snowflake auto-uppercases unquoted identifiers before
       matching them against the object's real (possibly quoted / mixed-case)
       column names. If a column was created case-sensitively, an unquoted
       reference the model writes can raise "invalid identifier" even though
       the same column appears fine under `SELECT *`. To avoid depending on
       the model reproducing exact case/quoting, every column that survives
       is re-emitted quoted with its EXACT case as reported by
       INFORMATION_SCHEMA.COLUMNS (the catalog), regardless of what the
       model wrote.

    Deliberately conservative: no-ops (returns sql, None unchanged) for
    multi-table/JOIN queries, aggregate/expression select lists (anything
    with '(' or a nested SELECT), or any token that doesn't resolve to a
    real catalog column -- narrowing/requoting is a best-effort aid, not
    something that should risk breaking a query it can't confidently parse."""
    if len(table_names) != 1:
        return sql, None
    table = table_names[0]
    all_cols = catalog.get(table, [])
    if not all_cols:
        return sql, None
    exact_case = {c.upper(): c for c, _ in all_cols}
    all_col_names = [c for c, _ in all_cols]

    m = re.match(r"^\s*select\s+(.*?)\s+from\s+(.*)$", sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return sql, None
    select_list, rest = m.group(1).strip(), m.group(2)

    if select_list == "*":
        resolved = list(all_col_names)
    else:
        if "(" in select_list or re.search(r"\bselect\b", select_list, re.IGNORECASE):
            return sql, None  # aggregate/expression/subquery -- leave untouched
        raw_tokens = [t.strip().strip('"') for t in select_list.split(",")]
        if not raw_tokens or any(not t for t in raw_tokens):
            return sql, None
        resolved = []
        for t in raw_tokens:
            real = exact_case.get(t.upper())
            if real is None:
                return sql, None  # unknown token (alias/expression) -- bail safely
            if real not in resolved:
                resolved.append(real)

    if len(resolved) <= MAX_STAR_COLUMNS:
        # Small enough already -- but still re-quote with exact case to fix
        # any case-sensitive-identifier mismatch, unless it was already fine.
        if select_list == "*":
            return sql, None
        column_list = ", ".join(f'"{c}"' for c in resolved)
        return f"SELECT {column_list} FROM {rest}", None

    low_rest = rest.lower()

    def is_low_priority(col):
        cu = col.upper()
        return any(s in cu for s in _LOW_PRIORITY_COLUMN_SUBSTRINGS)

    def is_identifier(col):
        cu = col.upper()
        return any(cu == h or cu.endswith(h) for h in _IDENTIFIER_COLUMN_HINTS)

    referenced = [c for c in resolved if re.search(rf'\b{re.escape(c.lower())}\b', low_rest)]

    ordered, seen = [], set()
    for c in resolved:
        if is_identifier(c) and c not in seen:
            ordered.append(c)
            seen.add(c)
    for c in referenced:
        if c not in seen:
            ordered.append(c)
            seen.add(c)
    for c in resolved:
        if len(ordered) >= MAX_STAR_COLUMNS:
            break
        if c not in seen and not is_low_priority(c):
            ordered.append(c)
            seen.add(c)
    for c in resolved:  # last resort: fill remaining slots even with low-priority columns
        if len(ordered) >= MAX_STAR_COLUMNS:
            break
        if c not in seen:
            ordered.append(c)
            seen.add(c)

    chosen = ordered[:MAX_STAR_COLUMNS]
    if not chosen:
        return sql, None

    column_list = ", ".join(f'"{c}"' for c in chosen)
    narrowed_sql = f"SELECT {column_list} FROM {rest}"
    note = (
        f"Query selected {len(resolved)} column(s) from {table} -- "
        f"narrowed to {len(chosen)} for readability. "
        "Ask for 'all columns' explicitly to override."
    )
    return narrowed_sql, note

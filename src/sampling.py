"""
Value sampling: shows the model REAL distinct values for likely-categorical
columns, so it stops guessing display-style values ('Active') when the
actual stored encoding is something like 'active__v'.
"""

import json
import queue
import re
from concurrent.futures import ThreadPoolExecutor

from imcm_commons import imcm_logger as logger

from config import (
    CARDINALITY_SAMPLE_THRESHOLD,
    OPAQUE_ID_MIN_FRACTION,
    SAMPLABLE_TYPES,
    SKIP_NAME_SUBSTRINGS,
    VALUE_SAMPLE_LIMIT,
    VALUE_SAMPLE_TTL_SECONDS,
)
from snowflake_client import _STATE, cache_get, cache_set, get_sample_pool

# Vault/Veeva surrogate keys, e.g. 'V0V000000002060', 'V7RZ025E832IF4L'.
_OPAQUE_ID_RE = re.compile(r"^[A-Z0-9]{12,}$")


def is_samplable(col_name, data_type):
    """Type allow-list first (Snowflake normalizes VARCHAR/STRING/CHAR -> TEXT,
    INT/DECIMAL/NUMERIC -> NUMBER, etc., so we check against the *normalized*
    name), then name-based exclusions for free text / identifiers / urls.

    ORDER MATTERS. This previously returned True for any name ending in '__V'
    *before* consulting SKIP_NAME_SUBSTRINGS. In a Vault schema nearly every
    column ends in '__V', so the skip list was unreachable dead code -- which
    is why END_DATE__V, CREATED_DATE__V and EVENT_IDENTIFIER__V were all being
    sampled despite _DATE and _ID being listed. Observed effect: 175 candidate
    columns on EM_EVENT.

    The explicit '__V' branch is gone because it was redundant -- a '__V'
    column that survives the skip list falls through to the same `return True`.
    The only behavioural change is that '__V' columns now honour the skip list.
    """
    dt = data_type.upper()
    if dt not in SAMPLABLE_TYPES:
        return False
    name_upper = col_name.upper()
    if any(s in name_upper for s in SKIP_NAME_SUBSTRINGS):
        return False
    return True


def looks_like_opaque_ids(values):
    """True when most sampled values are Vault-style surrogate keys.

    Such a column is low-cardinality (so it passes the cardinality filter) but
    its values carry no meaning the model can reason about -- and offering them
    invites a spurious filter. Observed failure: for the question "...in GB",
    the model produced `LOCAL_CURRENCY__SYS = 'V0V000000002060'` instead of
    `SYS_TENANT = 'GB'`.

    Requires a digit so genuine uppercase tokens are not caught. Verified to
    drop 'V0V000000002060' / 'V7RZ025E832IF4L' while keeping 'GB', 'closed__v',
    'VAULT_CRM_EC2', 'EUR' and 'ACTIVE'.
    """
    strings = [v for v in values if isinstance(v, str)]
    if not strings:
        return False
    hits = sum(
        1 for v in strings
        if _OPAQUE_ID_RE.match(v) and any(ch.isdigit() for ch in v)
    )
    return (hits / len(strings)) >= OPAQUE_ID_MIN_FRACTION


def sample_table_values(conn, database, schema, table, catalog):
    """Cheap two-pass sampling: cheap approx-cardinality filter, then pull
    real distinct values only for columns that are both type-eligible and
    low-cardinality (i.e. actually categorical/picklist-like).

    Pass 2 gathers ALL low-cardinality columns' distinct values in a SINGLE
    query (one ARRAY_AGG(DISTINCT ...) expression per column, one table
    scan) rather than issuing one SELECT DISTINCT per column -- with wide
    Vault/Veeva tables (100-200+ candidate columns), N separate full-table
    round trips was the dominant cost (minutes per table); one combined
    query brings this down to ~2 round trips per table regardless of how
    many columns qualify."""
    key = (database.upper(), schema.upper(), table.upper())
    cached = cache_get(_STATE["value_samples"], key, VALUE_SAMPLE_TTL_SECONDS)
    if cached is not None:
        return cached

    columns = [(c, t) for c, t in catalog.get(table, []) if is_samplable(c, t)]
    if not columns:
        cache_set(_STATE["value_samples"], key, {})
        return {}

    cur = conn.cursor()
    samples = {}
    try:
        # Pass 1: cheap approx cardinality check for all candidate columns at once
        approx_exprs = ", ".join(f'APPROX_COUNT_DISTINCT("{c}") AS "{c}"' for c, _ in columns)
        cur.execute(f'SELECT {approx_exprs} FROM {database}.{schema}."{table}"')
        row = cur.fetchone()
        cardinalities = dict(zip([c for c, _ in columns], row)) if row else {}

        # `0 <` skips all-NULL columns: they pass the <= threshold test but
        # contribute nothing except a wider ARRAY_AGG query.
        low_card_cols = [
            c for c, _ in columns
            if 0 < (cardinalities.get(c) or 0) <= CARDINALITY_SAMPLE_THRESHOLD
        ]

        # Pass 2: one query, one table scan, one ARRAY_AGG(DISTINCT ...) per column
        if low_card_cols:
            agg_exprs = ", ".join(
                f'ARRAY_AGG(DISTINCT "{c}") WITHIN GROUP (ORDER BY "{c}") AS "{c}"'
                for c in low_card_cols
            )
            cur.execute(f'SELECT {agg_exprs} FROM {database}.{schema}."{table}"')
            agg_row = cur.fetchone()
            if agg_row:
                for col, raw_val in zip(low_card_cols, agg_row):
                    if raw_val is None:
                        samples[col] = []
                        continue
                    if isinstance(raw_val, str):
                        try:
                            parsed = json.loads(raw_val)
                        except (ValueError, TypeError):
                            parsed = [raw_val]
                    elif isinstance(raw_val, (list, tuple)):
                        parsed = list(raw_val)
                    else:
                        parsed = [raw_val]
                    vals = [v for v in parsed if v is not None][:VALUE_SAMPLE_LIMIT]
                    if vals and looks_like_opaque_ids(vals):
                        logger.log_info(
                            f'Dropping "{col}" from value samples -- values look like '
                            f"opaque surrogate keys (e.g. {vals[0]!r})"
                        )
                        continue
                    samples[col] = vals
    except Exception as exc:  # noqa: BLE001
        # Sampling is best-effort -- never fail the whole request over it.
        logger.log_info(f"Value sampling failed for {database}.{schema}.{table}: {exc}")
        samples = {}
    finally:
        cur.close()

    cache_set(_STATE["value_samples"], key, samples)
    logger.log_info(f"Sampled values for {database}.{schema}.{table}: {len(samples)} column(s) checked")
    return samples


def _sample_tables_parallel(database, schema, table_names, catalog):
    """Sample every selected table concurrently, one Snowflake connection
    per in-flight table (from get_sample_pool). Connections are checked out
    of a Queue rather than assigned by index -- with max_workers == pool
    size, that guarantees each connection is only ever used by one thread at
    a time regardless of how the thread pool schedules tasks."""
    pool = get_sample_pool(len(table_names))
    available = queue.Queue()
    for c in pool:
        available.put(c)

    def worker(t):
        conn = available.get()
        try:
            return t, sample_table_values(conn, database, schema, t, catalog)
        finally:
            available.put(conn)

    with ThreadPoolExecutor(max_workers=len(pool)) as executor:
        futures = [executor.submit(worker, t) for t in table_names]
        return [f.result() for f in futures]


def value_samples_block(conn, database, schema, catalog, table_names):
    """Human-readable block listing observed values per table/column, for
    injection into the SQL-generation prompt. Single-table requests (the
    common case) reuse the caller's connection directly; multi-table
    requests (joins) sample every table concurrently instead of serializing
    one DISTINCT-values pass per table."""
    if len(table_names) <= 1:
        results = [(t, sample_table_values(conn, database, schema, t, catalog)) for t in table_names]
    else:
        results = _sample_tables_parallel(database, schema, table_names, catalog)

    parts = []
    for t, samples in results:
        non_empty = {c: v for c, v in samples.items() if v}
        if not non_empty:
            continue
        lines = [f"Known values in {t} (use these exact values, not display-style guesses):"]
        for col, vals in non_empty.items():
            lines.append(f'  - "{col}": {vals}')
        parts.append("\n".join(lines))
    return "\n\n".join(parts)

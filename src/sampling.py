"""
Value sampling: shows the model REAL distinct values for likely-categorical
columns, so it stops guessing display-style values ('Active') when the
actual stored encoding is something like 'active__v'.
"""

import json

from imcm_commons import imcm_logger as logger

from config import (
    CARDINALITY_SAMPLE_THRESHOLD,
    SAMPLABLE_TYPES,
    SKIP_NAME_SUBSTRINGS,
    VALUE_SAMPLE_LIMIT,
    VALUE_SAMPLE_TTL_SECONDS,
)
from snowflake_client import _STATE, cache_get, cache_set


def is_samplable(col_name, data_type):
    """Type allow-list first (Snowflake normalizes VARCHAR/STRING/CHAR -> TEXT,
    INT/DECIMAL/NUMERIC -> NUMBER, etc., so we check against the *normalized*
    name). Within TEXT columns: always sample known picklist-style columns
    (the org's '__V' convention, e.g. Vault/Veeva picklists) regardless of
    name otherwise; skip other TEXT columns that look like free text/IDs/urls."""
    dt = data_type.upper()
    if dt not in SAMPLABLE_TYPES:
        return False
    name_upper = col_name.upper()
    if name_upper.endswith("__V"):
        return True
    if any(s in name_upper for s in SKIP_NAME_SUBSTRINGS):
        return False
    return True


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

        low_card_cols = [
            c for c, _ in columns
            if (cardinalities.get(c) or 0) <= CARDINALITY_SAMPLE_THRESHOLD
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
                    samples[col] = [v for v in parsed if v is not None][:VALUE_SAMPLE_LIMIT]
    except Exception as exc:  # noqa: BLE001
        # Sampling is best-effort -- never fail the whole request over it.
        logger.log_info(f"Value sampling failed for {database}.{schema}.{table}: {exc}")
        samples = {}
    finally:
        cur.close()

    cache_set(_STATE["value_samples"], key, samples)
    logger.log_info(f"Sampled values for {database}.{schema}.{table}: {len(samples)} column(s) checked")
    return samples


def value_samples_block(conn, database, schema, catalog, table_names):
    """Human-readable block listing observed values per table/column, for
    injection into the SQL-generation prompt."""
    parts = []
    for t in table_names:
        samples = sample_table_values(conn, database, schema, t, catalog)
        non_empty = {c: v for c, v in samples.items() if v}
        if not non_empty:
            continue
        lines = [f"Known values in {t} (use these exact values, not display-style guesses):"]
        for col, vals in non_empty.items():
            lines.append(f'  - "{col}": {vals}')
        parts.append("\n".join(lines))
    return "\n\n".join(parts)

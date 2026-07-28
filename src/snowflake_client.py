"""
Snowflake connection lifecycle, the warm-container cache, and schema
metadata introspection. Nothing here does prompt engineering or SQL
generation -- if you're touching the connection, the cache, or
INFORMATION_SCHEMA, it goes here.
"""

import time

from imcm_commons import imcm_logger as logger, snowflake_utils

from config import CATALOG_TTL_SECONDS, REGION_NAME, SNOWFLAKE_SECRET, SNOWFLAKE_SECRET_PK

# Reuse the connection across warm invocations; cache catalogs, value
# samples, and FK lookups per (database, schema, ...) combination queried.
_STATE = {
    "conn": None,
    "db_info": None,
    "catalogs": {},        # key -> {"data": catalog, "ts": epoch_seconds}
    "value_samples": {},   # key -> {"data": samples, "ts": epoch_seconds}
}


def get_conn():
    """Connect via snowflake_utils helper, reconnecting if the cached
    connection has gone stale/closed."""
    if _STATE["conn"] is not None:
        try:
            cur = _STATE["conn"].cursor()
            cur.execute("SELECT 1")
            cur.close()
            return _STATE["conn"], _STATE["db_info"]
        except Exception as exc:  # noqa: BLE001
            logger.log_info(f"Cached Snowflake connection appears dead, reconnecting: {exc}")
            _STATE["conn"] = None

    (
        conn,
        snow_database,
        enrich_schema,
        grey_database,
        grey_schema,
        aurora_db,
        schema_provision,
    ) = snowflake_utils.snowflake_connect(REGION_NAME, SNOWFLAKE_SECRET, SNOWFLAKE_SECRET_PK)

    db_info = {
        "snow_database": snow_database,
        "enrich_schema": enrich_schema,
        "grey_database": grey_database,
        "grey_schema": grey_schema,
        "aurora_db": aurora_db,
        "schema_provision": schema_provision,
    }
    logger.log_info(f"Connected to Snowflake. db_info={db_info}")

    _STATE["conn"] = conn
    _STATE["db_info"] = db_info
    return conn, db_info


def default_target(db_info):
    return db_info["snow_database"], db_info["enrich_schema"]


def valid_targets(db_info):
    """The only database/schema pairs a request is allowed to select --
    exactly the three pairs the org's own connection helper already trusts."""
    pairs = [
        (db_info["snow_database"], db_info["enrich_schema"]),
        (db_info["grey_database"], db_info["grey_schema"]),
        (db_info["aurora_db"], db_info["schema_provision"]),
    ]
    return {(d.upper(), s.upper()): (d, s) for d, s in pairs}


def resolve_target(database_hint, schema_hint, db_info):
    """Validate a caller-supplied DATABASE/SCHEMA pair against the known,
    trusted targets. Returns (database, schema, error_response_or_None)."""
    if not database_hint and not schema_hint:
        d, s = default_target(db_info)
        return d, s, None

    if bool(database_hint) != bool(schema_hint):
        return None, None, {"error": "Provide both DATABASE and SCHEMA together, or neither."}

    valid = valid_targets(db_info)
    key = (database_hint.upper(), schema_hint.upper())
    if key not in valid:
        return None, None, {
            "error": f"Unknown DATABASE/SCHEMA combination '{database_hint}.{schema_hint}'.",
            "valid_targets": [f"{d}.{s}" for d, s in valid.values()],
        }
    d, s = valid[key]
    return d, s, None


def cache_get(store, key, ttl_seconds):
    entry = store.get(key)
    if entry is None:
        return None
    if time.time() - entry["ts"] > ttl_seconds:
        return None
    return entry["data"]


def cache_set(store, key, data):
    store[key] = {"data": data, "ts": time.time()}


def get_catalog(conn, database, schema, table_filter=None):
    """Fetch {table: [(col, type), ...]}, cached per (database, schema,
    table_filter) with a TTL so schema changes (new columns, etc.) are
    eventually picked up without a redeploy.

    When table_filter is given, the query is scoped to exactly those tables
    -- fast regardless of how many tables the schema has overall. Without a
    filter, the WHOLE schema is scanned, which can be slow/timeout-prone for
    a schema with thousands of tables -- always pass Table alongside
    DATABASE/SCHEMA for a non-default, large schema to avoid that."""
    key = (
        database.upper(),
        schema.upper(),
        tuple(sorted(t.upper() for t in table_filter)) if table_filter else None,
    )
    cached = cache_get(_STATE["catalogs"], key, CATALOG_TTL_SECONDS)
    if cached is not None:
        return cached

    cur = conn.cursor()
    try:
        if table_filter:
            placeholders = ", ".join(["%s"] * len(table_filter))
            cur.execute(
                f"""
                SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
                FROM {database}.INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s
                  AND UPPER(TABLE_NAME) IN ({placeholders})
                ORDER BY TABLE_NAME, ORDINAL_POSITION
                """,
                (schema, *[t.upper() for t in table_filter]),
            )
        else:
            cur.execute(
                f"""
                SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
                FROM {database}.INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME, ORDINAL_POSITION
                """,
                (schema,),
            )
        catalog = {}
        for table_name, column_name, data_type in cur.fetchall():
            catalog.setdefault(table_name, []).append((column_name, data_type))
    finally:
        cur.close()

    cache_set(_STATE["catalogs"], key, catalog)
    logger.log_info(f"Loaded catalog for {database}.{schema} (filter={table_filter}): {len(catalog)} table(s)")
    return catalog


def get_declared_relationships(conn, database, schema, table_filter=None):
    """Best-effort lookup of real FK constraints from Snowflake's own
    metadata. Returns [] for schemas with no declared constraints (common
    for ELT'd/provisioned data) rather than failing -- the hand-curated
    JOIN_HINTS in glossary.py covers that gap. Cached like the catalog."""
    key = (
        "fk", database.upper(), schema.upper(),
        tuple(sorted(t.upper() for t in table_filter)) if table_filter else None,
    )
    cached = cache_get(_STATE["catalogs"], key, CATALOG_TTL_SECONDS)
    if cached is not None:
        return cached

    relationships = []
    cur = conn.cursor()
    try:
        cur.execute(
            f"""
            SELECT
                kcu1.TABLE_NAME AS child_table, kcu1.COLUMN_NAME AS child_column,
                kcu2.TABLE_NAME AS parent_table, kcu2.COLUMN_NAME AS parent_column
            FROM {database}.INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
            JOIN {database}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu1
                ON rc.CONSTRAINT_NAME = kcu1.CONSTRAINT_NAME AND rc.CONSTRAINT_SCHEMA = kcu1.CONSTRAINT_SCHEMA
            JOIN {database}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu2
                ON rc.UNIQUE_CONSTRAINT_NAME = kcu2.CONSTRAINT_NAME AND rc.UNIQUE_CONSTRAINT_SCHEMA = kcu2.CONSTRAINT_SCHEMA
            WHERE rc.CONSTRAINT_SCHEMA = %s
            """,
            (schema,),
        )
        for child_table, child_col, parent_table, parent_col in cur.fetchall():
            relationships.append((child_table, child_col, parent_table, parent_col, "declared FK"))
    except Exception as exc:  # noqa: BLE001
        # Not every schema exposes/enforces this -- absence is normal, not an error.
        logger.log_info(f"No declared FK metadata available for {database}.{schema}: {exc}")
    finally:
        cur.close()

    cache_set(_STATE["catalogs"], key, relationships)
    return relationships


def get_total_count(conn, sql):
    """Run a COUNT(*) over the generated query (BEFORE any LIMIT is added)
    wrapped as a subquery, so the caller knows how many rows actually match
    even though only MAX_ROWS are returned. Works for plain filters and for
    GROUP BY/aggregate queries alike (counts whatever rows the inner query
    produces). Best-effort: on failure, returns None rather than failing
    the whole request -- the capped row set is still useful without a count."""
    count_sql = f"SELECT COUNT(*) FROM ({sql}) AS _count_wrapper"
    cur = conn.cursor()
    try:
        cur.execute(count_sql)
        row = cur.fetchone()
        return int(row[0]) if row else None
    except Exception as exc:  # noqa: BLE001
        logger.log_info(f"Total-count query failed, continuing without it: {exc}")
        return None
    finally:
        cur.close()

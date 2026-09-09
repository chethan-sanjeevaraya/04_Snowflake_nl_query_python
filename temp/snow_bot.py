"""
Natural-language -> Snowflake query Lambda (text-to-SQL)

"""

import json
import os
import re

import boto3
from botocore.config import Config
from imcm_commons import imcm_logger as logger, snowflake_utils

REGION_NAME = os.environ.get("region", os.environ.get("AWS_REGION", "eu-central-1"))
SNOWFLAKE_SECRET = os.environ["snowflake_secret"]
SNOWFLAKE_SECRET_PK = os.environ["snowflake_secret_pk"]
MODEL_ID = os.environ.get("model_arn") or os.environ["MODEL_ARN"]
MAX_ROWS = int(os.environ.get("MAX_ROWS", "200"))
MAX_TABLES = int(os.environ.get("MAX_TABLES", "5"))
ALLOWED_TABLES = [t.strip().upper() for t in os.environ.get("ALLOWED_TABLES", "").split(",") if t.strip()]

_cfg = Config(read_timeout=30, connect_timeout=10, retries={"max_attempts": 2})
_bedrock = boto3.client("bedrock-runtime", region_name=REGION_NAME, config=_cfg)

# Reuse the connection across warm invocations; cache one catalog per
# (database, schema, table-filter) combination actually queried.
_STATE = {"conn": None, "db_info": None, "catalogs": {}}

_FORBIDDEN = {
    "insert", "update", "delete", "drop", "alter", "create", "merge",
    "truncate", "grant", "revoke", "call", "copy", "put", "remove", "use",
}


def _get_conn():
    """Connect via snowflake_utils helper"""
    if _STATE["conn"] is not None:
        return _STATE["conn"], _STATE["db_info"]

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


def _default_target(db_info):
    return db_info["snow_database"], db_info["enrich_schema"]


def _valid_targets(db_info):
    """The only database/schema pairs a request is allowed to select --
    exactly the three pairs the org's own connection helper already trusts."""
    pairs = [
        (db_info["snow_database"], db_info["enrich_schema"]),
        (db_info["grey_database"], db_info["grey_schema"]),
        (db_info["aurora_db"], db_info["schema_provision"]),
    ]
    return {(d.upper(), s.upper()): (d, s) for d, s in pairs}


def _resolve_target(database_hint, schema_hint, db_info):
    """Validate a caller-supplied DATABASE/SCHEMA pair against the known,
    trusted targets. Returns (database, schema, error_response_or_None)."""
    if not database_hint and not schema_hint:
        d, s = _default_target(db_info)
        return d, s, None

    if bool(database_hint) != bool(schema_hint):
        return None, None, {"error": "Provide both DATABASE and SCHEMA together, or neither."}

    valid = _valid_targets(db_info)
    key = (database_hint.upper(), schema_hint.upper())
    if key not in valid:
        return None, None, {
            "error": f"Unknown DATABASE/SCHEMA combination '{database_hint}.{schema_hint}'.",
            "valid_targets": [f"{d}.{s}" for d, s in valid.values()],
        }
    d, s = valid[key]
    return d, s, None


def _get_catalog(conn, database, schema, table_filter=None):
    """Fetch {table: [(col, type), ...]}, cached per (database, schema,
    table_filter) so repeated requests in the same warm container are free.

    When table_filter is given, the query is scoped to exactly those tables
    -- fast regardless of how many tables the schema has overall. Without a
    filter, the WHOLE schema is scanned, which can be slow/timeout-prone for
    a schema with thousands of tables (as seen with the default target
    before ALLOWED_TABLES was pushed into SQL) -- always pass Table alongside
    DATABASE/SCHEMA for a non-default, large schema to avoid that."""
    key = (
        database.upper(),
        schema.upper(),
        tuple(sorted(t.upper() for t in table_filter)) if table_filter else None,
    )
    if key in _STATE["catalogs"]:
        return _STATE["catalogs"][key]

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

    _STATE["catalogs"][key] = catalog
    logger.log_info(f"Loaded catalog for {database}.{schema} (filter={table_filter}): {len(catalog)} table(s)")
    return catalog


def _compact_catalog_text(catalog):
    """Table + column NAMES only (no types) -- kept small even at 100+ tables,
    used purely to let the model pick which table(s) are relevant."""
    return "\n".join(
        f"{t}: {', '.join(c for c, _ in cols)}" for t, cols in catalog.items()
    )


def _detailed_schema_text(catalog, table_names):
    """Full 'TABLE(col TYPE, ...)' description, but ONLY for the given tables."""
    return "\n".join(
        f"{t}({', '.join(f'{c} {ty}' for c, ty in catalog[t])})"
        for t in table_names
        if t in catalog
    )


def _resolve_tables(names, catalog):
    """Fuzzy-match a list of names against real table names (exact ->
    case-insensitive -> substring). Returns only real matches."""
    resolved = []
    lower = {t.lower(): t for t in catalog}
    for name in names or []:
        if not name:
            continue
        if name in catalog and name not in resolved:
            resolved.append(name)
        elif name.lower() in lower and lower[name.lower()] not in resolved:
            resolved.append(lower[name.lower()])
        else:
            for t in catalog:
                if (name.lower() in t.lower() or t.lower() in name.lower()) and t not in resolved:
                    resolved.append(t)
                    break
    return resolved


def _select_relevant_tables(query, catalog):
    """Stage 1: ask the model which table(s) are needed for this question."""
    prompt = (
        "You are choosing which database table(s) are needed to answer a question.\n"
        f"There are {len(catalog)} available tables. For each, the table name and its "
        "column names are listed (no data types, just to help you pick):\n\n"
        f"{_compact_catalog_text(catalog)}\n\n"
        "Return ONLY a JSON object: { \"tables\": [\"<exact table name>\", ...] }\n"
        f"- List at most {MAX_TABLES} tables -- only the ones actually needed "
        "(include more than one only if the question clearly needs a join).\n"
        "- Use EXACT table names from the list above.\n\n"
        f"Question: {query}"
    )
    resp = _bedrock.converse(
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
    return _resolve_tables(parsed.get("tables"), catalog)[:MAX_TABLES]


def _generate_sql(query, schema_text, database, schema):
    prompt = (
        "You are a Snowflake SQL expert. Write ONE single read-only SELECT query "
        "that answers the user's question, using only the tables/columns below.\n\n"
        f"Database: {database}\nSchema: {schema}\n\n"
        f"Tables:\n{schema_text}\n\n"
        "Rules:\n"
        "- Output ONLY the SQL statement, no markdown fences, no explanation.\n"
        "- SELECT statements only. Never write INSERT/UPDATE/DELETE/DDL.\n"
        "- You may JOIN across the tables given, use GROUP BY/ORDER BY/aggregates, "
        "and comparison operators (=, <, >, BETWEEN, IN, ILIKE for text).\n"
        "- Use fully qualified names when helpful; quote identifiers if they need it.\n"
        f"- Always include a LIMIT of at most {MAX_ROWS} rows unless it is an aggregate.\n\n"
        f"Question: {query}"
    )
    resp = _bedrock.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 700, "temperature": 0},
    )
    sql = resp["output"]["message"]["content"][0]["text"].strip()
    if sql.startswith("```"):
        sql = re.sub(r"^```[a-zA-Z]*", "", sql).strip().strip("`").strip()
    return sql.rstrip(";").strip()


def _is_safe_select(sql):
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


def _enforce_limit(sql):
    if re.search(r"\blimit\b", sql, re.IGNORECASE):
        return sql
    return f"{sql}\nLIMIT {MAX_ROWS}"


def _clean(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _is_api_gateway_event(event):
    return isinstance(event, dict) and (
        "requestContext" in event or "httpMethod" in event or "resource" in event
    )


def _respond(event, body_dict, status_code=200):
    if _is_api_gateway_event(event):
        return {
            "statusCode": status_code,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": json.dumps(body_dict, default=str),
        }
    return body_dict


def _get_param(event, *names):
    for n in names:
        if event.get(n):
            return event.get(n)
    qs = event.get("queryStringParameters") or {}
    for n in names:
        if qs.get(n):
            return qs.get(n)
    body = event.get("body")
    if body:
        try:
            data = json.loads(body)
            for n in names:
                if data.get(n):
                    return data.get(n)
        except (ValueError, TypeError):
            pass
    return None


def lambda_handler(event, context):
    logger.log_info(f"Incoming event: {json.dumps(event)[:2000]}")

    query = (_get_param(event, "query", "question") or "").strip()
    table_hint = _get_param(event, "Table", "table")
    database_hint = _get_param(event, "DATABASE", "Database", "database")
    schema_hint = _get_param(event, "SCHEMA", "Schema", "schema")
    if not query:
        return _respond(event, {"error": "Request must include a non-empty 'query' field."}, 400)

    try:
        conn, db_info = _get_conn()
    except Exception as exc:  # noqa: BLE001
        logger.log_info(f"Snowflake connection failed: {exc}")
        return _respond(event, {"error": "Could not connect to Snowflake.", "detail": str(exc)}, 500)

    database, schema, target_error = _resolve_target(database_hint, schema_hint, db_info)
    if target_error:
        return _respond(event, target_error, 400)
    is_default = (database.upper(), schema.upper()) == tuple(s.upper() for s in _default_target(db_info))

    requested_tables = [t.strip() for t in table_hint.split(",") if t.strip()] if table_hint else None

    # Whichever table filter narrows the metadata query the most: explicit
    # tables from the caller, else (only for the default target) the
    # server-side ALLOWED_TABLES allowlist, else no filter (full schema scan
    # -- fine for small schemas, but pass Table explicitly for a large one).
    if requested_tables:
        table_filter = requested_tables
    elif is_default and ALLOWED_TABLES:
        table_filter = ALLOWED_TABLES
    else:
        table_filter = None

    try:
        catalog = _get_catalog(conn, database, schema, table_filter)
    except Exception as exc:  # noqa: BLE001
        logger.log_info(f"Catalog load failed: {exc}")
        return _respond(event, {"error": "Could not load table metadata.", "detail": str(exc)}, 500)

    if not catalog:
        return _respond(
            event,
            {"error": f"No matching tables found in {database}.{schema} (check names / ALLOWED_TABLES)."},
            500,
        )

    if requested_tables:
        selected = _resolve_tables(requested_tables, catalog)
        missing = [t for t in requested_tables if t.upper() not in {k.upper() for k in catalog}]
        if not selected:
            return _respond(
                event,
                {
                    "error": f"None of the requested table(s) '{table_hint}' were found in {database}.{schema}.",
                    "available_tables": list(catalog),
                },
                404,
            )
        if missing:
            logger.log_info(f"Some requested tables were not found: {missing}")
    else:
        selected = _select_relevant_tables(query, catalog)
        if not selected:
            return _respond(
                event,
                {
                    "error": "Could not determine which table(s) your question refers to. "
                    "Pass a 'Table' field or mention the table/topic in your query.",
                    "table_count": len(catalog),
                },
                400,
            )

    logger.log_info(f"Target: {database}.{schema} | Selected tables: {selected}")
    schema_text = _detailed_schema_text(catalog, selected)

    sql = _generate_sql(query, schema_text, database, schema)
    sql = " ".join(sql.split())  # collapse to one line so logs/response don't show literal \n
    logger.log_info(f"Generated SQL: {sql}")

    ok, reason = _is_safe_select(sql)
    if not ok:
        return _respond(
            event,
            {"error": "Generated query was rejected by the safety check.", "reason": reason, "sql": sql},
            400,
        )

    sql = _enforce_limit(sql)

    try:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            columns = [c[0] for c in cur.description]
            rows = [dict(zip(columns, r)) for r in cur.fetchmany(MAX_ROWS)]
        finally:
            cur.close()
    except Exception as exc:  # noqa: BLE001
        logger.log_info(f"Query execution failed: {exc}")
        return _respond(event, {"error": "Query execution failed.", "detail": str(exc), "sql": sql}, 400)

    rows = [{k: _clean(v) for k, v in row.items()} for row in rows]

    result = {
        "answer": f"Returned {len(rows)} row(s).",
        "database": database,
        "schema": schema,
        "tables_used": selected,
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
    }
    logger.log_info(f"RESULT: {json.dumps(result, default=str)[:8000]}")
    return _respond(event, result, 200)

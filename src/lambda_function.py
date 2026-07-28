"""
Natural-language -> Snowflake query Lambda (text-to-SQL)

Entry point only. All actual logic lives in the sibling modules:
  config.py            env vars, constants, shared Bedrock client
  snowflake_client.py    connection, cache, catalog/FK introspection
  glossary.py            hand-curated business knowledge (join hints,
                          column descriptions, aliases, few-shot examples)
  sampling.py             real-value sampling for picklist columns
  sql_generation.py       table selection, classification, LLM SQL generation/repair
  sql_safety.py           deterministic safety checks and SQL post-processing

This file's job is ONLY: parse the event, call the above in order, and
shape the response. If you're adding new SQL-accuracy logic, business
knowledge, or a safety check, it almost certainly belongs in one of the
other modules, not here.
"""

import json

from imcm_commons import imcm_logger as logger

import snowflake_client
from config import ALLOWED_TABLES, LOG_ROW_PREVIEW_LIMIT, MAX_ROWS, SQL_REPAIR_MAX_ATTEMPTS
from sampling import value_samples_block
from sql_generation import detailed_schema_text, generate_sql, repair_sql, resolve_tables, select_relevant_tables
from sql_safety import enforce_limit, is_safe_select, normalize_select_list, qualify_and_quote_table_refs


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
        conn, db_info = snowflake_client.get_conn()
    except Exception as exc:  # noqa: BLE001
        logger.log_info(f"Snowflake connection failed: {exc}")
        return _respond(event, {"error": "Could not connect to Snowflake.", "detail": str(exc)}, 500)

    database, schema, target_error = snowflake_client.resolve_target(database_hint, schema_hint, db_info)
    if target_error:
        return _respond(event, target_error, 400)
    is_default = (database.upper(), schema.upper()) == tuple(
        s.upper() for s in snowflake_client.default_target(db_info)
    )

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
        catalog = snowflake_client.get_catalog(conn, database, schema, table_filter)
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
        selected = resolve_tables(requested_tables, catalog)
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
        selected = select_relevant_tables(query, catalog)
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
    schema_text = detailed_schema_text(catalog, selected)

    value_samples_text = value_samples_block(conn, database, schema, catalog, selected)

    sql = None
    narrow_note = None
    last_error = None
    columns, rows, total_count = None, None, None

    for attempt in range(1, SQL_REPAIR_MAX_ATTEMPTS + 1):
        if attempt == 1:
            candidate_sql = generate_sql(query, schema_text, database, schema, value_samples_text, selected, conn)
        else:
            candidate_sql = repair_sql(
                query, schema_text, database, schema, value_samples_text, selected, conn, sql, last_error
            )
        candidate_sql = " ".join(candidate_sql.split())  # collapse to one line

        candidate_sql = qualify_and_quote_table_refs(candidate_sql, database, schema, selected)
        candidate_sql, narrow_note = normalize_select_list(candidate_sql, catalog, selected)

        ok, reason = is_safe_select(candidate_sql)
        if not ok:
            return _respond(
                event,
                {"error": "Generated query was rejected by the safety check.", "reason": reason, "sql": candidate_sql},
                400,
            )

        sql = candidate_sql
        logger.log_info(
            f"Generated SQL (attempt {attempt}/{SQL_REPAIR_MAX_ATTEMPTS}): {sql}"
            + (f" | {narrow_note}" if narrow_note else "")
        )

        # Count total matching rows BEFORE the LIMIT is applied, so the caller
        # knows if the returned set was truncated. Best-effort -- never blocks
        # the main query if it fails (e.g. on an exotic SQL shape).
        total_count = snowflake_client.get_total_count(conn, sql)
        exec_sql = enforce_limit(sql)

        try:
            cur = conn.cursor()
            try:
                cur.execute(exec_sql)
                columns = [c[0] for c in cur.description]
                rows = [dict(zip(columns, r)) for r in cur.fetchmany(MAX_ROWS)]
            finally:
                cur.close()
            sql = exec_sql
            last_error = None
            break  # success
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            logger.log_info(f"Query execution failed (attempt {attempt}/{SQL_REPAIR_MAX_ATTEMPTS}): {exc}")

    if last_error is not None:
        return _respond(
            event,
            {
                "error": "Query execution failed after self-correction attempts.",
                "detail": last_error,
                "sql": sql,
                "repair_attempts": SQL_REPAIR_MAX_ATTEMPTS,
            },
            400,
        )

    rows = [{k: _clean(v) for k, v in row.items()} for row in rows]

    truncated = total_count is not None and total_count > len(rows)
    if total_count is not None:
        answer = (
            f"Returned {len(rows)} of {total_count} matching row(s)."
            if truncated else
            f"Returned {len(rows)} row(s)."
        )
    else:
        answer = f"Returned {len(rows)} row(s)."

    result = {
        "answer": answer,
        "database": database,
        "schema": schema,
        "tables_used": selected,
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "total_matching_rows": total_count,
        "truncated": truncated,
    }
    if narrow_note:
        result["note"] = narrow_note

    preview_rows = rows[:LOG_ROW_PREVIEW_LIMIT]
    logger.log_info(
        f"RESULT summary: {len(rows)} row(s) x {len(columns)} column(s), "
        f"total_matching_rows={total_count}, truncated={truncated}"
    )
    if preview_rows:
        preview_note = (
            "" if len(rows) <= LOG_ROW_PREVIEW_LIMIT
            else f" (showing first {LOG_ROW_PREVIEW_LIMIT} of {len(rows)})"
        )
        logger.log_info(f"RESULT rows{preview_note}: {json.dumps(preview_rows, default=str)}")

    return _respond(event, result, 200)

"""
CSV-direct query Lambda -- FULL SQL via embedded DuckDB.

Each CSV file under the S3 prefix is loaded as a real TABLE in an in-process
DuckDB database (name = file name without extension, e.g. 'equipment_data').
The model writes genuine SQL against that schema, so unlike the simple

"""

import json
import logging
import os
import re
import tempfile

import boto3
from botocore.config import Config

import duckdb

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = os.environ.get("S3_PREFIX")
S3_KEY = os.environ.get("S3_KEY")
MODEL_ID = os.environ["MODEL_ARN"]
REGION = os.environ.get("AWS_REGION", "eu-central-1")
MAX_ROWS = int(os.environ.get("MAX_ROWS", "200"))

_cfg = Config(read_timeout=60, connect_timeout=10, retries={"max_attempts": 2})
_s3 = boto3.client("s3", region_name=REGION, config=_cfg)
_runtime = boto3.client("bedrock-runtime", region_name=REGION, config=_cfg)

# Cache the DuckDB connection + schema text across warm invocations.
_STATE = {"con": None, "schema_text": None}

_FORBIDDEN = {
    "insert", "update", "delete", "drop", "alter", "create", "attach",
    "copy", "call", "pragma", "install", "load", "export", "import",
}


def _decode(raw):
    """Decode CSV bytes, detecting UTF-16/UTF-8 via BOM, and strip NUL bytes."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        enc = "utf-16"
    elif raw[:3] == b"\xef\xbb\xbf":
        enc = "utf-8-sig"
    else:
        enc = "utf-8"
    try:
        text = raw.decode(enc, errors="replace")
    except (UnicodeError, LookupError):
        text = raw.decode("latin-1", errors="replace")
    return text.replace("\x00", "")


def _list_csv_keys():
    if S3_PREFIX:
        keys = []
        paginator = _s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_PREFIX):
            for obj in page.get("Contents", []):
                if obj["Key"].lower().endswith(".csv"):
                    keys.append(obj["Key"])
        return sorted(keys)
    if S3_KEY:
        return [S3_KEY]
    raise ValueError("Set either S3_PREFIX (many files) or S3_KEY (one file).")


def _table_name(key):
    """'demo-imcm/equipment_data.csv' -> 'equipment_data'; sanitized for SQL identifiers."""
    base = key.rsplit("/", 1)[-1]
    if base.lower().endswith(".csv"):
        base = base[:-4]
    return re.sub(r"[^a-zA-Z0-9_]", "_", base)


def _get_con():
    """Load every CSV as a DuckDB table (cached across warm invocations)."""
    if _STATE["con"] is not None:
        return _STATE["con"]

    con = duckdb.connect(database=":memory:")
    tmpdir = tempfile.mkdtemp(prefix="csv_")
    loaded = []

    for key in _list_csv_keys():
        obj = _s3.get_object(Bucket=S3_BUCKET, Key=key)
        text = _decode(obj["Body"].read())
        table = _table_name(key)
        local_path = os.path.join(tmpdir, f"{table}.csv")
        with open(local_path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

        # read_csv_auto infers real column types (numbers, dates, etc.), so
        # comparisons like `age > 30` or `date > '2024-01-01'` work correctly.
        con.execute(
            f'CREATE TABLE "{table}" AS '
            f"SELECT * FROM read_csv_auto(?, SAMPLE_SIZE=-1, IGNORE_ERRORS=TRUE)",
            [local_path],
        )
        loaded.append(table)

    _STATE["con"] = con
    logger.info("Loaded %d table(s) into DuckDB: %s", len(loaded), ", ".join(loaded))
    return con


def _get_schema_text(con):
    if _STATE["schema_text"] is not None:
        return _STATE["schema_text"]

    rows = con.execute(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'main'
        ORDER BY table_name, ordinal_position
        """
    ).fetchall()

    tables = {}
    for table_name, column_name, data_type in rows:
        tables.setdefault(table_name, []).append(f"{column_name} {data_type}")

    schema_text = "\n".join(f"{t}({', '.join(cols)})" for t, cols in tables.items())
    _STATE["schema_text"] = schema_text
    return schema_text


def _generate_sql(query, schema_text, table_hint=None):
    hint_rule = (
        f'- Prefer the table "{table_hint}" unless the question clearly needs another/more tables.\n'
        if table_hint else ""
    )
    prompt = (
        "You are a SQL expert. Write ONE single read-only SELECT query (DuckDB "
        "SQL dialect, which is close to standard SQL/Postgres) that answers the "
        "user's question, using only the tables/columns below.\n\n"
        f"Tables:\n{schema_text}\n\n"
        "Rules:\n"
        "- Output ONLY the SQL statement, no markdown fences, no explanation.\n"
        "- SELECT statements only (CTEs with WITH are fine). Never write INSERT/"
        "UPDATE/DELETE/DDL.\n"
        "- You MAY join across tables, use GROUP BY, ORDER BY, aggregate functions "
        "(COUNT/SUM/AVG/MIN/MAX), and comparison operators (=, <, >, BETWEEN, IN).\n"
        "- Use ILIKE for case-insensitive partial text matches.\n"
        "- Quote table/column names with double quotes if they contain mixed case "
        "or special characters.\n"
        f"{hint_rule}"
        f"- Always include a LIMIT of at most {MAX_ROWS} rows unless the query is a "
        "single aggregate (e.g. one count/sum with no GROUP BY).\n\n"
        f"Question: {query}"
    )
    resp = _runtime.converse(
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
    return str(v)  # dates/decimals/etc. -> string, so the result is JSON-serializable


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
    logger.info("Incoming event: %s", json.dumps(event)[:2000])

    query = (_get_param(event, "query", "question") or "").strip()
    table_hint = _get_param(event, "Table", "table")
    if not query:
        return _respond(event, {"error": "Request must include a non-empty 'query' field."}, 400)

    try:
        con = _get_con()
        schema_text = _get_schema_text(con)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to load CSVs into DuckDB")
        return _respond(event, {"error": "Could not load CSV files from S3.", "detail": str(exc)}, 500)

    sql = _generate_sql(query, schema_text, table_hint)
    logger.info("Generated SQL: %s", sql)

    ok, reason = _is_safe_select(sql)
    if not ok:
        return _respond(
            event,
            {"error": "Generated query was rejected by the safety check.", "reason": reason, "sql": sql},
            400,
        )

    sql = _enforce_limit(sql)

    try:
        cur = con.execute(sql)
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        logger.exception("Query execution failed")
        return _respond(event, {"error": "Query execution failed.", "detail": str(exc), "sql": sql}, 400)

    rows = [{k: _clean(v) for k, v in row.items()} for row in rows]

    result = {
        "answer": f"Returned {len(rows)} row(s).",
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
    }
    logger.info("RESULT: %s", json.dumps(result, default=str)[:8000])
    return _respond(event, result, 200)

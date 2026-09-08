"""
Configuration: environment variables, tuning constants, and the shared
Bedrock client. Nothing here talks to Snowflake or does prompt engineering
-- if you're adding a new env var or constant, it goes here.
"""

import os
import re

import boto3
from botocore.config import Config

# A trailing `LIMIT n`. Shared by sql_safety.enforce_limit (which clamps an
# over-large limit) and snowflake_client.get_total_count (which must strip the
# model's own limit before wrapping the query in COUNT(*), or the count comes
# back equal to the limit every time). Lives here so neither module has to
# import the other.
TRAILING_LIMIT_RE = re.compile(r"\s+limit\s+(\d+)\s*$", re.IGNORECASE)

REGION_NAME = os.environ.get("region", os.environ.get("AWS_REGION", "eu-central-1"))
SNOWFLAKE_SECRET = os.environ["snowflake_secret"]
SNOWFLAKE_SECRET_PK = os.environ["snowflake_secret_pk"]
MODEL_ID = os.environ.get("model_arn") or os.environ["MODEL_ARN"]
MAX_ROWS = int(os.environ.get("MAX_ROWS", "200"))
MAX_TABLES = int(os.environ.get("MAX_TABLES", "5"))
ALLOWED_TABLES = [t.strip().upper() for t in os.environ.get("ALLOWED_TABLES", "").split(",") if t.strip()]

# --- value-sampling tuning knobs -------------------------------------------
CARDINALITY_SAMPLE_THRESHOLD = int(os.environ.get("CARDINALITY_SAMPLE_THRESHOLD", "50"))
VALUE_SAMPLE_LIMIT = int(os.environ.get("VALUE_SAMPLE_LIMIT", "20"))
VALUE_SAMPLE_TTL_SECONDS = int(os.environ.get("VALUE_SAMPLE_TTL_SECONDS", str(6 * 3600)))  # 6h default
CATALOG_TTL_SECONDS = int(os.environ.get("CATALOG_TTL_SECONDS", str(6 * 3600)))

# Extra Snowflake connections (beyond the single one from get_conn()) kept
# warm per container so value sampling can run one table per connection
# concurrently instead of serializing -- a connector connection isn't safe
# for concurrent cursors from multiple threads, so parallel sampling needs
# one connection per in-flight table, capped at this size.
SAMPLE_POOL_SIZE = int(os.environ.get("SAMPLE_POOL_SIZE", "3"))

# Stage 1 (table selection) LRU cache size -- repeated/near-identical
# questions (e.g. a BI dashboard re-issuing the same question) skip the
# table-selection Bedrock call entirely on a warm hit.
TABLE_SELECTION_CACHE_SIZE = int(os.environ.get("TABLE_SELECTION_CACHE_SIZE", "64"))

# Snowflake's INFORMATION_SCHEMA.COLUMNS normalizes declared types, e.g.
# VARCHAR/STRING/CHAR/TEXT -> "TEXT", INT/NUMBER/DECIMAL -> "NUMBER".
# Allow-list (not deny-list) so unexpected/new types safely fall through to "skip".
SAMPLABLE_TYPES = {"TEXT"}  # add "BOOLEAN" back if you want True/False sampled too

# Name patterns that make a TEXT column a poor sampling candidate even though
# its type qualifies (free text, identifiers, urls, raw semi-structured-as-text).
SKIP_NAME_SUBSTRINGS = ("_ID", "_TS", "_DATE", "_TIME", "_URL", "_DESC", "_NOTE", "_COMMENT", "_JSON", "_XML")

# Fraction of a column's sampled values that must look like opaque Vault
# surrogate keys ('V0V000000002060') before the column is dropped from the
# prompt. Offering such values invites a spurious filter on an ID the model
# cannot reason about.
OPAQUE_ID_MIN_FRACTION = float(os.environ.get("OPAQUE_ID_MIN_FRACTION", "0.6"))

# Candidates handed to the stage-1 table-selection call. Bounds that prompt to
# a constant size no matter how many tables the schema holds -- without it,
# compact_catalog_text emits every table with every column name.
TABLE_SHORTLIST_K = int(os.environ.get("TABLE_SHORTLIST_K", "25"))

MAX_STAR_COLUMNS = int(os.environ.get("MAX_STAR_COLUMNS", "20"))
SQL_REPAIR_MAX_ATTEMPTS = int(os.environ.get("SQL_REPAIR_MAX_ATTEMPTS", "2"))
LOG_ROW_PREVIEW_LIMIT = int(os.environ.get("LOG_ROW_PREVIEW_LIMIT", "5"))

_FORBIDDEN = {
    "insert", "update", "delete", "drop", "alter", "create", "merge",
    "truncate", "grant", "revoke", "call", "copy", "put", "remove", "use",
}

_cfg = Config(read_timeout=30, connect_timeout=10, retries={"max_attempts": 2})
bedrock = boto3.client("bedrock-runtime", region_name=REGION_NAME, config=_cfg)

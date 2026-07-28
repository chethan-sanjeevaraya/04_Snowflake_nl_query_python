"""
Configuration: environment variables, tuning constants, and the shared
Bedrock client. Nothing here talks to Snowflake or does prompt engineering
-- if you're adding a new env var or constant, it goes here.
"""

import os

import boto3
from botocore.config import Config

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

# Snowflake's INFORMATION_SCHEMA.COLUMNS normalizes declared types, e.g.
# VARCHAR/STRING/CHAR/TEXT -> "TEXT", INT/NUMBER/DECIMAL -> "NUMBER".
# Allow-list (not deny-list) so unexpected/new types safely fall through to "skip".
SAMPLABLE_TYPES = {"TEXT"}  # add "BOOLEAN" back if you want True/False sampled too

# Name patterns that make a TEXT column a poor sampling candidate even though
# its type qualifies (free text, identifiers, urls, raw semi-structured-as-text).
SKIP_NAME_SUBSTRINGS = ("_ID", "_TS", "_DATE", "_TIME", "_URL", "_DESC", "_NOTE", "_COMMENT", "_JSON", "_XML")

MAX_STAR_COLUMNS = int(os.environ.get("MAX_STAR_COLUMNS", "20"))
SQL_REPAIR_MAX_ATTEMPTS = int(os.environ.get("SQL_REPAIR_MAX_ATTEMPTS", "2"))
LOG_ROW_PREVIEW_LIMIT = int(os.environ.get("LOG_ROW_PREVIEW_LIMIT", "5"))

_FORBIDDEN = {
    "insert", "update", "delete", "drop", "alter", "create", "merge",
    "truncate", "grant", "revoke", "call", "copy", "put", "remove", "use",
}

_cfg = Config(read_timeout=30, connect_timeout=10, retries={"max_attempts": 2})
bedrock = boto3.client("bedrock-runtime", region_name=REGION_NAME, config=_cfg)

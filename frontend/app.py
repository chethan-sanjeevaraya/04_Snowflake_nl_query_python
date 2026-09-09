"""Streamlit chat UI for the natural-language -> Snowflake query Lambda.

Runs server-side, so the API call never happens in the user's browser: no CORS
preflight to configure, and any credential stays on this host.

    streamlit run app.py
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
import streamlit as st

from api_client import DEFAULT_TIMEOUT_SECONDS, QueryResult, SnowflakeNLClient

st.set_page_config(page_title="Snowflake NL Query", page_icon="~", layout="wide")

# Rows above this get a scrollable fixed-height grid instead of an
# auto-expanding one, so a 200-row answer doesn't bury the chat input.
TALL_TABLE_ROW_THRESHOLD = 12


def _initial_api_url() -> str:
    """Env var wins, then .streamlit/secrets.toml, then blank (sidebar input)."""
    if os.environ.get("SNOWFLAKE_NL_API_URL"):
        return os.environ["SNOWFLAKE_NL_API_URL"]
    try:
        return st.secrets.get("api_url", "")
    except Exception:  # noqa: BLE001 - no secrets.toml present is normal
        return ""


def _initial_api_key() -> str:
    if os.environ.get("SNOWFLAKE_NL_API_KEY"):
        return os.environ["SNOWFLAKE_NL_API_KEY"]
    try:
        return st.secrets.get("api_key", "")
    except Exception:  # noqa: BLE001
        return ""


for key, default in [
    ("messages", []),
    ("api_url", _initial_api_url()),
    ("api_key", _initial_api_key()),
    ("targets", []),
    ("target_error", ""),
    ("session_id", ""),
]:
    st.session_state.setdefault(key, default)


# --------------------------------------------------------------------------
# Sidebar: connection + per-request hints
# --------------------------------------------------------------------------

with st.sidebar:
    st.header("Connection")
    st.session_state.api_url = st.text_input(
        "API Gateway invoke URL",
        value=st.session_state.api_url,
        placeholder="https://abc123.execute-api.eu-central-1.amazonaws.com/dev/query",
        help="Set SNOWFLAKE_NL_API_URL to avoid retyping this each run.",
    )
    st.session_state.api_key = st.text_input(
        "API key (optional)",
        value=st.session_state.api_key,
        type="password",
        help="Sent as the x-api-key header. Leave blank if the endpoint is open.",
    )
    timeout = st.slider(
        "Client timeout (s)", min_value=15, max_value=180, value=DEFAULT_TIMEOUT_SECONDS, step=5
    )

    st.divider()
    st.header("Target")

    if st.button("Discover valid targets", use_container_width=True):
        if not st.session_state.api_url:
            st.session_state.target_error = "Set the API URL first."
            st.session_state.targets = []
        else:
            probe = SnowflakeNLClient(st.session_state.api_url, st.session_state.api_key, timeout)
            with st.spinner("Asking the API which targets it accepts..."):
                found, err = probe.discover_targets()
            st.session_state.targets = found
            st.session_state.target_error = err

    if st.session_state.targets:
        choice = st.selectbox(
            "DATABASE.SCHEMA", options=["(Lambda default)"] + st.session_state.targets
        )
        if choice == "(Lambda default)":
            database, schema = "", ""
        else:
            database, _, schema = choice.partition(".")
    else:
        if st.session_state.target_error:
            st.caption(f"Discovery failed: {st.session_state.target_error}")
        col_db, col_sc = st.columns(2)
        database = col_db.text_input("DATABASE", value="", placeholder="(default)")
        schema = col_sc.text_input("SCHEMA", value="", placeholder="(default)")
        st.caption("Leave both blank to use the Lambda's default target.")

    table_hint = st.text_input(
        "Table hint (optional)",
        value="",
        placeholder="EVENT__V, USER__SYS",
        help=(
            "Comma-separated. Skips the model's table-selection call and scopes the "
            "metadata query, which is much faster on a large schema."
        ),
    )

    st.divider()
    st.header("Behaviour")
    show_sql_expanded = st.toggle("Expand SQL by default", value=False)
    if st.session_state.session_id:
        st.caption(
            f"Conversation memory: session `{st.session_state.session_id[:8]}...` -- "
            "follow-ups and 'what about that one' refer back to this thread server-side. "
            "If the Lambda has no SESSION_TABLE configured, it silently ignores this and "
            "every question is answered fresh."
        )
    else:
        st.caption(
            "Conversation memory: no session yet -- starts after your first question, "
            "if the Lambda has SESSION_TABLE configured."
        )

    if st.button("Clear chat", use_container_width=True):
        st.session_state.messages = []
        st.session_state.session_id = ""
        st.rerun()


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _render_success(result: QueryResult, msg_index: int) -> None:
    payload = result.payload
    st.markdown(result.answer or "Query completed.")

    if result.truncated:
        st.warning(
            f"Showing {payload.get('row_count')} of "
            f"{payload.get('total_matching_rows')} matching rows.",
            icon=":material/filter_alt:",
        )
    if payload.get("note"):
        st.caption(payload["note"])

    meta_bits = []
    if payload.get("database") and payload.get("schema"):
        meta_bits.append(f"`{payload['database']}.{payload['schema']}`")
    if payload.get("tables_used"):
        meta_bits.append("tables: " + ", ".join(f"`{t}`" for t in payload["tables_used"]))
    meta_bits.append(f"{result.elapsed:.1f}s")
    st.caption(" · ".join(meta_bits))

    if result.sql:
        with st.expander("SQL", expanded=show_sql_expanded):
            st.code(result.sql, language="sql")

    if result.rows:
        df = pd.DataFrame(result.rows, columns=result.columns or None)
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            height=420 if len(df) > TALL_TABLE_ROW_THRESHOLD else None,
        )
        st.download_button(
            "Download CSV",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name=f"query_result_{msg_index}.csv",
            mime="text/csv",
            key=f"dl_{msg_index}",
        )
    else:
        st.info("The query ran successfully but matched no rows.", icon=":material/info:")


def _render_clarification(result: QueryResult, msg_index: int) -> None:
    """The Lambda couldn't confidently pick a table and is asking which one you
    meant, instead of just erroring out. Answering (typing a table name, or
    clicking one of the buttons below the LATEST such message) resolves it
    server-side via the session -- no need to repeat the original question."""
    st.markdown(result.clarification_message or "Which table did you mean?")
    st.caption(f"{result.elapsed:.1f}s")

    is_latest = msg_index == len(st.session_state.messages) - 1
    if is_latest and result.candidates:
        cols = st.columns(len(result.candidates))
        for i, (col, candidate) in enumerate(zip(cols, result.candidates)):
            if col.button(candidate, key=f"clarify_{msg_index}_{i}", use_container_width=True):
                st.session_state["_pending_answer"] = candidate
                st.rerun()


def _render_error(result: QueryResult) -> None:
    st.error(result.error, icon=":material/error:")
    if result.detail:
        with st.expander("Detail"):
            st.code(result.detail)

    payload = result.payload
    # A rejected-or-failed query still returns the SQL that caused it, which is
    # the most useful thing to show.
    if payload.get("reason"):
        st.caption(f"Reason: {payload['reason']}")
    if payload.get("sql"):
        with st.expander("Generated SQL", expanded=True):
            st.code(payload["sql"], language="sql")
    if payload.get("available_tables"):
        with st.expander(f"Available tables ({len(payload['available_tables'])})"):
            st.write(payload["available_tables"])
    if payload.get("valid_targets"):
        st.caption("Valid targets: " + ", ".join(payload["valid_targets"]))
    st.caption(f"{result.elapsed:.1f}s")


def _render_message(message: dict[str, Any], index: int) -> None:
    with st.chat_message(message["role"]):
        if message["role"] == "user":
            st.markdown(message["content"])
            return
        result: QueryResult = message["result"]
        if result.needs_clarification:
            _render_clarification(result, index)
        elif result.ok:
            _render_success(result, index)
        else:
            _render_error(result)


st.title("Snowflake NL Query")
st.caption(
    "Ask a question in plain English; the Lambda picks the tables, writes the SQL, and runs it."
)

if not st.session_state.api_url:
    st.info("Set your API Gateway invoke URL in the sidebar to start.", icon=":material/link:")

for i, message in enumerate(st.session_state.messages):
    _render_message(message, i)


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------

prompt = st.chat_input("e.g. how many events were created last month?")
# A click on one of the clarification buttons acts exactly like typing that
# table name as the next chat message.
prompt = prompt or st.session_state.pop("_pending_answer", None)

if prompt:
    if not st.session_state.api_url:
        st.error("Set the API Gateway invoke URL in the sidebar first.")
        st.stop()

    st.session_state.messages.append({"role": "user", "content": prompt})
    _render_message(st.session_state.messages[-1], len(st.session_state.messages) - 1)

    client = SnowflakeNLClient(st.session_state.api_url, st.session_state.api_key, timeout)
    with st.chat_message("assistant"):
        with st.spinner("Generating SQL and querying Snowflake..."):
            result = client.ask(
                prompt, table_hint, database, schema, session_id=st.session_state.session_id
            )

    if result.session_id:
        st.session_state.session_id = result.session_id

    st.session_state.messages.append({"role": "assistant", "result": result})
    st.rerun()

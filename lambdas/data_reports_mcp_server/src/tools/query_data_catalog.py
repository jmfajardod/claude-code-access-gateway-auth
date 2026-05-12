import csv
import io
import json
import logging
import re
import time
from datetime import datetime, timezone

import boto3
import fastmcp

from config import settings
from security import auth

_logger = logging.getLogger("data-reports-mcp.query_data_catalog")
_logger.setLevel(logging.INFO)

_TOOL_VERSION = "1.0.0-query-data-catalog"
_SQL_LOG_MAX_LEN = 2000  # truncate very long queries to keep log lines bounded

_ARN_RE = re.compile(r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:[^\s'\"]+")

_athena = boto3.client("athena")
_s3 = boto3.client("s3")


def _redact_arns(msg: str | None) -> str | None:
    """Replace any AWS ARN substrings with `<arn-redacted>`.
    """
    if not msg:
        return msg
    return _ARN_RE.sub("<arn-redacted>", msg)


def _log_event(level: str, event: str, **fields) -> None:
    """Emit a single-line JSON log entry.

    Args:
        level: Standard logging level name in lowercase
        event: Short dot-delimited event identifier
        **fields: Arbitrary structured context
    """
    payload = {"event": event, "tool": "query_data_catalog", **fields}
    try:
        message = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        message = repr(payload)
    getattr(_logger, level, _logger.info)(message)


def _truncate_sql(sql: str | None) -> str | None:
    """Cap SQL length for log lines so a 10 MB query doesn't flood CW Logs."""
    if sql is None:
        return None
    if len(sql) <= _SQL_LOG_MAX_LEN:
        return sql
    return sql[:_SQL_LOG_MAX_LEN] + f"...[truncated, total={len(sql)} chars]"


def _start_query(sql: str, database: str | None) -> str:
    """Submit a SQL query to Athena and return the execution ID.

    Args:
        sql: The SQL query to execute.
        database: Optional default database

    Returns:
        The Athena QueryExecutionId for polling.
    """
    kwargs: dict = {
        "QueryString": sql,
        "WorkGroup": settings.athena_workgroup,
    }
    if database:
        kwargs["QueryExecutionContext"] = {"Database": database}
    return _athena.start_query_execution(**kwargs)["QueryExecutionId"]


def _wait_for_query(query_execution_id: str) -> dict:
    """Poll Athena until the query reaches a terminal state.

    Args:
        query_execution_id: The ID returned by `start_query_execution`.

    Returns:
        The full `QueryExecution` dict (caller inspects `Status.State`).

    Raises:
        TimeoutError: If the query does not finish within
            `settings.athena_query_timeout_seconds`.
    """
    deadline = time.time() + settings.athena_query_timeout_seconds
    backoff = 0.5
    while True:
        resp = _athena.get_query_execution(QueryExecutionId=query_execution_id)
        execution = resp["QueryExecution"]
        state = execution["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return execution
        if time.time() >= deadline:
            try:
                _athena.stop_query_execution(QueryExecutionId=query_execution_id)
            except Exception:  # noqa: BLE001  best-effort cancel
                pass
            raise TimeoutError(
                f"Athena query did not finish within "
                f"{settings.athena_query_timeout_seconds}s"
            )
        time.sleep(backoff)
        backoff = min(backoff * 1.5, 5.0)


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    assert uri.startswith("s3://"), f"unexpected Athena output URI {uri!r}"
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


def _read_result_csv(output_location: str) -> tuple[list[str], list[list[str]], int]:
    """Download and parse the Athena result CSV from S3.

    Args:
        output_location: The `s3://...` URI 

    Returns:
        Tuple of `(header_row, data_rows, csv_size_bytes)`.
    """
    bucket, key = _parse_s3_uri(output_location)
    obj = _s3.get_object(Bucket=bucket, Key=key)
    size_bytes = int(obj.get("ContentLength", 0))
    text = obj["Body"].read().decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])
    rows = list(reader)
    return header, rows, size_bytes


def _rows_to_dicts(header: list[str], rows: list[list[str]]) -> list[dict]:
    """Zip header into each data row to produce a list of column-keyed dicts.

    Args:
        header: Column names from the CSV header row.
        rows: Data rows as lists of strings.

    Returns:
        One dict per row mapping column_name to string_value.
    """
    return [
        {col: (row[idx] if idx < len(row) else "") for idx, col in enumerate(header)}
        for row in rows
    ]


def _error(
    error_type: str,
    message: str,
    *,
    query_execution_id: str | None = None,
    athena_state: str | None = None,
    sql: str | None = None,
) -> dict:
    """Build a structured error envelope for the MCP response.

    Args:
        error_type: Short machine-readable identifier
        message: Human-readable error description.
        query_execution_id: Athena execution ID, if a query was started.
        athena_state: Athena terminal state
        sql: The original SQL string

    Returns:
        Dict suitable for direct return from the MCP tool.
    """
    body: dict = {
        "status": "error",
        "error": {"type": error_type, "message": _redact_arns(message)},
        "_meta": {"tool_version": _TOOL_VERSION},
    }
    if query_execution_id:
        body["error"]["query_execution_id"] = query_execution_id
    if athena_state:
        body["error"]["athena_state"] = athena_state
    if sql:
        body["error"]["sql"] = sql
    return body


def query_data_catalog(sql: str, database: str | None = None) -> dict:
    """Run a SQL query against the Glue Data Catalog via Athena.

    The query MUST be valid **Athena SQL (Trino dialect, engine v3)**.
    Common dialect notes:
      - Date literal: ``DATE '2024-01-01'`` (no Postgres-style ``::date``).
      - Cast: ``CAST(x AS type)`` only — no ``::`` casts.
      - String concat: ``||`` or ``CONCAT(...)``.
      - Identifier quoting: double quotes (``"col name"``).
      - String literals: single quotes only (``'value'``).
      - Regex: ``regexp_like(col, 'pattern')`` (case-sensitive).
      - Date math: ``date_add('day', 7, col)``, ``date_diff('day', a, b)``.
      - JSON: ``json_extract_scalar(col, '$.field')``.
      - Row limit: ``LIMIT n`` only — no ``TOP n`` / ``FETCH FIRST``.
      - Arrays: ``ARRAY[1, 2, 3]``; unnest via ``CROSS JOIN UNNEST(...)``.

    Schema discovery: do **NOT** use ``DESCRIBE``. Under tag-based
    LakeFormation, ``DESCRIBE`` calls a Glue path that requires permission
    on the workgroup's default database (``default``) which this tool's
    caller lacks; the error you get back is misleading ("not authorized on
    database/default"). Use ``information_schema`` instead::

        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'mydb' AND table_name = 'mytable'
        ORDER BY ordinal_position

    Numeric quirk: numeric columns surface as JSON **strings** in the
    response even when Glue declares them ``int``/``double`` (the result
    CSV stringifies everything). Always ``CAST`` before aggregating, e.g.
    ``SUM(CAST(total_price AS DOUBLE))``.

    Totals & marginals: do NOT sum displayed cell values by hand to
    produce grand totals or row/column subtotals — long mental arithmetic
    occasionally drifts. Instead, push the rollup into a single SQL query
    with ``GROUP BY ROLLUP(col_a, col_b)`` or ``GROUPING SETS (...)``::

        SELECT region, channel,
               SUM(CAST(total_price AS DOUBLE)) AS revenue
        FROM mcp_poc_db.sales_gold
        GROUP BY ROLLUP (region, channel)
        ORDER BY region, channel

    The rollup yields the per-cell rows plus the per-region subtotals,
    per-channel subtotals (use ``GROUPING SETS`` for both axes plus a
    grand total), and the grand total — all in one query, all arithmetic
    done by Athena.

    Args:
        sql: The SQL query to execute. Use ``database.table`` to reference
            tables across multiple Gold-tagged databases in one statement.
        database: Optional default database for unqualified table refs.
            Equivalent to a ``USE <database>`` prefix; omit to require
            fully-qualified names.

    Returns:
        A JSON-serializable dict. Always includes `status` and `_meta`.

        On error: keys are `status="error"` and `error` (with `type`,
        `message`, and optionally `query_execution_id`, `athena_state`,
        `sql`).

        On small success (inline): keys are `status="success"`,
        `inline=True`, `query_execution_id`, `schema`, `row_count`,
        `rows`, `next_steps`.

        On large success (presigned): keys are `status="success"`,
        `inline=False`, `query_execution_id`, `schema`, `row_count`,
        `full_results` (with `format`, `size_bytes`, `presigned_url`,
        `presigned_url_ttl_seconds`, `presigned_url_expires_at_unix`,
        `s3_uri`), `next_steps`.
    """
    started_at = time.perf_counter()
    identity = auth.enforce_domain_or_raise()

    caller_email = (identity or {}).get("email") or "dev"
    caller_domain = (identity or {}).get("domain") or "n/a"

    _log_event(
        "info",
        "query_data_catalog.invoked",
        caller_email=caller_email,
        caller_domain=caller_domain,
        database=database,
        sql=_truncate_sql(sql) if isinstance(sql, str) else None,
        sql_chars=len(sql) if isinstance(sql, str) else None,
    )

    if not settings.athena_workgroup or not settings.athena_results_bucket:
        _log_event(
            "error",
            "query_data_catalog.configuration_error",
            athena_workgroup=settings.athena_workgroup,
            athena_results_bucket=settings.athena_results_bucket,
            caller_email=caller_email,
        )
        return _error(
            "configuration_error",
            "Athena settings not configured (athena_workgroup / athena_results_bucket)",
        )

    if not isinstance(sql, str) or not sql.strip():
        _log_event(
            "warning",
            "query_data_catalog.invalid_input",
            caller_email=caller_email,
            sql_type=type(sql).__name__,
        )
        return _error("invalid_input", "sql must be a non-empty string", sql=sql)

    # --- start query -----------------------------------------------------
    start_phase = time.perf_counter()
    try:
        qid = _start_query(sql, database)
    except Exception as exc:  # noqa: BLE001  surface the error to Claude
        _log_event(
            "error",
            "query_data_catalog.start_failed",
            caller_email=caller_email,
            error_type=type(exc).__name__,
            error=str(exc),
            sql=_truncate_sql(sql),
        )
        _logger.exception("Failed to start Athena query")
        return _error("athena_start_failed", str(exc), sql=sql)
    start_ms = round((time.perf_counter() - start_phase) * 1000, 2)
    _log_event(
        "info",
        "query_data_catalog.started",
        query_execution_id=qid,
        caller_email=caller_email,
        database=database,
        start_ms=start_ms,
    )

    # --- poll until done -------------------------------------------------
    poll_phase = time.perf_counter()
    try:
        execution = _wait_for_query(qid)
    except TimeoutError as exc:
        _log_event(
            "error",
            "query_data_catalog.timeout",
            query_execution_id=qid,
            caller_email=caller_email,
            timeout_seconds=settings.athena_query_timeout_seconds,
            sql=_truncate_sql(sql),
        )
        return _error("timeout", str(exc), query_execution_id=qid, sql=sql)
    except Exception as exc:  # noqa: BLE001
        _log_event(
            "error",
            "query_data_catalog.poll_failed",
            query_execution_id=qid,
            caller_email=caller_email,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        _logger.exception("Failed while polling Athena query")
        return _error("athena_poll_failed", str(exc), query_execution_id=qid, sql=sql)
    poll_ms = round((time.perf_counter() - poll_phase) * 1000, 2)

    status = execution["Status"]
    state = status["State"]
    state_change_reason = status.get("StateChangeReason")
    athena_stats = execution.get("Statistics", {}) or {}

    if state != "SUCCEEDED":
        raw_reason = state_change_reason or f"Query ended in state {state}"
        is_table_not_found = "TABLE_NOT_FOUND" in raw_reason
        if is_table_not_found:
            err_type = "table_not_accessible"
            client_message = (
                f"{raw_reason}  Note: this error is returned either when the "
                "table does not exist OR when the caller's role lacks "
                "permission to read it. Through this tool, only tables tagged "
                "with the appropriate LF tags are queryable; tables in other layers "
                "appear as not-found regardless of whether they exist."
            )
        else:
            err_type = "athena_query_failed"
            client_message = raw_reason

        _log_event(
            "error",
            "query_data_catalog.athena_failed",
            query_execution_id=qid,
            caller_email=caller_email,
            athena_state=state,
            state_change_reason=raw_reason,
            table_not_found_path=is_table_not_found,
            engine_execution_time_ms=athena_stats.get("EngineExecutionTimeInMillis"),
            data_scanned_bytes=athena_stats.get("DataScannedInBytes"),
            poll_ms=poll_ms,
            sql=_truncate_sql(sql),
        )
        return _error(
            err_type,
            client_message,
            query_execution_id=qid,
            athena_state=state,
            sql=sql,
        )

    output_location = execution["ResultConfiguration"]["OutputLocation"]

    # --- read CSV --------------------------------------------------------
    read_phase = time.perf_counter()
    try:
        header, rows, size_bytes = _read_result_csv(output_location)
    except Exception as exc:  # noqa: BLE001
        _log_event(
            "error",
            "query_data_catalog.result_read_failed",
            query_execution_id=qid,
            caller_email=caller_email,
            output_location=output_location,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        _logger.exception("Failed to read Athena result CSV")
        return _error("result_read_failed", str(exc), query_execution_id=qid, sql=sql)
    read_ms = round((time.perf_counter() - read_phase) * 1000, 2)

    row_count = len(rows)
    inline_candidate = _rows_to_dicts(header, rows)
    inline_serialized_bytes = len(
        json.dumps(inline_candidate, default=str).encode("utf-8")
    )

    if (
        row_count <= settings.athena_inline_max_rows
        and inline_serialized_bytes <= settings.athena_inline_max_bytes
    ):
        total_ms = round((time.perf_counter() - started_at) * 1000, 2)
        _log_event(
            "info",
            "query_data_catalog.succeeded",
            query_execution_id=qid,
            caller_email=caller_email,
            row_count=row_count,
            inline=True,
            inline_bytes=inline_serialized_bytes,
            csv_bytes=size_bytes,
            engine_execution_time_ms=athena_stats.get("EngineExecutionTimeInMillis"),
            data_scanned_bytes=athena_stats.get("DataScannedInBytes"),
            start_ms=start_ms,
            poll_ms=poll_ms,
            read_ms=read_ms,
            total_ms=total_ms,
        )
        return {
            "status": "success",
            "inline": True,
            "query_execution_id": qid,
            "schema": header,
            "row_count": row_count,
            "rows": inline_candidate,
            "next_steps": (
                f"Returned {row_count:,} rows inline (~{inline_serialized_bytes:,} B). "
                "Use the rows directly to answer the user's question."
            ),
            "_meta": {"tool_version": _TOOL_VERSION},
        }

    bucket, key = _parse_s3_uri(output_location)
    presigned_url = _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=settings.presigned_url_ttl_seconds,
    )
    ttl_minutes = max(1, settings.presigned_url_ttl_seconds // 60)
    size_kb = max(1, round(size_bytes / 1024))
    expires_at = (
        int(datetime.now(timezone.utc).timestamp())
        + settings.presigned_url_ttl_seconds
    )
    total_ms = round((time.perf_counter() - started_at) * 1000, 2)
    _log_event(
        "info",
        "query_data_catalog.succeeded",
        query_execution_id=qid,
        caller_email=caller_email,
        row_count=row_count,
        inline=False,
        csv_bytes=size_bytes,
        engine_execution_time_ms=athena_stats.get("EngineExecutionTimeInMillis"),
        data_scanned_bytes=athena_stats.get("DataScannedInBytes"),
        s3_uri=output_location,
        start_ms=start_ms,
        poll_ms=poll_ms,
        read_ms=read_ms,
        total_ms=total_ms,
    )
    return {
        "status": "success",
        "inline": False,
        "query_execution_id": qid,
        "schema": header,
        "row_count": row_count,
        "full_results": {
            "format": "csv",
            "size_bytes": size_bytes,
            "presigned_url": presigned_url,
            "presigned_url_ttl_seconds": settings.presigned_url_ttl_seconds,
            "presigned_url_expires_at_unix": expires_at,
            "s3_uri": output_location,
        },
        "next_steps": (
            f"Returned {row_count:,} rows (~{size_kb:,} KB) — too large to inline. "
            f"Tell the user: download the CSV from `full_results.presigned_url` "
            f"(expires in ~{ttl_minutes} minutes) and drag it into this chat."
        ),
        "_meta": {"tool_version": _TOOL_VERSION},
    }


def register(mcp: fastmcp.FastMCP) -> None:
    """Register `query_data_catalog` as an MCP tool on the given server.

    Args:
        mcp: The FastMCP server instance to attach the tool to.
    """
    mcp.tool()(query_data_catalog)

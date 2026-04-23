"""MCP target Lambda — invoked by AgentCore Gateway for tools/call.

AgentCore does all MCP/JSON-RPC framing itself.  Contract here:

  event                                                    -> raw tool arguments dict
  context.client_context.custom["bedrockAgentCoreToolName"]-> "<TargetName>___<tool>"
                                                              (triple underscore)

Return value -> HTTP-proxy shape: {"statusCode": 200, "body": <json-string>}.
AgentCore unwraps `body` and emits it back to the MCP client inside a
tools/call `result.content[].text` envelope.

`initialize` and `tools/list` are NOT routed here — AgentCore answers those
from the inline tool schema registered on the CfnGatewayTarget.

Tools implemented:
  - get_weather: canned weather response for a location
  - get_time:    current time for a timezone
  - query_data:  presigned S3 URL for a seeded CSV dataset (Phase 1 probe for
                 Athena/Glue — see README)
"""

import csv
import io
import json
import logging
import os
import statistics
import typing
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3

_LOGGER = logging.getLogger()
_LOGGER.setLevel(logging.INFO)

_TARGET_PREFIX_SEP = "___"

_RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET", "")
_RESULTS_OBJECT_KEY = os.environ.get("RESULTS_OBJECT_KEY", "")
_PRESIGNED_URL_TTL_SECONDS = int(os.environ.get("PRESIGNED_URL_TTL_SECONDS", "300"))
_SAMPLE_ROW_COUNT = 50
_TOOL_VERSION = "1.0.0-phase1"
_TOP_VALUES_MAX = 5
_LOW_CARDINALITY_THRESHOLD = 30

_s3_client = boto3.client("s3")


def _read_seed_csv() -> tuple[list[str], list[list[str]], int]:
    obj = _s3_client.get_object(Bucket=_RESULTS_BUCKET, Key=_RESULTS_OBJECT_KEY)
    size_bytes = int(obj.get("ContentLength", 0))
    text = obj["Body"].read().decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    rows = list(reader)
    return header, rows, size_bytes


def _infer_column_type(values: list[str]) -> str:
    """Internal type inference used to pick which summary stats to compute."""
    sample = [v for v in values if v]
    if not sample:
        return "string"
    try:
        for v in sample:
            int(v)
        return "integer"
    except ValueError:
        pass
    try:
        for v in sample:
            float(v)
        return "number"
    except ValueError:
        pass
    try:
        for v in sample:
            datetime.fromisoformat(v)
        return "date"
    except ValueError:
        pass
    return "string"


def _compute_summary_stats(header: list[str], rows: list[list[str]]) -> dict:
    stats: dict = {}
    for idx, col_name in enumerate(header):
        col_values = [row[idx] if idx < len(row) else "" for row in rows]
        non_null = [v for v in col_values if v != ""]
        nulls = len(col_values) - len(non_null)
        col_type = _infer_column_type(non_null)

        if col_type in ("integer", "number") and non_null:
            nums = [float(v) for v in non_null]
            stats[col_name] = {
                "min": min(nums),
                "max": max(nums),
                "mean": round(statistics.fmean(nums), 4),
                "stddev": round(statistics.pstdev(nums), 4) if len(nums) > 1 else 0.0,
                "nulls": nulls,
            }
        elif col_type == "date" and non_null:
            stats[col_name] = {
                "min": min(non_null),
                "max": max(non_null),
                "distinct": len(set(non_null)),
                "nulls": nulls,
            }
        else:
            distinct_values = set(non_null)
            col_stats: dict = {
                "distinct": len(distinct_values),
                "nulls": nulls,
            }
            if 0 < len(distinct_values) <= _LOW_CARDINALITY_THRESHOLD:
                col_stats["top"] = [
                    {"value": value, "count": count}
                    for value, count in Counter(non_null).most_common(_TOP_VALUES_MAX)
                ]
            stats[col_name] = col_stats
    return stats


def _unprefix_tool(name: str) -> str:
    _, sep, tail = name.partition(_TARGET_PREFIX_SEP)
    return tail if sep else name


def _ok(payload: dict) -> dict:
    return {"statusCode": 200, "body": json.dumps(payload)}


def _error(code: int, message: str) -> dict:
    return {"statusCode": code, "body": json.dumps({"error": message})}


def handler(event: dict, context: typing.Any) -> dict:
    tool_name = ""
    try:
        if getattr(context, "client_context", None) is not None:
            custom = context.client_context.custom or {}
            tool_name = custom.get("bedrockAgentCoreToolName", "")
    except Exception as exc:
        _LOGGER.warning("Failed to read client_context: %s", exc)

    _LOGGER.info("Target invoked: tool=%r args=%s", tool_name, json.dumps(event))

    unprefixed = _unprefix_tool(tool_name)

    if unprefixed == "get_weather":
        location = event.get("location", "unknown")
        return _ok({"weather": f"Weather in {location}: 72F, sunny"})

    if unprefixed == "get_time":
        tz_name = event.get("timezone", "UTC")
        try:
            now = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return _ok({"time": f"Current time in {tz_name}: {now}"})

    if unprefixed == "query_data":
        if not _RESULTS_BUCKET or not _RESULTS_OBJECT_KEY:
            return _error(500, "Results bucket not configured")
        try:
            header, rows, size_bytes = _read_seed_csv()
            url = _s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": _RESULTS_BUCKET, "Key": _RESULTS_OBJECT_KEY},
                ExpiresIn=_PRESIGNED_URL_TTL_SECONDS,
            )
        except Exception as exc:
            _LOGGER.exception("query_data failed")
            return _error(500, f"query_data failed: {exc}")

        sample_rows = [dict(zip(header, row)) for row in rows[:_SAMPLE_ROW_COUNT]]
        ttl_minutes = max(1, _PRESIGNED_URL_TTL_SECONDS // 60)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=_PRESIGNED_URL_TTL_SECONDS)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        size_kb = max(1, round(size_bytes / 1024))

        next_steps = (
            f"Returned {len(rows):,} rows (~{size_kb:,} KB). Summary statistics and the "
            f"first {len(sample_rows)} rows are shown inline — use these for quick "
            "analysis. For full-row analysis, tell the user: download the CSV from "
            f"`presigned_url` (expires in ~{ttl_minutes} minutes) and drag it into this "
            "chat; the analysis tool can then process every row."
        )

        return _ok(
            {
                "status": "success",
                "row_count": len(rows),
                "summary_stats": _compute_summary_stats(header, rows),
                "sample_rows": sample_rows,
                "full_results": {
                    "format": "csv",
                    "size_bytes": size_bytes,
                    "presigned_url": url,
                    "presigned_url_expires_at": expires_at,
                    "presigned_url_ttl_seconds": _PRESIGNED_URL_TTL_SECONDS,
                    "inline": False,
                },
                "next_steps": next_steps,
                "_meta": {"tool_version": _TOOL_VERSION},
            }
        )

    return _error(400, f"Unknown tool: {tool_name!r}")

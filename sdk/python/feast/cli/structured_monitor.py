"""Single-target monitoring; the structured batch path propagates failures.

Automatic and multi-target monitoring remain unsupported until the service can
report per-view outcomes rather than swallowing failures.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import click

from feast.cli.output import StructuredError, invocation
from feast.repo_operations import create_feature_store


def monitor_single_target(ctx: click.Context, **params: Any) -> dict[str, Any]:
    source_type = params["source_type"]
    if (
        source_type not in {"log", "batch"}
        or (
            source_type == "log"
            and (
                not params["feature_service"]
                or params["feature_view"]
                or params["feature_name"]
            )
        )
        or (
            source_type == "batch"
            and (not params["feature_view"] or params["feature_service"])
        )
        or not params["start_date"]
        or not params["end_date"]
        or params["set_baseline"]
    ):
        raise StructuredError(
            "UNSUPPORTED_CAPABILITY",
            "Structured monitoring requires one named target and explicit dates.",
            "Use --source-type log --feature-service NAME or --source-type batch "
            "--feature-view NAME with --start-date and --end-date; baseline and auto modes are unsupported.",
        )
    try:
        start = date.fromisoformat(params["start_date"])
        end = date.fromisoformat(params["end_date"])
    except ValueError as e:
        raise click.UsageError("Dates must use YYYY-MM-DD format") from e
    if start > end:
        raise click.UsageError("Start date must not exceed end date")

    from feast.monitoring.monitoring_service import MonitoringService

    store = create_feature_store(ctx)
    project = params["project"] or store.project
    state = invocation.get()
    assert state is not None
    # The call may persist metrics before an exception is raised.
    state.operation_started = True
    service = MonitoringService(store)
    if source_type == "log":
        result = service.compute_log_metrics(
            project=project,
            feature_service_name=params["feature_service"],
            start_date=start,
            end_date=end,
            granularity=params["granularity"] or "daily",
        )
    else:
        result = service.compute_metrics(
            project=project,
            feature_view_name=params["feature_view"],
            feature_names=list(params["feature_name"]) or None,
            start_date=start,
            end_date=end,
            granularity=params["granularity"] or "daily",
            raise_on_error=True,
        )
    if source_type == "log" and result["status"] == "skipped":
        return {
            "status": "skipped",
            "project": project,
            "target": params["feature_service"],
            "reason_code": "NO_LOGGING_SOURCE",
        }
    if result["status"] != "completed":
        raise StructuredError(
            "OPERATION_FAILED",
            "The monitoring service did not confirm completion.",
            "Inspect persisted metrics before retrying.",
        )
    data = {
        "status": "completed",
        "project": project,
        "target": params["feature_service"]
        if source_type == "log"
        else params["feature_view"],
        "source_type": source_type,
        "granularity": result["granularity"],
        "computed_features": result["computed_features"],
        "duration_ms": result["duration_ms"],
    }
    if source_type == "log":
        data["metric_date"] = result["metric_date"]
    else:
        data["computed_feature_views"] = result["computed_feature_views"]
        data["computed_feature_services"] = result["computed_feature_services"]
        data["metric_dates"] = result["metric_dates"]
    return data

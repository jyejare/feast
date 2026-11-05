"""Bounded feature retrieval for machine consumers.

Never serialize arbitrary pandas/protobuf internals; rely on existing SDK converters
and an explicit maximum for output size. Large training datasets need export APIs.
"""

from __future__ import annotations

import json
from typing import Any

import click
import pandas as pd

from feast.cli.output import StructuredError
from feast.repo_operations import create_feature_store

MAX_ROWS = 100
MAX_INPUT_BYTES = 1_000_000
MAX_FEATURE_REFS = 100


def retrieve(path: str, ctx: click.Context, **params: Any) -> dict[str, Any]:
    if len(params["features"]) > MAX_FEATURE_REFS:
        raise click.UsageError("Request at most 100 feature references")
    if path == "get-online-features":
        if (
            sum(len(raw.encode("utf-8")) for raw in params["entities"])
            > MAX_INPUT_BYTES
        ):
            raise click.UsageError("Entity input exceeds the 1 MB limit")
        entities: dict[str, list[str]] = {}
        for raw in params["entities"]:
            key, separator, value = raw.partition("=")
            if not separator or not key or not value:
                raise click.UsageError("Entities must use key=value format")
            entities.setdefault(key, []).append(value)
        lengths = {len(values) for values in entities.values()}
        if len(lengths) != 1 or next(iter(lengths)) > MAX_ROWS:
            raise click.UsageError(
                "Entity columns must have equal length and at most 100 rows"
            )
        rows = [dict(zip(entities, values)) for values in zip(*entities.values())]
        store = create_feature_store(ctx)
        values = store.get_online_features(
            features=list(params["features"]), entity_rows=rows
        ).to_dict()
        if any(len(column) > MAX_ROWS for column in values.values()):
            raise StructuredError(
                "RESULT_TOO_LARGE",
                "Feature result exceeds the CLI row limit.",
                "Request fewer entities or use the feature server.",
            )
        return {"features": values}

    dataframe = params["dataframe"]
    start, end = params["start_date"], params["end_date"]
    if dataframe and len(dataframe.encode("utf-8")) > MAX_INPUT_BYTES:
        raise click.UsageError("Entity dataframe input exceeds the 1 MB limit")
    if not params["features"] or not dataframe or start or end:
        raise click.UsageError(
            "Structured historical retrieval requires --features and a bounded --dataframe; "
            "date-range-only queries are not supported"
        )
    entity_df = None
    if dataframe:
        try:
            rows = json.loads(dataframe)
            if not isinstance(rows, list) or len(rows) > MAX_ROWS:
                raise ValueError("invalid rows")
            entity_df = pd.DataFrame(rows)
            entity_df["event_timestamp"] = pd.to_datetime(entity_df["event_timestamp"])
            if entity_df["event_timestamp"].isna().any():
                raise ValueError("missing event timestamps")
        except (TypeError, KeyError, ValueError) as e:
            raise click.UsageError("Invalid entity dataframe or too many rows") from e
    store = create_feature_store(ctx)
    job = store.get_historical_features(
        entity_df=entity_df,
        features=list(params["features"]),
        start_date=None,
        end_date=None,
    )
    result = job.to_df()
    if len(result) > MAX_ROWS:
        raise StructuredError(
            "RESULT_TOO_LARGE",
            "Historical result exceeds the CLI row limit.",
            "Use a bounded entity dataframe or export with the SDK.",
        )
    return {"items": json.loads(result.to_json(orient="records", date_format="iso"))}

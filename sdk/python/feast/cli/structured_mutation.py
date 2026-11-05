"""Explicit, non-interactive mutation endpoints for structured CLI consumers."""

from __future__ import annotations

from typing import Any

import click

from feast.cli.output import StructuredError, invocation
from feast.feature_view import FeatureView, FeatureViewState
from feast.on_demand_feature_view import OnDemandFeatureView
from feast.repo_operations import create_feature_store


def mutate(path: str, ctx: click.Context, **params: Any) -> dict[str, Any]:
    if path in {"projects delete", "delete", "teardown"} and not params["yes"]:
        raise StructuredError(
            "CONFIRMATION_REQUIRED",
            "Deletion requires explicit confirmation.",
            "Inspect the target and repeat with --yes (and --type for generic delete).",
            exit_code=2,
        )
    if path == "delete" and not params["object_type_name"]:
        raise StructuredError(
            "INVALID_ARGUMENT",
            "An explicit object type is required.",
            "Use delete NAME --type entity|feature-view|feature-service|data-source|... --yes.",
            exit_code=2,
        )
    store = create_feature_store(ctx)
    state = invocation.get()
    assert state is not None
    if path == "teardown":
        state.operation_started = True
        store.teardown()
        return {"action": "teardown", "status": "completed"}
    if path == "delete":
        kind = params["object_type_name"]
        getter = getattr(store, "get_" + kind.replace("-", "_"))
        obj = getter(params["object_id"])
        if isinstance(obj, list):
            raise StructuredError(
                "UNSUPPORTED_CAPABILITY",
                "This object type requires a unique target.",
                "Use an unambiguous object identity and retry.",
            )
        state.operation_started = True
        store.apply([obj], objects_to_delete=[obj], partial=False)
        return {"action": "deleted", "type": kind, "name": obj.name}
    name = params["name"]
    if path == "projects delete":
        # Check existence before marking mutation attempted; no hidden prompt.
        store.get_project(name)
        state.operation_started = True
        store.delete_project(name)
        return {"name": name, "action": "deleted", "type": "Project"}

    fv = store.registry.get_any_feature_view(name, store.project)
    if not isinstance(fv, (FeatureView, OnDemandFeatureView)):
        raise StructuredError(
            "UNSUPPORTED_CAPABILITY",
            "This view cannot be updated by the command.",
            "Select a batch or on-demand feature view.",
        )
    action = path.rsplit(" ", 1)[-1]
    if action == "set-state":
        target = FeatureViewState[params["state"].upper()]
        if fv.state != target and not fv.state.can_transition_to(target):
            raise StructuredError(
                "INVALID_ARGUMENT",
                "The requested state transition is not allowed.",
                "Inspect the current feature view state and allowed transitions.",
                exit_code=2,
            )
        changed = fv.state != target
        fv.state = target
        value: Any = target.name
    else:
        value = action == "enable"
        changed = fv.enabled != value
        fv.enabled = value
    if changed:
        state.operation_started = True
        store.registry.apply_feature_view(fv, store.project)
    return {
        "name": name,
        "action": action,
        "changed": changed,
        "enabled" if action != "set-state" else "state": value,
    }

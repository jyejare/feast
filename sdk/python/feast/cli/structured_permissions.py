"""Minimal permission introspection without dumping policy subjects or tag values."""

from __future__ import annotations

from typing import Any

import click

from feast import utils
from feast.cli import cli_utils
from feast.errors import FeastObjectNotFoundException
from feast.permissions.action import ALL_ACTIONS
from feast.repo_operations import create_feature_store


def _summary(permission: Any) -> dict[str, Any]:
    return {
        "name": permission.name,
        "types": sorted(t.__name__ for t in permission.types),
        "actions": sorted(action.value for action in permission.actions),
        "policy_type": type(permission.policy).__name__,
        # Name-patterns, required tags and policy subjects may contain sensitive data.
    }


def inspect_permissions(path: str, ctx: click.Context, **params: Any) -> dict[str, Any]:
    store = create_feature_store(ctx)
    action = path.split()[1]
    if action == "describe":
        return _summary(store.get_permission(params["name"]))
    if action == "list":
        permissions = store.list_permissions(
            tags=utils.tags_list_to_dict(params["tags"])
        )
        return {
            "items": sorted(
                (_summary(p) for p in permissions), key=lambda item: item["name"]
            )
        }
    permissions = store.list_permissions()
    if action == "list-roles":
        return {"items": cli_utils.fetch_all_permission_roles(permissions)}
    if action == "check":
        objects = cli_utils.fetch_all_feast_objects(store)
        unsecured = []
        for obj in objects:
            unmatched = [
                action.value
                for action in ALL_ACTIONS
                if not any(
                    p.match_resource(obj) and p.match_actions([action])
                    for p in permissions
                )
            ]
            if unmatched:
                unsecured.append(
                    {
                        "name": obj.name,
                        "type": type(obj).__name__,
                        "unsecured_actions": sorted(unmatched),
                    }
                )
        return {
            "items": sorted(unsecured, key=lambda item: (item["type"], item["name"]))
        }
    raise FeastObjectNotFoundException()

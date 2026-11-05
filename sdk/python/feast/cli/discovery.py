"""Command inventory and opt-in structured dispatch, without executing callbacks."""

from __future__ import annotations

from functools import wraps
from importlib.metadata import version
from typing import Any

import click

from feast.cli.output import StructuredError, invocation

READ_COMMANDS = {
    f"{resource} {action}"
    for resource in (
        "entities",
        "feature-views",
        "feature-services",
        "data-sources",
        "features",
    )
    for action in ("list", "describe")
}
MORE_READ_COMMANDS = {
    f"{resource} {action}"
    for resource in (
        "projects",
        "label-views",
        "stream-feature-views",
        "on-demand-feature-views",
        "saved-datasets",
        "validation-references",
    )
    for action in ("list", "describe")
}
SUPPORTED_COMMANDS = (
    READ_COMMANDS
    | MORE_READ_COMMANDS
    | {
        "commands",
        "version",
        "plan",
        "apply",
        "materialize",
        "materialize-incremental",
        "feature-views list-versions",
        "projects current_project",
        "projects delete",
        "feature-views enable",
        "feature-views disable",
        "feature-views set-state",
        "get-online-features",
        "get-historical-features",
        "permissions list",
        "permissions describe",
        "permissions check",
        "permissions list-roles",
        "dbt list",
        "configuration",
        "registry create-schema",
        "delete",
        "teardown",
        "monitor run",
    }
)
MANAGED_COMMANDS = {
    "materialize",
    "materialize-incremental",
    "projects delete",
    "feature-views enable",
    "feature-views disable",
    "feature-views set-state",
}
DESTRUCTIVE_COMMANDS = {"delete", "teardown", "projects delete"}
WRITE_COMMANDS = DESTRUCTIVE_COMMANDS | {
    "apply",
    "materialize",
    "materialize-incremental",
    "monitor run",
    "validate",
    "feature-views enable",
    "feature-views disable",
    "feature-views set-state",
    "registry create-schema",
    "dbt import",
    "init",
    "demo-notebooks",
    "mlflow sync-dataset",
}
SERVER_COMMANDS = {
    "serve",
    "serve_registry",
    "serve_offline",
    "serve_transformations",
    "serve_lineage",
    "listen",
    "ui",
}
OTHER_READ_COMMANDS = {
    "configuration",
    "endpoint",
    "registry-dump",
    "get-online-features",
    "get-historical-features",
    "feature-views list-versions",
    "dbt list",
    "permissions list",
    "permissions describe",
    "permissions check",
    "permissions list-roles",
    "projects list",
    "projects describe",
    "projects current_project",
    "mlflow preview-dataset",
    "mlflow validate-source",
    "mlflow list-sources",
} | {
    f"{resource} {action}"
    for resource in (
        "label-views",
        "stream-feature-views",
        "on-demand-feature-views",
        "saved-datasets",
        "validation-references",
    )
    for action in ("list", "describe")
}


def capabilities(path: str) -> dict[str, Any]:
    effect = "unknown"
    if path == "dbt list":
        effect = "preview"  # Reads a user-supplied manifest, not the Feast registry.
    elif path in READ_COMMANDS | MORE_READ_COMMANDS | OTHER_READ_COMMANDS | {
        "commands",
        "version",
    }:
        effect = "read"
    elif path == "plan":
        effect = "preview"
    elif path in WRITE_COMMANDS:
        effect = "write"
    elif path in SERVER_COMMANDS:
        effect = "server"
    return {
        "structured_output": path in SUPPORTED_COMMANDS,
        "output_formats": ["json", "yaml"] if path in SUPPORTED_COMMANDS else [],
        "effect": effect,
        "may_delete_resources": path in DESTRUCTIVE_COMMANDS | {"apply", "dbt import"},
        "requires_repository": path not in {"commands", "version", "init", "dbt list"},
        "confirmation": "--yes"
        if path in {"projects delete", "delete", "teardown"}
        else ("template-dependent" if path == "init" else "none"),
        "retry_semantics": "read-only" if effect == "read" else "inspect-before-retry",
        "executes_repository_python": path in {"plan", "apply"},
        "structured_constraints": (
            {"provider": "local", "online_store": "sqlite", "auto_baseline": False}
            if path in {"plan", "apply"}
            else {
                "provider": "local",
                "online_store": "sqlite",
                "synchronous": True,
                "batch_engine": False,
                "on_demand_views": False,
            }
            if path in {"materialize", "materialize-incremental"}
            else {
                "source_type": ["log", "batch"],
                "single_named_target_required": True,
                "start_and_end_date_required": True,
                "set_baseline": False,
            }
            if path == "monitor run"
            else None
        ),
    }


def parameter_info(param: click.Parameter) -> dict[str, Any]:
    default = param.default
    if callable(default) or not isinstance(
        default, (str, int, float, bool, list, tuple, type(None))
    ):
        default = None
    return {
        "name": param.name,
        "kind": "option" if isinstance(param, click.Option) else "argument",
        "options": list(param.opts) + list(param.secondary_opts),
        "type": param.type.name,
        "choices": list(param.type.choices)
        if isinstance(param.type, click.Choice)
        else None,
        "required": param.required,
        "default": default,
        "nargs": param.nargs,
        "multiple": getattr(param, "multiple", False),
        "is_flag": getattr(param, "is_flag", False),
        "envvar": param.envvar,
        "help": getattr(param, "help", None),
    }


def command_inventory(root: click.Group) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []

    def visit(group: click.Group, prefix: str = "") -> None:
        for name, command in sorted(group.commands.items()):
            path = f"{prefix} {name}".strip()
            is_group = isinstance(command, click.Group)
            commands.append(
                {
                    "command": f"feast {path}",
                    "group": is_group,
                    "help": command.help or "",
                    "parameters": [parameter_info(param) for param in command.params],
                    "capabilities": None if is_group else capabilities(path),
                }
            )
            if isinstance(command, click.Group):
                visit(command, path)

    visit(root)
    return {
        "global_parameters": [parameter_info(param) for param in root.params],
        "commands": commands,
    }


def install_dispatch(root: click.Group) -> None:
    """Wrap leaf callbacks once; parsing and legacy callbacks remain Click-owned."""

    def visit(group: click.Group, prefix: str = "") -> None:
        for name, command in group.commands.items():
            setattr(command, "get_help", wrap_help(command.get_help))
            path = f"{prefix} {name}".strip()
            if isinstance(command, click.Group):
                visit(command, path)
            elif command.callback is not None:
                command.callback = wrap(command.callback, path)

    def wrap_help(get_help: Any) -> Any:
        def help_text(ctx: click.Context) -> str:
            text: str = get_help(ctx)
            state = invocation.get()
            if state is not None:
                state.help_text = text
            return text

        return help_text

    def wrap(callback: Any, path: str) -> Any:
        @wraps(callback)
        def dispatch(*args: Any, **kwargs: Any) -> Any:
            state = invocation.get()
            if state is None:
                return callback(*args, **kwargs)
            state.command = f"feast {path}"
            if path not in SUPPORTED_COMMANDS:
                raise StructuredError(
                    "UNSUPPORTED_OUTPUT",
                    "This command does not support structured output yet.",
                    "Run feast commands to inspect supported commands, or omit the root --output option.",
                    exit_code=2,
                )
            if path in READ_COMMANDS | MORE_READ_COMMANDS | {
                "projects current_project",
                "feature-views list-versions",
            }:
                from feast.cli.structured_read import read_command

                state.data = read_command(path, click.get_current_context(), **kwargs)
            elif path in {"dbt list", "configuration"}:
                from feast.cli.structured_misc import inspect

                state.data = inspect(path, click.get_current_context(), kwargs)
            elif path == "registry create-schema":
                from feast.cli.structured_misc import create_registry_schema

                state.data = create_registry_schema(click.get_current_context())
            elif path in {"get-online-features", "get-historical-features"}:
                from feast.cli.structured_retrieval import retrieve

                state.data = retrieve(path, click.get_current_context(), **kwargs)
            elif path.startswith("permissions "):
                from feast.cli.structured_permissions import inspect_permissions

                state.data = inspect_permissions(
                    path, click.get_current_context(), **kwargs
                )
            elif path == "monitor run":
                from feast.cli.structured_monitor import monitor_single_target

                state.data = monitor_single_target(
                    click.get_current_context(), **kwargs
                )
            elif path in {
                "delete",
                "teardown",
                "projects delete",
                "feature-views enable",
                "feature-views disable",
                "feature-views set-state",
            }:
                from feast.cli.structured_mutation import mutate

                state.data = mutate(path, click.get_current_context(), **kwargs)
            elif path == "commands":
                state.data = command_inventory(root)
            elif path == "version":
                state.data = {"version": version("feast")}
            elif path in MANAGED_COMMANDS or path in {"plan", "apply"}:
                callback(*args, **kwargs)
            else:
                raise StructuredError(
                    "UNSUPPORTED_OUTPUT",
                    "No structured handler is registered.",
                    "Use feast commands to inspect supported commands.",
                    exit_code=2,
                )
            return state.data

        return dispatch

    visit(root)

"""Small structured adapters for safe inspection and explicit schema creation."""

from __future__ import annotations

from typing import Any

import click

from feast.cli.output import StructuredError, invocation
from feast.repo_operations import cli_check_repo


def inspect(path: str, ctx: click.Context, params: dict[str, Any]) -> dict[str, Any]:
    if path == "configuration":
        from feast.repo_config import load_repo_config

        repo = ctx.obj["CHDIR"]
        fs_yaml_file = ctx.obj["FS_YAML_FILE"]
        cli_check_repo(repo, fs_yaml_file)
        config = load_repo_config(repo, fs_yaml_file)
        return {
            "project": config.project,
            "provider": config.provider,
            "registry_type": config.registry.registry_type,
            "online_store_type": getattr(config.online_store, "type", None),
            "offline_store_type": getattr(config.offline_store, "type", None),
        }
    from feast.dbt.parser import DbtManifestParser

    try:
        parser = DbtManifestParser(params["manifest_path"])
        parser.parse()
    except (FileNotFoundError, ImportError, ValueError) as e:
        raise StructuredError(
            "UNSUPPORTED_CAPABILITY",
            "The dbt manifest could not be read.",
            "Check the manifest path and install feast[dbt] if required.",
        ) from e
    models = parser.get_models(tag_filter=params["tag_filter"])
    return {
        "items": sorted(
            [
                {
                    "name": model.name,
                    "columns": sorted(
                        [
                            {"name": column.name, "dtype": column.data_type}
                            for column in model.columns
                        ],
                        key=lambda item: item["name"],
                    )
                    if params["show_columns"]
                    else None,
                }
                for model in models
            ],
            key=lambda item: item["name"],
        )
    }


def create_registry_schema(ctx: click.Context) -> dict[str, Any]:
    """Reuse the same SQL metadata and connection options as the legacy command."""
    from sqlalchemy import create_engine

    from feast.infra.registry.sql import SqlRegistryConfig, metadata
    from feast.repo_config import load_repo_config

    state = invocation.get()
    assert state is not None
    repo = ctx.obj["CHDIR"]
    fs_yaml_file = ctx.obj["FS_YAML_FILE"]
    cli_check_repo(repo, fs_yaml_file)
    config = load_repo_config(repo, fs_yaml_file)
    registry_config = config.registry
    if not isinstance(registry_config, SqlRegistryConfig):
        raise StructuredError(
            "UNSUPPORTED_CAPABILITY",
            "The configured registry is not SQL based.",
            "Use a SQL-based registry or omit the root --output option.",
        )
    state.operation_started = True
    engine = create_engine(
        registry_config.path, **registry_config.sqlalchemy_config_kwargs
    )
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()
    return {"action": "create-schema", "status": "completed"}

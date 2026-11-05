from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
from click.testing import CliRunner

from feast.cli.cli import cli
from feast.materialization_report import MaterializationReport


def invoke(*args: str) -> tuple[int, dict]:
    result = CliRunner().invoke(cli, ["--output", "json", *args])
    return result.exit_code, json.loads(result.output)


def test_materialization_report_records_only_checkpointed_views() -> None:
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    report = MaterializationReport("test", False, now)
    assert report.result()["completed_views"] == []
    report.begin()
    report.completed("driver_stats", now)
    assert report.result()["completed_views"] == [
        {
            "name": "driver_stats",
            "status": "completed",
            "start_ts": now.isoformat(),
            "end_ts": now.isoformat(),
        }
    ]


def test_report_explicitly_preserves_effective_utc_interval() -> None:
    from feast import utils

    naive_start = datetime(2026, 9, 28)
    naive_end = datetime(2026, 9, 29)
    report = MaterializationReport("test", False, naive_end, start_date=naive_start)
    report.start_date = utils.make_tzaware(naive_start)
    report.end_date = utils.make_tzaware(naive_end)
    report.begin()
    report.completed("driver_stats", report.start_date)
    assert (
        report.result()["requested_start_ts"]
        == report.result()["completed_views"][0]["start_ts"]
    )
    assert report.result()["end_ts"] == report.result()["completed_views"][0]["end_ts"]


def test_materialize_success_and_jsonl(tmp_path: Path) -> None:
    store = Mock()
    store.project = "test"
    store.config.provider = "local"
    store.config.online_store.type = "sqlite"

    def complete(*args: object, **kwargs: object) -> None:
        report = kwargs["report"]
        report.begin()
        report.completed("driver_stats", kwargs["start_date"])

    store.materialize.side_effect = complete
    progress = tmp_path / "progress.jsonl"
    with patch("feast.cli.cli.create_feature_store", return_value=store):
        code, body = invoke(
            "materialize",
            "2026-09-28T00:00:00",
            "2026-09-29T00:00:00",
            "--views",
            "driver_stats",
            "--progress-file",
            str(progress),
        )
    assert code == 0, body
    assert body["data"]["completed_views"][0]["name"] == "driver_stats"
    assert [
        event["event"] for event in map(json.loads, progress.read_text().splitlines())
    ] == [
        "started",
        "view_completed",
        "completed",
    ]


def test_materialize_partial_failure_does_not_claim_completion(tmp_path: Path) -> None:
    store = Mock()
    store.project = "test"
    store.config.provider = "local"
    store.config.online_store.type = "sqlite"

    def fail(*args: object, **kwargs: object) -> None:
        report = kwargs["report"]
        report.begin()
        report.completed("completed", kwargs["start_date"])
        raise RuntimeError("password=secret")

    store.materialize.side_effect = fail
    progress = tmp_path / "partial.jsonl"
    with patch("feast.cli.cli.create_feature_store", return_value=store):
        code, body = invoke(
            "materialize",
            "2026-09-28T00:00:00",
            "2026-09-29T00:00:00",
            "--progress-file",
            str(progress),
        )
    assert code == 1
    assert body["data"]["remaining_outcome"] == "unknown"
    assert [view["name"] for view in body["data"]["completed_views"]] == ["completed"]
    assert body["error"]["retry_safe"] is False
    assert "secret" not in json.dumps(body)
    assert json.loads(progress.read_text().splitlines()[-1])["event"] == "failed"


def test_materialize_rejects_unsupported_before_writes() -> None:
    store = Mock()
    store.config.provider = "remote"
    store.config.online_store.type = "sqlite"
    with patch("feast.cli.cli.create_feature_store", return_value=store):
        code, body = invoke("materialize", "2026-09-28T00:00:00", "2026-09-29T00:00:00")
    assert code == 1
    assert body["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    store.materialize.assert_not_called()


def test_progress_flag_only_in_machine_mode(tmp_path: Path) -> None:
    progress = tmp_path / "progress.jsonl"
    result = CliRunner().invoke(cli, ["materialize", "--progress-file", str(progress)])
    assert result.exit_code == 2
    assert not progress.exists()


def test_progress_file_must_be_new(tmp_path: Path) -> None:
    progress = tmp_path / "progress.jsonl"
    progress.write_text("existing")
    store = Mock()
    store.config.provider = "local"
    store.config.online_store.type = "sqlite"
    store.project = "test"
    with patch("feast.cli.cli.create_feature_store", return_value=store):
        code, body = invoke(
            "materialize",
            "2026-09-28T00:00:00",
            "2026-09-29T00:00:00",
            "--progress-file",
            str(progress),
        )
    assert code == 2
    assert body["error"]["code"] == "INVALID_ARGUMENT"
    assert progress.read_text() == "existing"
    store.materialize.assert_not_called()


def test_new_read_commands_are_structured() -> None:
    store = Mock()
    store.list_projects.return_value = [SimpleNamespace(name="test", description="")]
    with patch("feast.cli.structured_read.create_feature_store", return_value=store):
        code, body = invoke("projects", "list")
    assert code == 0, body
    assert body["data"]["items"][0]["name"] == "test"


def test_project_delete_requires_yes_before_access() -> None:
    with patch("feast.cli.structured_mutation.create_feature_store") as create:
        code, body = invoke("projects", "delete", "test")
    assert code == 2
    assert body["error"]["code"] == "CONFIRMATION_REQUIRED"
    create.assert_not_called()


def test_project_delete_confirmation_is_not_silent() -> None:
    store = Mock()
    with patch(
        "feast.cli.structured_mutation.create_feature_store", return_value=store
    ):
        code, body = invoke("projects", "delete", "test", "--yes")
    assert code == 0, body
    store.delete_project.assert_called_once_with("test")
    assert body["data"]["action"] == "deleted"


def test_generic_delete_requires_confirmation_and_type_before_store() -> None:
    with patch("feast.cli.structured_mutation.create_feature_store") as create:
        code, body = invoke("delete", "driver", "--type", "entity")
        assert code == 2
        assert body["error"]["code"] == "CONFIRMATION_REQUIRED"
        code, body = invoke("delete", "driver", "--yes")
        assert code == 2
        assert body["error"]["code"] == "INVALID_ARGUMENT"
    create.assert_not_called()


def test_generic_delete_targets_only_requested_type() -> None:
    store = Mock()
    entity = SimpleNamespace(name="driver")
    store.get_entity.return_value = entity
    with patch(
        "feast.cli.structured_mutation.create_feature_store", return_value=store
    ):
        code, body = invoke("delete", "driver", "--type", "entity", "--yes")
    assert code == 0, body
    store.get_entity.assert_called_once_with("driver")
    store.apply.assert_called_once_with(
        [entity], objects_to_delete=[entity], partial=False
    )
    assert body["data"] == {"action": "deleted", "type": "entity", "name": "driver"}


def test_teardown_requires_yes() -> None:
    with patch("feast.cli.structured_mutation.create_feature_store") as create:
        code, body = invoke("teardown")
    assert code == 2
    assert body["error"]["code"] == "CONFIRMATION_REQUIRED"
    create.assert_not_called()


def test_state_transition_rejection_is_nonzero() -> None:
    from feast.feature_view import FeatureViewState

    store = Mock()
    view = Mock()
    view.name = "driver"
    view.state = FeatureViewState.CREATED
    store.registry.get_any_feature_view.return_value = view
    # A real FV subtype is required for the mutation path.
    from feast import Entity, FeatureView, Field, FileSource
    from feast.types import Float32
    from feast.value_type import ValueType

    fv = FeatureView(
        name="driver",
        entities=[Entity(name="driver_id", value_type=ValueType.INT64)],
        ttl=None,
        schema=[Field(name="rate", dtype=Float32)],
        source=FileSource(
            name="src", path="file.parquet", timestamp_field="event_timestamp"
        ),
    )
    fv.state = FeatureViewState.CREATED
    store.registry.get_any_feature_view.return_value = fv
    with patch(
        "feast.cli.structured_mutation.create_feature_store", return_value=store
    ):
        code, body = invoke("feature-views", "set-state", "driver", "AVAILABLE_ONLINE")
    assert code == 2, body
    store.registry.apply_feature_view.assert_not_called()


def test_bounded_online_retrieval() -> None:
    store = Mock()
    store.get_online_features.return_value.to_dict.return_value = {"conv_rate": [0.3]}
    with patch(
        "feast.cli.structured_retrieval.create_feature_store", return_value=store
    ):
        code, body = invoke(
            "get-online-features", "-e", "driver_id=1", "-f", "stats:conv_rate"
        )
    assert code == 0, body
    assert body["data"]["features"]["conv_rate"] == [0.3]


def test_online_output_byte_limit_prevents_unbounded_agent_context() -> None:
    store = Mock()
    store.get_online_features.return_value.to_dict.return_value = {
        "conv_rate": ["x" * 1_000_100]
    }
    with patch(
        "feast.cli.structured_retrieval.create_feature_store", return_value=store
    ):
        code, body = invoke(
            "get-online-features", "-e", "driver_id=1", "-f", "stats:conv_rate"
        )
    assert code == 1
    assert body["error"]["code"] == "RESULT_TOO_LARGE"
    assert "xxx" not in json.dumps(body)


def test_bounded_historical_retrieval() -> None:
    store = Mock()
    store.get_historical_features.return_value.to_df.return_value = pd.DataFrame(
        {"id": [1]}
    )
    with patch(
        "feast.cli.structured_retrieval.create_feature_store", return_value=store
    ):
        code, body = invoke(
            "get-historical-features",
            "-d",
            '[{"id": 1, "event_timestamp": "2026-09-28T00:00:00"}]',
            "-f",
            "stats:rate",
        )
    assert code == 0, body
    assert body["data"]["items"] == [{"id": 1}]


def test_historical_limit_does_not_serialize_unbounded_data() -> None:
    store = Mock()
    store.get_historical_features.return_value.to_df.return_value = pd.DataFrame(
        {"id": range(101)}
    )
    with patch(
        "feast.cli.structured_retrieval.create_feature_store", return_value=store
    ):
        code, body = invoke(
            "get-historical-features",
            "-d",
            '[{"id": 1, "event_timestamp": "2026-09-28T00:00:00"}]',
            "-f",
            "stats:rate",
        )
    assert code == 1
    assert body["error"]["code"] == "RESULT_TOO_LARGE"


def test_historical_date_range_rejected_before_query() -> None:
    with patch("feast.cli.structured_retrieval.create_feature_store") as create:
        code, body = invoke(
            "get-historical-features", "-s", "2026-09-28 00:00:00", "-f", "stats:rate"
        )
    assert code == 2
    assert body["error"]["code"] == "INVALID_ARGUMENT"
    create.assert_not_called()


def test_historical_input_over_limit_rejected_before_query() -> None:
    data = json.dumps(
        [{"id": i, "event_timestamp": "2026-09-28T00:00:00"} for i in range(101)]
    )
    with patch("feast.cli.structured_retrieval.create_feature_store") as create:
        code, body = invoke("get-historical-features", "-d", data, "-f", "stats:rate")
    assert code == 2
    assert body["error"]["code"] == "INVALID_ARGUMENT"
    create.assert_not_called()


def test_large_historical_input_rejected_before_query() -> None:
    data = json.dumps(
        [{"id": "x" * 1_000_001, "event_timestamp": "2026-09-28T00:00:00"}]
    )
    with patch("feast.cli.structured_retrieval.create_feature_store") as create:
        code, body = invoke("get-historical-features", "-d", data, "-f", "stats:rate")
    assert code == 2
    assert body["error"]["code"] == "INVALID_ARGUMENT"
    create.assert_not_called()


def test_large_online_input_rejected_before_query() -> None:
    with patch("feast.cli.structured_retrieval.create_feature_store") as create:
        code, body = invoke(
            "get-online-features",
            "-e",
            "driver_id=" + "x" * 1_000_001,
            "-f",
            "stats:rate",
        )
    assert code == 2
    assert body["error"]["code"] == "INVALID_ARGUMENT"
    create.assert_not_called()


def test_capabilities_do_not_advertise_unsupported_commands() -> None:
    code, body = invoke("commands")
    assert code == 0, body
    entries = {item["command"]: item for item in body["data"]["commands"]}
    assert entries["feast materialize"]["capabilities"]["structured_output"]
    assert not entries["feast serve"]["capabilities"]["structured_output"]
    assert not entries["feast mlflow sync-dataset"]["capabilities"]["structured_output"]


def test_permission_summary_does_not_expose_subjects_or_tag_values() -> None:
    from feast.permissions.policy import RoleBasedPolicy

    permission = Mock()
    permission.name = "read_view"
    permission.types = [type("FeatureView", (), {})]
    permission.actions = [SimpleNamespace(value="read")]
    permission.policy = RoleBasedPolicy(roles=["secret-role"])
    store = Mock()
    store.list_permissions.return_value = [permission]
    with patch(
        "feast.cli.structured_permissions.create_feature_store", return_value=store
    ):
        code, body = invoke("permissions", "list")
    assert code == 0, body
    assert body["data"]["items"][0]["policy_type"] == "RoleBasedPolicy"
    assert "secret-role" not in json.dumps(body)


def test_permission_roles_explicitly_requested() -> None:
    from feast.permissions.policy import RoleBasedPolicy

    store = Mock()
    store.list_permissions.return_value = [
        SimpleNamespace(policy=RoleBasedPolicy(["data-reader"]))
    ]
    with patch(
        "feast.cli.structured_permissions.create_feature_store", return_value=store
    ):
        code, body = invoke("permissions", "list-roles")
    assert code == 0, body
    assert body["data"]["items"] == ["data-reader"]


def test_dbt_manifest_listing_uses_minimized_fields() -> None:
    parser = Mock()
    parser.get_models.return_value = [
        SimpleNamespace(
            name="driver_stats",
            columns=[SimpleNamespace(name="rate", data_type="FLOAT")],
            meta={"secret": "must-not-leak"},
        )
    ]
    with patch("feast.dbt.parser.DbtManifestParser", return_value=parser):
        code, body = invoke("dbt", "list", "-m", __file__, "--show-columns")
    assert code == 0, body
    assert body["data"]["items"] == [
        {"name": "driver_stats", "columns": [{"name": "rate", "dtype": "FLOAT"}]}
    ]
    assert "must-not-leak" not in json.dumps(body)


def test_endpoint_remains_unsupported_without_redaction() -> None:
    code, body = invoke("endpoint")
    assert code == 2
    assert body["error"]["code"] == "UNSUPPORTED_OUTPUT"


def test_registry_create_schema_rejects_file_backend_before_ddl() -> None:
    with (
        patch("feast.cli.structured_misc.cli_check_repo"),
        patch("feast.repo_config.load_repo_config", return_value=Mock(registry=Mock())),
        patch("sqlalchemy.create_engine") as engine,
    ):
        code, body = invoke("registry", "create-schema")
    assert code == 1, body
    assert body["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    engine.assert_not_called()


def test_configuration_only_exposes_allowlisted_types() -> None:
    config = Mock()
    config.project = "test"
    config.provider = "local"
    config.registry.registry_type = "file"
    config.online_store.type = "sqlite"
    config.offline_store.type = "file"
    config.password = "password=secret"
    with (
        patch("feast.cli.structured_misc.cli_check_repo"),
        patch("feast.repo_config.load_repo_config", return_value=config),
    ):
        code, body = invoke("configuration")
    assert code == 0, body
    assert body["data"] == {
        "project": "test",
        "provider": "local",
        "registry_type": "file",
        "online_store_type": "sqlite",
        "offline_store_type": "file",
    }
    assert "secret" not in json.dumps(body)


def test_documented_agent_workflow_commands_are_discoverable() -> None:
    from feast.cli.discovery import command_inventory

    guide = (
        Path(__file__).resolve().parents[5]
        / "docs/how-to-guides/agent-cli-workflows.md"
    ).read_text()
    commands = {
        item["command"]
        for item in command_inventory(cli)["commands"]
        if not item["group"] and item["capabilities"]["structured_output"]
    }
    for command in (
        "feast commands",
        "feast projects current_project",
        "feast feature-views list",
        "feast feature-views describe",
        "feast plan",
        "feast apply",
        "feast materialize",
    ):
        assert command in commands
        assert command.split(" ", 1)[1] in guide

    root = Path(__file__).resolve().parents[5]
    for skill in (
        root / "skills/SKILL.md",
        root / "skills/feast-user-guide/SKILL.md",
    ):
        text = skill.read_text()
        assert "feast --output json commands" in text
        assert "obtain approval" in text
        guide_link = (
            "../../docs/how-to-guides/agent-cli-workflows.md"
            if skill.parent.name == "feast-user-guide"
            else "../docs/how-to-guides/agent-cli-workflows.md"
        )
        assert (skill.parent / guide_link).exists()


def test_monitor_structured_rejects_batch_before_store_creation() -> None:
    with patch("feast.cli.structured_monitor.create_feature_store") as create:
        code, body = invoke("monitor", "run")
    assert code == 1
    assert body["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    create.assert_not_called()


def test_monitor_structured_requires_single_log_service_and_dates() -> None:
    with patch("feast.cli.structured_monitor.create_feature_store") as create:
        code, body = invoke(
            "monitor",
            "run",
            "--source-type",
            "log",
            "--start-date",
            "2026-09-01",
            "--end-date",
            "2026-09-02",
        )
    assert code == 1
    assert body["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    create.assert_not_called()


def test_monitor_structured_invalid_dates_do_not_start() -> None:
    with patch("feast.cli.structured_monitor.create_feature_store") as create:
        code, body = invoke(
            "monitor",
            "run",
            "--source-type",
            "log",
            "--feature-service",
            "stats",
            "--start-date",
            "bad",
            "--end-date",
            "2026-09-02",
        )
    assert code == 2
    assert body["error"]["code"] == "INVALID_ARGUMENT"
    create.assert_not_called()


def test_monitor_structured_reports_single_log_result() -> None:
    store = Mock(project="test")
    service = Mock()
    service.compute_log_metrics.return_value = {
        "status": "completed",
        "granularity": "daily",
        "computed_features": 2,
        "metric_date": "2026-09-01",
        "duration_ms": 31,
    }
    with (
        patch("feast.cli.structured_monitor.create_feature_store", return_value=store),
        patch(
            "feast.monitoring.monitoring_service.MonitoringService",
            return_value=service,
        ),
    ):
        code, body = invoke(
            "monitor",
            "run",
            "--source-type",
            "log",
            "--feature-service",
            "stats",
            "--start-date",
            "2026-09-01",
            "--end-date",
            "2026-09-02",
        )
    assert code == 0, body
    assert body["data"]["computed_features"] == 2
    assert body["data"]["status"] == "completed"
    service.compute_log_metrics.assert_called_once()


def test_monitor_structured_never_claims_skipped_as_completed() -> None:
    store = Mock(project="test")
    service = Mock()
    service.compute_log_metrics.return_value = {
        "status": "skipped",
        "reason": "password=private",
        "duration_ms": 5,
    }
    with (
        patch("feast.cli.structured_monitor.create_feature_store", return_value=store),
        patch(
            "feast.monitoring.monitoring_service.MonitoringService",
            return_value=service,
        ),
    ):
        code, body = invoke(
            "monitor",
            "run",
            "--source-type",
            "log",
            "--feature-service",
            "stats",
            "--start-date",
            "2026-09-01",
            "--end-date",
            "2026-09-02",
        )
    assert code == 0, body
    assert body["data"]["status"] == "skipped"
    assert "private" not in json.dumps(body)


def test_monitor_structured_failure_is_not_safe_to_retry() -> None:
    store = Mock(project="test")
    service = Mock()
    service.compute_log_metrics.side_effect = RuntimeError("password=private")
    with (
        patch("feast.cli.structured_monitor.create_feature_store", return_value=store),
        patch(
            "feast.monitoring.monitoring_service.MonitoringService",
            return_value=service,
        ),
    ):
        code, body = invoke(
            "monitor",
            "run",
            "--source-type",
            "log",
            "--feature-service",
            "stats",
            "--start-date",
            "2026-09-01",
            "--end-date",
            "2026-09-02",
        )
    assert code == 1
    assert body["error"]["retry_safe"] is False
    assert "private" not in json.dumps(body)


def test_monitor_structured_batch_requires_named_view_before_store() -> None:
    with patch("feast.cli.structured_monitor.create_feature_store") as create:
        code, body = invoke(
            "monitor",
            "run",
            "--source-type",
            "batch",
            "--start-date",
            "2026-09-01",
            "--end-date",
            "2026-09-02",
        )
    assert code == 1
    assert body["error"]["code"] == "UNSUPPORTED_CAPABILITY"
    create.assert_not_called()


def test_monitor_structured_batch_uses_strict_service_mode() -> None:
    store = Mock(project="test")
    service = Mock()
    service.compute_metrics.return_value = {
        "status": "completed",
        "granularity": "daily",
        "computed_features": 2,
        "computed_feature_views": 1,
        "computed_feature_services": 1,
        "metric_dates": ["2026-09-01"],
        "duration_ms": 31,
    }
    with (
        patch("feast.cli.structured_monitor.create_feature_store", return_value=store),
        patch(
            "feast.monitoring.monitoring_service.MonitoringService",
            return_value=service,
        ),
    ):
        code, body = invoke(
            "monitor",
            "run",
            "--source-type",
            "batch",
            "--feature-view",
            "driver_stats",
            "--start-date",
            "2026-09-01",
            "--end-date",
            "2026-09-02",
        )
    assert code == 0, body
    assert body["data"]["computed_feature_views"] == 1
    assert body["data"]["target"] == "driver_stats"
    assert service.compute_metrics.call_args.kwargs["raise_on_error"] is True


def test_monitor_structured_batch_failure_never_claims_completion() -> None:
    store = Mock(project="test")
    service = Mock()
    service.compute_metrics.side_effect = RuntimeError("credential=secret")
    with (
        patch("feast.cli.structured_monitor.create_feature_store", return_value=store),
        patch(
            "feast.monitoring.monitoring_service.MonitoringService",
            return_value=service,
        ),
    ):
        code, body = invoke(
            "monitor",
            "run",
            "--source-type",
            "batch",
            "--feature-view",
            "driver_stats",
            "--start-date",
            "2026-09-01",
            "--end-date",
            "2026-09-02",
        )
    assert code == 1
    assert body["status"] == "error"
    assert body["data"]["remaining_outcome"] == "unknown"
    assert body["error"]["retry_safe"] is False
    assert "secret" not in json.dumps(body)


def test_legacy_monitor_missing_log_service_exits_with_usage_error() -> None:
    store = Mock(project="test")
    with patch("feast.cli.monitor.create_feature_store", return_value=store):
        result = CliRunner().invoke(
            cli,
            ["monitor", "run", "--source-type", "log", "--start-date", "2026-09-01"],
        )
    assert result.exit_code == 2
    assert "--feature-service is required" in result.output


def test_legacy_feature_view_invalid_transition_exits_nonzero() -> None:
    from feast import Entity, FeatureView, Field, FileSource
    from feast.feature_view import FeatureViewState
    from feast.types import Float32
    from feast.value_type import ValueType

    fv = FeatureView(
        name="driver",
        entities=[Entity(name="driver_id", value_type=ValueType.INT64)],
        ttl=None,
        schema=[Field(name="rate", dtype=Float32)],
        source=FileSource(
            name="src", path="file.parquet", timestamp_field="event_timestamp"
        ),
    )
    fv.state = FeatureViewState.CREATED
    store = Mock(project="test")
    store.registry.get_any_feature_view.return_value = fv
    with patch("feast.cli.feature_views.create_feature_store", return_value=store):
        result = CliRunner().invoke(
            cli, ["feature-views", "set-state", "driver", "AVAILABLE_ONLINE"]
        )
    assert result.exit_code == 2
    assert "Invalid state transition" in result.output
    store.registry.apply_feature_view.assert_not_called()

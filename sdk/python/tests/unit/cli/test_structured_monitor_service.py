"""The structured CLI must not mistake swallowed monitoring failures for success."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from feast.monitoring.monitoring_service import MonitoringService


@pytest.fixture
def service() -> MonitoringService:
    store = MagicMock()
    store.config.project = "test"
    svc = MonitoringService(store)
    svc._ensure_monitoring_tables = MagicMock()
    svc._resolve_feature_views = MagicMock(
        return_value=[SimpleNamespace(name="driver_stats")]
    )
    svc._get_max_timestamp = MagicMock(return_value=None)
    svc._compute_for_feature_view = MagicMock(
        side_effect=RuntimeError("secret provider failure")
    )
    svc._compute_feature_service_metrics = MagicMock(return_value=0)
    return svc


def test_batch_strict_mode_propagates_feature_view_failure(
    service: MonitoringService,
) -> None:
    with pytest.raises(RuntimeError, match="secret provider failure"):
        service.compute_metrics(
            "test",
            feature_view_name="driver_stats",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 2),
            raise_on_error=True,
        )
    service._compute_feature_service_metrics.assert_not_called()


def test_batch_default_keeps_legacy_best_effort(service: MonitoringService) -> None:
    result = service.compute_metrics(
        "test",
        feature_view_name="driver_stats",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 2),
    )
    assert result["status"] == "completed"
    assert result["computed_feature_views"] == 0
    assert (
        service._compute_feature_service_metrics.call_args.kwargs["raise_on_error"]
        is False
    )


def test_batch_strict_mode_propagates_aggregate_failure(
    service: MonitoringService,
) -> None:
    service._compute_for_feature_view = MagicMock(
        return_value={"feature_count": 1, "dates": {date(2026, 9, 1)}}
    )
    service._compute_feature_service_metrics = MagicMock(
        side_effect=RuntimeError("aggregate write failed")
    )
    with pytest.raises(RuntimeError, match="aggregate write failed"):
        service.compute_metrics(
            "test",
            feature_view_name="driver_stats",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 2),
            raise_on_error=True,
        )


def test_aggregate_strict_mode_propagates_store_failure(
    service: MonitoringService,
) -> None:
    # The fixture stubs this method for compute_metrics; exercise the real
    # aggregate implementation in the tests below.
    del service.__dict__["_compute_feature_service_metrics"]
    store = service._store
    store.registry.list_feature_services.return_value = [
        SimpleNamespace(
            name="risk_service",
            feature_view_projections=[SimpleNamespace(name="driver_stats")],
        )
    ]
    offline_store = MagicMock()
    offline_store.query_monitoring_metrics.side_effect = RuntimeError("query failed")
    with patch.object(service, "_get_offline_store", return_value=offline_store):
        with pytest.raises(RuntimeError, match="query failed"):
            service._compute_feature_service_metrics(
                "test", "daily", [date(2026, 9, 1)], False, raise_on_error=True
            )


def test_aggregate_default_keeps_legacy_best_effort(service: MonitoringService) -> None:
    del service.__dict__["_compute_feature_service_metrics"]
    store = service._store
    store.registry.list_feature_services.return_value = [
        SimpleNamespace(
            name="risk_service",
            feature_view_projections=[SimpleNamespace(name="driver_stats")],
        )
    ]
    offline_store = MagicMock()
    offline_store.query_monitoring_metrics.side_effect = RuntimeError("query failed")
    with patch.object(service, "_get_offline_store", return_value=offline_store):
        assert (
            service._compute_feature_service_metrics(
                "test", "daily", [date(2026, 9, 1)], False
            )
            == 0
        )

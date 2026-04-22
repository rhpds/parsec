"""Tests for MLflow tracking connection module."""

from unittest.mock import MagicMock, patch


def test_init_mlflow_disabled_when_no_url():
    """When tracking_url is empty, init should skip and client should be None."""
    with patch("src.connections.mlflow_tracking.get_config") as mock_cfg:
        mock_cfg.return_value.get.return_value = {}
        from src.connections import mlflow_tracking

        mlflow_tracking._client = None
        mlflow_tracking.init_mlflow()
        assert mlflow_tracking._client is None


def test_init_mlflow_creates_client_with_url():
    """When tracking_url is set, init should create a client."""
    with patch("src.connections.mlflow_tracking.get_config") as mock_cfg:
        mock_cfg.return_value.get.return_value = {
            "tracking_url": "http://localhost:5000",
            "experiment_name": "test-experiment",
        }
        with patch("src.connections.mlflow_tracking.mlflow") as mock_mlflow:
            mock_mlflow.MlflowClient.return_value = MagicMock()
            from src.connections import mlflow_tracking

            mlflow_tracking._client = None
            mlflow_tracking.init_mlflow()
            assert mlflow_tracking._client is not None
            mock_mlflow.set_tracking_uri.assert_called_once_with("http://localhost:5000")


def test_get_mlflow_client_returns_none_when_disabled():
    """get_mlflow_client returns None when MLflow is not configured."""
    from src.connections import mlflow_tracking

    mlflow_tracking._client = None
    assert mlflow_tracking.get_mlflow_client() is None


def test_get_experiment_name_default():
    """Experiment name defaults to parsec-agent-metrics."""
    from src.connections import mlflow_tracking

    mlflow_tracking._experiment_name = None
    assert mlflow_tracking.get_experiment_name() == "parsec-agent-metrics"


def test_get_experiment_name_configured():
    """Experiment name is read from config."""
    from src.connections import mlflow_tracking

    mlflow_tracking._experiment_name = "custom-experiment"
    assert mlflow_tracking.get_experiment_name() == "custom-experiment"

"""Coverage tests for src/connections/babylon.py.

Covers _load_client_cert, _build_ssl_context, and _parse_kubeconfig.
"""

import ssl
from base64 import b64encode
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.connections.babylon import (
    _build_ssl_context,
    _load_client_cert,
    _parse_kubeconfig,
)

# ===================================================================
# _load_client_cert
# ===================================================================


class TestLoadClientCert:
    def test_loads_cert_and_key(self):
        fake_cert = b"-----BEGIN CERTIFICATE-----\nfake-cert\n-----END CERTIFICATE-----"
        fake_key = b"FAKE-CERT-DATA-FOR-TESTING"
        cert_data = b64encode(fake_cert).decode()
        key_data = b64encode(fake_key).decode()

        mock_ctx = MagicMock(spec=ssl.SSLContext)
        with patch("os.unlink") as mock_unlink:
            _load_client_cert(mock_ctx, cert_data, key_data)

        mock_ctx.load_cert_chain.assert_called_once()
        cert_path, key_path = mock_ctx.load_cert_chain.call_args[0]
        assert cert_path.endswith(".crt")
        assert key_path.endswith(".key")
        # Both temp files should be cleaned up
        assert mock_unlink.call_count == 2

    def test_temp_files_contain_decoded_data(self):
        fake_cert = b"cert-content"
        fake_key = b"key-content"
        cert_data = b64encode(fake_cert).decode()
        key_data = b64encode(fake_key).decode()

        written_cert = None
        written_key = None

        def capture_load(cert_path, key_path):
            nonlocal written_cert, written_key
            with open(cert_path, "rb") as f:
                written_cert = f.read()
            with open(key_path, "rb") as f:
                written_key = f.read()

        mock_ctx = MagicMock(spec=ssl.SSLContext)
        mock_ctx.load_cert_chain.side_effect = capture_load

        _load_client_cert(mock_ctx, cert_data, key_data)
        assert written_cert == fake_cert
        assert written_key == fake_key


# ===================================================================
# _build_ssl_context
# ===================================================================


class TestBuildSslContext:
    def test_no_verify_no_client_cert_returns_false(self):
        cfg = {
            "verify_ssl": False,
            "ca_data": "",
            "client_cert_data": "",
            "client_key_data": "",
        }
        result = _build_ssl_context(cfg)
        assert result is False

    def test_verify_ssl_no_certs_returns_true(self):
        cfg = {
            "verify_ssl": True,
            "ca_data": "",
            "client_cert_data": "",
            "client_key_data": "",
        }
        result = _build_ssl_context(cfg)
        assert result is True

    def test_with_ca_data_returns_ssl_context(self):
        fake_cert = b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----"
        ca_data = b64encode(fake_cert).decode()
        cfg = {
            "verify_ssl": True,
            "ca_data": ca_data,
            "client_cert_data": "",
            "client_key_data": "",
        }
        with patch("src.connections.babylon._load_ca_cert") as mock_load:
            result = _build_ssl_context(cfg)
        assert isinstance(result, ssl.SSLContext)
        mock_load.assert_called_once()

    def test_with_client_cert_returns_ssl_context(self):
        fake_cert = b"cert"
        fake_key = b"key"
        cfg = {
            "verify_ssl": True,
            "ca_data": "",
            "client_cert_data": b64encode(fake_cert).decode(),
            "client_key_data": b64encode(fake_key).decode(),
        }
        with patch("src.connections.babylon._load_client_cert") as mock_load:
            result = _build_ssl_context(cfg)
        assert isinstance(result, ssl.SSLContext)
        mock_load.assert_called_once()

    def test_no_verify_with_client_cert_disables_hostname_check(self):
        fake_cert = b"cert"
        fake_key = b"key"
        cfg = {
            "verify_ssl": False,
            "ca_data": "",
            "client_cert_data": b64encode(fake_cert).decode(),
            "client_key_data": b64encode(fake_key).decode(),
        }
        with patch("src.connections.babylon._load_client_cert"):
            result = _build_ssl_context(cfg)
        assert isinstance(result, ssl.SSLContext)
        assert result.check_hostname is False
        assert result.verify_mode == ssl.CERT_NONE

    def test_with_both_ca_and_client_cert(self):
        fake_ca = b"ca-data"
        fake_cert = b"cert-data"
        fake_key = b"key-data"
        cfg = {
            "verify_ssl": True,
            "ca_data": b64encode(fake_ca).decode(),
            "client_cert_data": b64encode(fake_cert).decode(),
            "client_key_data": b64encode(fake_key).decode(),
        }
        with (
            patch("src.connections.babylon._load_ca_cert") as mock_ca,
            patch("src.connections.babylon._load_client_cert") as mock_client,
        ):
            result = _build_ssl_context(cfg)
        assert isinstance(result, ssl.SSLContext)
        mock_ca.assert_called_once()
        mock_client.assert_called_once()


# ===================================================================
# _parse_kubeconfig
# ===================================================================


class TestParseKubeconfig:
    def test_parses_basic_kubeconfig(self, tmp_path):
        kc = {
            "current-context": "test",
            "contexts": [
                {
                    "name": "test",
                    "context": {
                        "cluster": "test-cluster",
                        "user": "test-user",
                    },
                }
            ],
            "clusters": [
                {
                    "name": "test-cluster",
                    "cluster": {
                        "server": "https://api.example.com:6443/",
                        "insecure-skip-tls-verify": True,
                    },
                }
            ],
            "users": [
                {
                    "name": "test-user",
                    "user": {"token": "test-token-123"},
                }
            ],
        }
        kc_path = tmp_path / "kubeconfig"
        with open(kc_path, "w") as f:
            yaml.dump(kc, f)

        result = _parse_kubeconfig(str(kc_path))
        assert result["server"] == "https://api.example.com:6443"
        assert result["token"] == "test-token-123"
        assert result["verify_ssl"] is False
        assert result["ca_data"] == ""

    def test_raises_on_missing_file(self):
        with pytest.raises(FileNotFoundError):
            _parse_kubeconfig("/nonexistent/kubeconfig")

    def test_raises_on_missing_cluster(self, tmp_path):
        kc = {
            "current-context": "test",
            "contexts": [
                {
                    "name": "test",
                    "context": {
                        "cluster": "missing-cluster",
                        "user": "test-user",
                    },
                }
            ],
            "clusters": [
                {
                    "name": "other-cluster",
                    "cluster": {"server": "https://other.example.com"},
                }
            ],
            "users": [],
        }
        kc_path = tmp_path / "kubeconfig"
        with open(kc_path, "w") as f:
            yaml.dump(kc, f)

        with pytest.raises(ValueError, match="Cluster.*not found"):
            _parse_kubeconfig(str(kc_path))

    def test_parses_with_ca_and_client_cert(self, tmp_path):
        kc = {
            "current-context": "test",
            "contexts": [
                {
                    "name": "test",
                    "context": {"cluster": "c1", "user": "u1"},
                }
            ],
            "clusters": [
                {
                    "name": "c1",
                    "cluster": {
                        "server": "https://api.cluster.com:6443",
                        "certificate-authority-data": "Y2EtZGF0YQ==",
                    },
                }
            ],
            "users": [
                {
                    "name": "u1",
                    "user": {
                        "client-certificate-data": "Y2VydC1kYXRh",
                        "client-key-data": "dGVzdA==",
                    },
                }
            ],
        }
        kc_path = tmp_path / "kubeconfig"
        with open(kc_path, "w") as f:
            yaml.dump(kc, f)

        result = _parse_kubeconfig(str(kc_path))
        assert result["verify_ssl"] is True
        assert result["ca_data"] == "Y2EtZGF0YQ=="
        assert result["client_cert_data"] == "Y2VydC1kYXRh"
        assert result["client_key_data"] == "dGVzdA=="

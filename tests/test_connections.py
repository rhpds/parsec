"""Tests for pure-logic helpers in connection modules."""

import ssl
from base64 import b64encode
from unittest.mock import MagicMock, patch

import pytest

from src.connections.babylon import (
    _load_ca_cert,
    _resolve_context,
    _resolve_user_credentials,
    resolve_cluster_from_comment,
)
from src.connections.reporting_mcp import (
    _build_knowledge_tool,
    _build_prompt_tool,
    _mcp_schema_to_claude,
    get_mcp_tool_original,
    is_mcp_tool,
)

# ---------------------------------------------------------------------------
# Mock tool object for MCP schema conversion
# ---------------------------------------------------------------------------


class MockTool:
    """Minimal stand-in for an MCP Tool object."""

    def __init__(self, name, description, input_schema=None):
        self.name = name
        self.description = description
        self.inputSchema = input_schema


# ---------------------------------------------------------------------------
# _mcp_schema_to_claude
# ---------------------------------------------------------------------------


class TestMcpSchemaToClaudeTool:
    def test_basic_conversion(self):
        tool = MockTool(
            name="list_tables",
            description="List all database tables",
            input_schema={
                "properties": {"schema": {"type": "string"}},
                "required": ["schema"],
            },
        )
        result = _mcp_schema_to_claude(tool)
        assert result["name"] == "db_list_tables"
        assert result["description"] == "List all database tables"
        assert result["input_schema"]["properties"] == {"schema": {"type": "string"}}
        assert result["input_schema"]["required"] == ["schema"]

    def test_no_input_schema(self):
        tool = MockTool(name="ping", description="Health check", input_schema=None)
        result = _mcp_schema_to_claude(tool)
        assert result["name"] == "db_ping"
        assert result["input_schema"]["properties"] == {}
        assert result["input_schema"]["required"] == []

    def test_empty_input_schema(self):
        tool = MockTool(name="noop", description=None, input_schema={})
        result = _mcp_schema_to_claude(tool)
        assert result["name"] == "db_noop"
        assert result["description"] == "MCP tool: noop"
        assert result["input_schema"]["properties"] == {}
        assert result["input_schema"]["required"] == []

    def test_schema_with_multiple_properties(self):
        tool = MockTool(
            name="query",
            description="Run SQL",
            input_schema={
                "properties": {
                    "sql": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["sql"],
            },
        )
        result = _mcp_schema_to_claude(tool)
        assert "sql" in result["input_schema"]["properties"]
        assert "limit" in result["input_schema"]["properties"]
        assert result["input_schema"]["required"] == ["sql"]

    def test_prefix_applied(self):
        tool = MockTool(name="get_schema", description="Get schema")
        result = _mcp_schema_to_claude(tool)
        assert result["name"].startswith("db_")

    def test_none_description_fallback(self):
        tool = MockTool(name="mystery", description=None)
        result = _mcp_schema_to_claude(tool)
        assert result["description"] == "MCP tool: mystery"

    def test_empty_description_fallback(self):
        tool = MockTool(name="mystery", description="")
        result = _mcp_schema_to_claude(tool)
        assert result["description"] == "MCP tool: mystery"


# ---------------------------------------------------------------------------
# _build_knowledge_tool
# ---------------------------------------------------------------------------


class TestBuildKnowledgeTool:
    def test_single_domain(self):
        result = _build_knowledge_tool(["provisioning"])
        assert result["name"] == "db_read_knowledge"
        enum = result["input_schema"]["properties"]["domain"]["enum"]
        assert enum == ["provisioning"]
        assert "domain" in result["input_schema"]["required"]

    def test_multiple_domains(self):
        result = _build_knowledge_tool(["billing", "catalog", "provisioning"])
        enum = result["input_schema"]["properties"]["domain"]["enum"]
        assert enum == ["billing", "catalog", "provisioning"]

    def test_empty_domains(self):
        result = _build_knowledge_tool([])
        enum = result["input_schema"]["properties"]["domain"]["enum"]
        assert enum == []

    def test_description_present(self):
        result = _build_knowledge_tool(["x"])
        assert "knowledge" in result["description"].lower()


# ---------------------------------------------------------------------------
# _build_prompt_tool
# ---------------------------------------------------------------------------


class TestBuildPromptTool:
    def test_basic(self):
        prompts = [
            {"name": "investigate_cost", "description": "Cost investigation"},
            {"name": "audit_user", "description": "User audit"},
        ]
        result = _build_prompt_tool(prompts)
        assert result["name"] == "db_get_prompt"
        enum = result["input_schema"]["properties"]["prompt_name"]["enum"]
        assert enum == ["investigate_cost", "audit_user"]
        assert "prompt_name" in result["input_schema"]["required"]
        assert "investigate_cost: Cost investigation" in result["description"]
        assert "audit_user: User audit" in result["description"]

    def test_missing_description(self):
        prompts = [{"name": "simple"}]
        result = _build_prompt_tool(prompts)
        enum = result["input_schema"]["properties"]["prompt_name"]["enum"]
        assert enum == ["simple"]
        assert "simple: " in result["description"]

    def test_arguments_property_present(self):
        prompts = [{"name": "t1", "description": "d1"}]
        result = _build_prompt_tool(prompts)
        assert "arguments" in result["input_schema"]["properties"]
        assert result["input_schema"]["properties"]["arguments"]["type"] == "object"

    def test_empty_prompts(self):
        result = _build_prompt_tool([])
        enum = result["input_schema"]["properties"]["prompt_name"]["enum"]
        assert enum == []


# ---------------------------------------------------------------------------
# get_mcp_tool_original
# ---------------------------------------------------------------------------


class TestGetMcpToolOriginal:
    def test_strips_prefix(self):
        assert get_mcp_tool_original("db_list_tables") == "list_tables"

    def test_no_prefix(self):
        assert get_mcp_tool_original("investigate_costs") == "investigate_costs"

    def test_only_prefix(self):
        assert get_mcp_tool_original("db_") == ""

    def test_empty_string(self):
        assert get_mcp_tool_original("") == ""

    def test_double_prefix(self):
        assert get_mcp_tool_original("db_db_thing") == "db_thing"


# ---------------------------------------------------------------------------
# is_mcp_tool
# ---------------------------------------------------------------------------


class TestIsMcpTool:
    def test_known_tool(self, monkeypatch):
        monkeypatch.setattr(
            "src.connections.reporting_mcp._mcp_tool_names",
            {"db_list_tables", "db_query"},
        )
        assert is_mcp_tool("db_list_tables") is True
        assert is_mcp_tool("db_query") is True

    def test_unknown_tool(self, monkeypatch):
        monkeypatch.setattr(
            "src.connections.reporting_mcp._mcp_tool_names",
            {"db_list_tables"},
        )
        assert is_mcp_tool("investigate_costs") is False

    def test_empty_set(self, monkeypatch):
        monkeypatch.setattr("src.connections.reporting_mcp._mcp_tool_names", set())
        assert is_mcp_tool("db_anything") is False


# ===========================================================================
# src/connections/babylon.py
# ===========================================================================

# ---------------------------------------------------------------------------
# _resolve_context
# ---------------------------------------------------------------------------


class TestResolveContext:
    def test_uses_current_context(self):
        kc = {
            "current-context": "prod",
            "contexts": [
                {"name": "dev", "context": {"cluster": "dev-cluster"}},
                {"name": "prod", "context": {"cluster": "prod-cluster"}},
            ],
        }
        ctx = _resolve_context(kc, "/fake/path")
        assert ctx["name"] == "prod"

    def test_falls_back_to_first_context(self):
        kc = {
            "contexts": [
                {"name": "alpha", "context": {"cluster": "alpha-cluster"}},
                {"name": "beta", "context": {"cluster": "beta-cluster"}},
            ],
        }
        ctx = _resolve_context(kc, "/fake/path")
        assert ctx["name"] == "alpha"

    def test_falls_back_when_current_context_not_found(self):
        kc = {
            "current-context": "nonexistent",
            "contexts": [
                {"name": "only", "context": {"cluster": "only-cluster"}},
            ],
        }
        ctx = _resolve_context(kc, "/fake/path")
        assert ctx["name"] == "only"

    def test_raises_on_empty_contexts(self):
        kc = {"contexts": []}
        with pytest.raises(ValueError, match="No contexts"):
            _resolve_context(kc, "/some/kubeconfig")

    def test_raises_on_missing_contexts(self):
        kc = {}
        with pytest.raises(ValueError, match="No contexts"):
            _resolve_context(kc, "/some/kubeconfig")

    def test_empty_current_context_falls_back(self):
        kc = {
            "current-context": "",
            "contexts": [
                {"name": "fallback", "context": {"cluster": "fb-cluster"}},
            ],
        }
        ctx = _resolve_context(kc, "/fake/path")
        assert ctx["name"] == "fallback"


# ---------------------------------------------------------------------------
# _resolve_user_credentials
# ---------------------------------------------------------------------------


class TestResolveUserCredentials:
    def test_finds_user_with_token(self):
        kc = {
            "users": [
                {
                    "name": "admin",
                    "user": {"token": "abc123"},
                },
            ],
        }
        creds = _resolve_user_credentials(kc, "admin")
        assert creds["token"] == "abc123"
        assert creds["client_cert_data"] == ""
        assert creds["client_key_data"] == ""

    def test_finds_user_with_cert(self):
        kc = {
            "users": [
                {
                    "name": "cert-user",
                    "user": {
                        "client-certificate-data": "CERTDATA",
                        "client-key-data": "KEYDATA",
                    },
                },
            ],
        }
        creds = _resolve_user_credentials(kc, "cert-user")
        assert creds["token"] == ""
        assert creds["client_cert_data"] == "CERTDATA"
        assert creds["client_key_data"] == "KEYDATA"

    def test_user_not_found(self):
        kc = {
            "users": [
                {"name": "other", "user": {"token": "x"}},
            ],
        }
        creds = _resolve_user_credentials(kc, "missing")
        assert creds["token"] == ""
        assert creds["client_cert_data"] == ""
        assert creds["client_key_data"] == ""

    def test_empty_user_name(self):
        kc = {"users": [{"name": "admin", "user": {"token": "x"}}]}
        creds = _resolve_user_credentials(kc, "")
        assert creds["token"] == ""
        assert creds["client_cert_data"] == ""
        assert creds["client_key_data"] == ""

    def test_no_users_section(self):
        kc = {}
        creds = _resolve_user_credentials(kc, "admin")
        assert creds["token"] == ""

    def test_user_without_user_key(self):
        kc = {"users": [{"name": "bare"}]}
        creds = _resolve_user_credentials(kc, "bare")
        assert creds["token"] == ""
        assert creds["client_cert_data"] == ""
        assert creds["client_key_data"] == ""

    def test_selects_correct_user_from_multiple(self):
        kc = {
            "users": [
                {"name": "alice", "user": {"token": "alice-tok"}},
                {"name": "bob", "user": {"token": "bob-tok"}},
            ],
        }
        creds = _resolve_user_credentials(kc, "bob")
        assert creds["token"] == "bob-tok"


# ---------------------------------------------------------------------------
# resolve_cluster_from_comment
# ---------------------------------------------------------------------------


class TestResolveClusterFromComment:
    @pytest.fixture(autouse=True)
    def setup_cluster_configs(self, monkeypatch):
        configs = {
            "east": {"server": "https://api.ocp-us-east-1.infra.open.redhat.com:6443"},
            "west": {"server": "https://api.ocp-us-west-2.infra.open.redhat.com:6443"},
        }
        monkeypatch.setattr("src.connections.babylon._cluster_configs", configs)

    def test_matches_east(self):
        comment = (
            "sandbox-api https://console-openshift-console.apps.ocp-us-east-1.infra.open.redhat.com"
        )
        assert resolve_cluster_from_comment(comment) == "east"

    def test_matches_west(self):
        comment = (
            "sandbox-api https://console-openshift-console.apps.ocp-us-west-2.infra.open.redhat.com"
        )
        assert resolve_cluster_from_comment(comment) == "west"

    def test_empty_comment(self):
        assert resolve_cluster_from_comment("") == ""

    def test_no_url_in_comment(self):
        assert resolve_cluster_from_comment("just some text without a URL") == ""

    def test_no_matching_cluster(self):
        comment = "sandbox-api https://console-openshift-console.apps.unknown-cluster.example.com"
        assert resolve_cluster_from_comment(comment) == ""

    def test_trailing_slash(self):
        comment = "sandbox-api https://console-openshift-console.apps.ocp-us-east-1.infra.open.redhat.com/"
        assert resolve_cluster_from_comment(comment) == "east"

    def test_comment_with_extra_text(self):
        comment = "sandbox-api https://console-openshift-console.apps.ocp-us-west-2.infra.open.redhat.com some extra info"
        assert resolve_cluster_from_comment(comment) == "west"


# ---------------------------------------------------------------------------
# _load_ca_cert
# ---------------------------------------------------------------------------


class TestLoadCaCert:
    def test_decodes_and_loads(self):
        fake_cert = b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----"
        ca_data = b64encode(fake_cert).decode()

        mock_ctx = MagicMock(spec=ssl.SSLContext)
        with patch("os.unlink") as mock_unlink:
            _load_ca_cert(mock_ctx, ca_data)

        mock_ctx.load_verify_locations.assert_called_once()
        ca_path = mock_ctx.load_verify_locations.call_args[0][0]
        assert ca_path.endswith(".crt")
        mock_unlink.assert_called_once_with(ca_path)

    def test_temp_file_contains_decoded_data(self):
        fake_cert = b"test-ca-data"
        ca_data = b64encode(fake_cert).decode()

        written_content = None

        def capture_load(path):
            nonlocal written_content
            with open(path, "rb") as f:
                written_content = f.read()

        mock_ctx = MagicMock(spec=ssl.SSLContext)
        mock_ctx.load_verify_locations.side_effect = capture_load

        _load_ca_cert(mock_ctx, ca_data)
        assert written_content == fake_cert

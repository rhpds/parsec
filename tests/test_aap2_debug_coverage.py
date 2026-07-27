"""Coverage tests for src/tools/aap2_debug.py.

Covers find_controller_for_url, _parse_extra_vars, _int_field,
and additional fetch_* helpers.
"""


from src.tools.aap2_debug import (
    _int_field,
    _parse_extra_vars,
    find_controller_for_url,
)

# ===================================================================
# find_controller_for_url
# ===================================================================


class TestFindControllerForUrl:
    def test_resolves_known_controller(self, monkeypatch):
        monkeypatch.setattr(
            "src.tools.aap2_debug.resolve_controller",
            lambda hostname: "east",
        )
        result = find_controller_for_url(
            "https://aap2-east.apps.ocp-us-east-1.infra.open.redhat.com/#/jobs/playbook/123"
        )
        assert result == "east"

    def test_extracts_hostname_correctly(self, monkeypatch):
        captured = {}

        def _capture(hostname):
            captured["hostname"] = hostname
            return "west"

        monkeypatch.setattr("src.tools.aap2_debug.resolve_controller", _capture)
        find_controller_for_url(
            "https://aap2-west.apps.ocp-us-west-2.infra.open.redhat.com/api/v2/jobs/456/"
        )
        assert captured["hostname"] == "aap2-west.apps.ocp-us-west-2.infra.open.redhat.com"

    def test_handles_url_without_port(self, monkeypatch):
        monkeypatch.setattr(
            "src.tools.aap2_debug.resolve_controller",
            lambda hostname: "",
        )
        result = find_controller_for_url("https://unknown-controller.example.com")
        assert result == ""


# ===================================================================
# _parse_extra_vars
# ===================================================================


class TestParseExtraVars:
    def test_dict_input(self):
        data = {"extra_vars": {"ACTION": "provision", "GUID": "abc123"}}
        result = _parse_extra_vars(data)
        assert result == {"ACTION": "provision", "GUID": "abc123"}

    def test_json_string_input(self):
        data = {"extra_vars": '{"ACTION": "destroy", "GUID": "xyz"}'}
        result = _parse_extra_vars(data)
        assert result == {"ACTION": "destroy", "GUID": "xyz"}

    def test_empty_string(self):
        data = {"extra_vars": ""}
        result = _parse_extra_vars(data)
        assert result == {}

    def test_invalid_json_string(self):
        data = {"extra_vars": "not valid json"}
        result = _parse_extra_vars(data)
        assert result == {}

    def test_none_value(self):
        data = {"extra_vars": None}
        result = _parse_extra_vars(data)
        assert result == {}

    def test_missing_key(self):
        data = {}
        result = _parse_extra_vars(data)
        assert result == {}

    def test_integer_value(self):
        data = {"extra_vars": 42}
        result = _parse_extra_vars(data)
        assert result == {}


# ===================================================================
# _int_field
# ===================================================================


class TestIntField:
    def test_returns_int(self):
        assert _int_field({"ee_id": 5}, "ee_id") == 5

    def test_returns_none_for_string(self):
        assert _int_field({"ee_id": "five"}, "ee_id") is None

    def test_returns_none_for_missing_key(self):
        assert _int_field({}, "ee_id") is None

    def test_returns_none_for_none_value(self):
        assert _int_field({"ee_id": None}, "ee_id") is None

    def test_returns_none_for_float(self):
        assert _int_field({"ee_id": 5.5}, "ee_id") is None

    def test_returns_zero(self):
        assert _int_field({"ee_id": 0}, "ee_id") == 0

    def test_returns_negative_int(self):
        assert _int_field({"ee_id": -3}, "ee_id") == -3


# ===================================================================
# Additional parse_job_url tests for edge cases
# ===================================================================


class TestParseJobUrlExtended:
    def test_inventory_job_type(self):
        from src.tools.aap2_debug import parse_job_url

        controller, job_id = parse_job_url("https://aap2.example.com/#/jobs/inventory/777")
        assert controller == "https://aap2.example.com"
        assert job_id == 777

    def test_project_job_type(self):
        from src.tools.aap2_debug import parse_job_url

        controller, job_id = parse_job_url("https://aap2.example.com/#/jobs/project/888")
        assert controller == "https://aap2.example.com"
        assert job_id == 888

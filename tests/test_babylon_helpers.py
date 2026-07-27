"""Tests for pure-logic helpers in src/tools/babylon.py."""


from src.tools.babylon import _extract_provision_count, _parse_tower_jobs

# ---------------------------------------------------------------------------
# _parse_tower_jobs
# ---------------------------------------------------------------------------


class TestParseTowerJobs:
    def test_basic_parsing(self):
        tower_jobs = {
            "provision": {
                "towerHost": "east.controller.example.com",
                "deployerJob": "12345",
                "jobStatus": "successful",
                "completeTimestamp": "2024-01-15T10:30:00Z",
            },
            "destroy": {
                "towerHost": "east.controller.example.com",
                "deployerJob": "12346",
            },
        }
        fields = {
            "towerHost": "controller",
            "deployerJob": "job_id",
            "jobStatus": "status",
            "completeTimestamp": "completed",
        }
        result = _parse_tower_jobs(tower_jobs, fields)
        assert "provision" in result
        assert result["provision"]["controller"] == "east.controller.example.com"
        assert result["provision"]["job_id"] == "12345"
        assert result["provision"]["status"] == "successful"
        assert result["provision"]["completed"] == "2024-01-15T10:30:00Z"
        assert "destroy" in result
        assert result["destroy"]["controller"] == "east.controller.example.com"
        assert result["destroy"]["job_id"] == "12346"
        # destroy has no jobStatus or completeTimestamp
        assert "status" not in result["destroy"]

    def test_empty_tower_jobs(self):
        assert _parse_tower_jobs({}, {"towerHost": "controller"}) == {}

    def test_none_tower_jobs(self):
        assert _parse_tower_jobs(None, {"towerHost": "controller"}) == {}

    def test_non_dict_tower_jobs(self):
        assert _parse_tower_jobs("not a dict", {"towerHost": "controller"}) == {}

    def test_non_dict_job_info_skipped(self):
        tower_jobs = {
            "provision": "not a dict",
            "destroy": {
                "towerHost": "east.controller.example.com",
                "deployerJob": "12345",
            },
        }
        fields = {"towerHost": "controller", "deployerJob": "job_id"}
        result = _parse_tower_jobs(tower_jobs, fields)
        assert "provision" not in result
        assert "destroy" in result

    def test_missing_fields_omitted(self):
        tower_jobs = {
            "provision": {
                "towerHost": "east.controller.example.com",
            },
        }
        fields = {
            "towerHost": "controller",
            "deployerJob": "job_id",
            "jobStatus": "status",
        }
        result = _parse_tower_jobs(tower_jobs, fields)
        assert result["provision"] == {"controller": "east.controller.example.com"}

    def test_empty_value_not_included(self):
        tower_jobs = {
            "provision": {
                "towerHost": "",
                "deployerJob": "12345",
            },
        }
        fields = {"towerHost": "controller", "deployerJob": "job_id"}
        result = _parse_tower_jobs(tower_jobs, fields)
        # Empty string is falsy, so towerHost is not included
        assert result["provision"] == {"job_id": "12345"}

    def test_all_empty_values_produces_no_entry(self):
        tower_jobs = {
            "provision": {
                "towerHost": "",
                "deployerJob": "",
            },
        }
        fields = {"towerHost": "controller", "deployerJob": "job_id"}
        result = _parse_tower_jobs(tower_jobs, fields)
        # No entry is added because all values are falsy
        assert result == {}

    def test_multiple_actions(self):
        tower_jobs = {
            "provision": {"towerHost": "ctrl1", "deployerJob": "1"},
            "stop": {"towerHost": "ctrl1", "deployerJob": "2"},
            "start": {"towerHost": "ctrl1", "deployerJob": "3"},
            "destroy": {"towerHost": "ctrl1", "deployerJob": "4"},
        }
        fields = {"towerHost": "controller", "deployerJob": "job_id"}
        result = _parse_tower_jobs(tower_jobs, fields)
        assert len(result) == 4
        assert set(result.keys()) == {"provision", "stop", "start", "destroy"}


# ---------------------------------------------------------------------------
# _extract_provision_count
# ---------------------------------------------------------------------------


class TestExtractProvisionCount:
    def test_basic_counts(self):
        status = {
            "provisionCount": {
                "ordered": 10,
                "active": 8,
                "failed": 2,
                "retries": 1,
            }
        }
        result = _extract_provision_count(status)
        assert result == {"ordered": 10, "active": 8, "failed": 2, "retries": 1}

    def test_empty_provision_count(self):
        status = {"provisionCount": {}}
        result = _extract_provision_count(status)
        assert result == {"ordered": 0, "active": 0, "failed": 0, "retries": 0}

    def test_missing_provision_count(self):
        status = {}
        result = _extract_provision_count(status)
        assert result == {"ordered": 0, "active": 0, "failed": 0, "retries": 0}

    def test_non_dict_provision_count(self):
        status = {"provisionCount": "invalid"}
        result = _extract_provision_count(status)
        assert result == {"ordered": 0, "active": 0, "failed": 0, "retries": 0}

    def test_partial_counts(self):
        status = {
            "provisionCount": {
                "ordered": 5,
                "active": 3,
            }
        }
        result = _extract_provision_count(status)
        assert result == {"ordered": 5, "active": 3, "failed": 0, "retries": 0}

    def test_zero_counts(self):
        status = {
            "provisionCount": {
                "ordered": 0,
                "active": 0,
                "failed": 0,
                "retries": 0,
            }
        }
        result = _extract_provision_count(status)
        assert result == {"ordered": 0, "active": 0, "failed": 0, "retries": 0}

    def test_none_provision_count(self):
        status = {"provisionCount": None}
        result = _extract_provision_count(status)
        assert result == {"ordered": 0, "active": 0, "failed": 0, "retries": 0}

"""Tests for AAP2 debug orchestrator (URL parsing and controller resolution)."""

import pytest

from src.tools.aap2_debug import parse_job_url


def test_parse_hash_fragment_url():
    controller, job_id = parse_job_url("https://aap2-prod.example.com/#/jobs/playbook/12345/output")
    assert controller == "https://aap2-prod.example.com"
    assert job_id == 12345


def test_parse_api_url():
    controller, job_id = parse_job_url("https://aap2-prod.example.com/api/v2/jobs/67890/")
    assert controller == "https://aap2-prod.example.com"
    assert job_id == 67890


def test_parse_command_job():
    controller, job_id = parse_job_url("https://controller.example.com/#/jobs/command/999")
    assert controller == "https://controller.example.com"
    assert job_id == 999


def test_parse_with_query_params():
    controller, job_id = parse_job_url(
        "https://controller.example.com/#/jobs/playbook/555?tab=output"
    )
    assert controller == "https://controller.example.com"
    assert job_id == 555


def test_parse_invalid_url():
    with pytest.raises(ValueError, match="Could not extract job ID"):
        parse_job_url("https://example.com/not-a-job-url")


def test_parse_garbage():
    with pytest.raises(ValueError):
        parse_job_url("not a url at all")

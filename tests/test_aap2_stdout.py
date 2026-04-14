"""Tests for Ansible stdout parser."""

from src.tools.aap2_stdout import extract_failing_task


def test_fatal_failed_with_json():
    stdout = (
        "TASK [agnosticd.osp_on_ocp.ocp4_setup : Create namespace] ***\n"
        "fatal: [bastion.abc12.sandbox1234.opentlc.com]: FAILED! => "
        '{"msg": "Failed to create namespace", "rc": 1}\n'
    )
    result = extract_failing_task(stdout)
    assert result is not None
    assert result["taskName"] == "Create namespace"
    assert result["roleFqcn"] == "agnosticd.osp_on_ocp.ocp4_setup"
    assert result["hostPattern"] == "bastion.abc12.sandbox1234.opentlc.com"
    assert "Failed to create namespace" in result["errorMessage"]


def test_fatal_without_role():
    stdout = (
        "TASK [Install packages] ***\n"
        'fatal: [host1]: FAILED! => {"msg": "No package matching found"}\n'
    )
    result = extract_failing_task(stdout)
    assert result is not None
    assert result["taskName"] == "Install packages"
    assert result["roleFqcn"] is None
    assert result["hostPattern"] == "host1"


def test_error_bracket():
    stdout = '[ERROR]: Task failed: cannot find role "missing_role"\n'
    result = extract_failing_task(stdout)
    assert result is not None
    assert result["taskName"] == "Ansible error"
    assert "missing_role" in result["errorMessage"]


def test_error_bang():
    stdout = "ERROR! No inventory was parsed\n"
    result = extract_failing_task(stdout)
    assert result is not None
    assert result["taskName"] == "Ansible parse error"
    assert "No inventory was parsed" in result["errorMessage"]


def test_no_failure():
    stdout = "TASK [Do something] ***\nok: [host1]\nPLAY RECAP ***\n"
    result = extract_failing_task(stdout)
    assert result is None


def test_empty_stdout():
    assert extract_failing_task("") is None


def test_failed_loop_item():
    stdout = (
        "TASK [agnosticd.core.setup : Verify DNS] ***\n"
        "failed: [host1] (item=api.cluster.example.com) => "
        '{"msg": "DNS lookup failed", "item": "api.cluster.example.com"}\n'
    )
    result = extract_failing_task(stdout)
    assert result is not None
    assert result["taskName"] == "Verify DNS"
    assert result["roleFqcn"] == "agnosticd.core.setup"
    assert "DNS lookup failed" in result["errorMessage"]

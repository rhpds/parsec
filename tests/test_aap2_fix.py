"""Tests for AAP2 fix recommendation engine (pattern matching only)."""

from src.tools.aap2_fix import match_pattern


def test_match_invalid_client_token():
    result = match_pattern(
        "InvalidClientTokenId: The security token included in the request is invalid"
    )
    assert result is not None
    assert result["source"] == "pattern"
    assert "credential" in result["explanation"].lower()


def test_match_private_data_dir():
    result = match_pattern("unrecognized arguments: --private-data-dir /runner/artifacts")
    assert result is not None
    assert result["source"] == "pattern"
    assert "entrypoint" in result["explanation"].lower()


def test_match_json_format():
    result = match_pattern("configuration string is not in JSON format")
    assert result is not None
    assert result["source"] == "pattern"


def test_match_role_not_found():
    result = match_pattern("ERROR! the role 'agnosticd.missing_role' was not found")
    assert result is not None
    assert result["source"] == "pattern"
    assert "collection" in result["explanation"].lower()


def test_match_worker_stream():
    result = match_pattern("Failed to JSON parse a line from worker stream")
    assert result is not None
    assert result["source"] == "pattern"


def test_no_match():
    result = match_pattern("Some random unrecognized error message")
    assert result is None


def test_catalog_item_substitution():
    result = match_pattern(
        "InvalidClientTokenId",
        extra_vars={"catalog_item": "ocp4-cluster", "account": "agd-v2"},
        job_template_name="RHPDS agd-v2.ocp4-cluster.prod-abc12-1-provision x",
    )
    assert result is not None
    assert "agd-v2/ocp4-cluster" in result["file"]


def test_extract_catalog_item_from_template_name():
    from src.tools.aap2_fix import extract_catalog_item_path

    path = extract_catalog_item_path({}, "RHPDS agd-v2.sovereign-cloud.prod-abc12-1-provision x")
    assert path == "agd_v2/sovereign-cloud"


def test_extract_catalog_item_from_extra_vars():
    from src.tools.aap2_fix import extract_catalog_item_path

    path = extract_catalog_item_path({"catalog_item": "ocp4-cluster", "account": "openshift_cnv"})
    assert path == "openshift_cnv/ocp4-cluster"

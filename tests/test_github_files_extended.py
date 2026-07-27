"""Tests for src/tools/github_files.py — GitHub file tools and catalog index."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.tools.github_files import (
    _build_catalog_index,
    _fetch_pr_changed_files,
    _find_catalog_dirs,
    _is_catalog_dir_candidate,
    _redact_secrets,
    _simplify_directory_listing,
)

# ---------------------------------------------------------------------------
# _is_catalog_dir_candidate
# ---------------------------------------------------------------------------


class TestIsCatalogDirCandidate:
    def test_valid_path_parts(self):
        assert _is_catalog_dir_candidate(["sandboxes-gpte", "ITEM_NAME"]) is True

    def test_dot_prefix_excluded(self):
        assert _is_catalog_dir_candidate([".github", "workflows"]) is False

    def test_skip_dir_includes(self):
        assert _is_catalog_dir_candidate(["includes", "shared"]) is False

    def test_skip_dir_tests(self):
        assert _is_catalog_dir_candidate(["tests", "unit"]) is False

    def test_skip_dir_example_account(self):
        assert _is_catalog_dir_candidate(["EXAMPLE_ACCOUNT", "demo"]) is False

    def test_normal_directory(self):
        assert _is_catalog_dir_candidate(["my-account", "my-item"]) is True

    def test_single_part(self):
        assert _is_catalog_dir_candidate(["sandboxes-gpte"]) is True

    def test_dot_hidden_single(self):
        assert _is_catalog_dir_candidate([".hidden"]) is False


# ---------------------------------------------------------------------------
# _find_catalog_dirs
# ---------------------------------------------------------------------------


class TestFindCatalogDirs:
    def test_empty_tree(self):
        assert _find_catalog_dirs([]) == {}

    def test_valid_two_level_dirs(self):
        tree = [
            {"path": "sandboxes-gpte/ITEM_A", "type": "tree"},
            {"path": "sandboxes-gpte/ITEM_B", "type": "tree"},
        ]
        result = _find_catalog_dirs(tree)
        assert "sandboxes-gpte/ITEM_A" in result
        assert "sandboxes-gpte/ITEM_B" in result
        assert result["sandboxes-gpte/ITEM_A"] == []
        assert result["sandboxes-gpte/ITEM_B"] == []

    def test_blobs_inside_known_dirs(self):
        tree = [
            {"path": "sandboxes-gpte/ITEM_A", "type": "tree"},
            {"path": "sandboxes-gpte/ITEM_A/common.yaml", "type": "blob"},
            {"path": "sandboxes-gpte/ITEM_A/dev.yaml", "type": "blob"},
        ]
        result = _find_catalog_dirs(tree)
        assert result["sandboxes-gpte/ITEM_A"] == ["common.yaml", "dev.yaml"]

    def test_skip_dirs_excluded(self):
        tree = [
            {"path": "includes/shared_role", "type": "tree"},
            {"path": "tests/test_item", "type": "tree"},
            {"path": "EXAMPLE_ACCOUNT/DEMO", "type": "tree"},
        ]
        result = _find_catalog_dirs(tree)
        assert result == {}

    def test_dot_prefix_dirs_excluded(self):
        tree = [
            {"path": ".github/workflows", "type": "tree"},
        ]
        result = _find_catalog_dirs(tree)
        assert result == {}

    def test_mixed_valid_and_invalid(self):
        tree = [
            {"path": "sandboxes-gpte/GOOD_ITEM", "type": "tree"},
            {"path": ".github/workflows", "type": "tree"},
            {"path": "includes/shared", "type": "tree"},
            {"path": "sandboxes-gpte/GOOD_ITEM/common.yaml", "type": "blob"},
            {"path": "another-account/OTHER_ITEM", "type": "tree"},
        ]
        result = _find_catalog_dirs(tree)
        assert "sandboxes-gpte/GOOD_ITEM" in result
        assert "another-account/OTHER_ITEM" in result
        assert result["sandboxes-gpte/GOOD_ITEM"] == ["common.yaml"]
        assert ".github/workflows" not in result
        assert "includes/shared" not in result

    def test_blobs_for_unknown_parent_ignored(self):
        tree = [
            {"path": "sandboxes-gpte/UNKNOWN/common.yaml", "type": "blob"},
        ]
        result = _find_catalog_dirs(tree)
        # Parent was never registered as a tree entry, so blob is ignored
        assert result == {}

    def test_single_level_tree_ignored(self):
        tree = [
            {"path": "sandboxes-gpte", "type": "tree"},
        ]
        # Only 1 part, needs >= 2 for catalog dir registration
        result = _find_catalog_dirs(tree)
        assert result == {}

    def test_deeper_tree_entries_skipped_for_dir_registration(self):
        tree = [
            {"path": "acct/item/sub/deep", "type": "tree"},
        ]
        # 4 parts — len(parts) >= 2 and type == tree but len(parts) != 2
        result = _find_catalog_dirs(tree)
        assert result == {}


# ---------------------------------------------------------------------------
# _index_single_repo
# ---------------------------------------------------------------------------


def _make_response(status_code, json_data):
    """Create a mock httpx response."""
    return SimpleNamespace(status_code=status_code, json=lambda: json_data)


class TestIndexSingleRepo:
    @pytest.mark.asyncio
    async def test_successful_index(self):
        from src.tools.github_files import _index_single_repo

        repo_resp = _make_response(200, {"default_branch": "main"})
        tree_resp = _make_response(
            200,
            {
                "tree": [
                    {"path": "sandboxes-gpte/MY_ITEM", "type": "tree"},
                    {"path": "sandboxes-gpte/MY_ITEM/common.yaml", "type": "blob"},
                    {"path": "sandboxes-gpte/MY_ITEM/dev.yaml", "type": "blob"},
                ]
            },
        )

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[repo_resp, tree_resp])

        result = await _index_single_repo(client, "rhpds", "agnosticv", {})
        assert "my-item" in result
        assert result["my-item"]["owner"] == "rhpds"
        assert result["my-item"]["repo"] == "agnosticv"
        assert result["my-item"]["account"] == "sandboxes-gpte"
        assert result["my-item"]["directory"] == "MY_ITEM"
        assert "common.yaml" in result["my-item"]["files"]
        assert result["my-item"]["default_branch"] == "main"

    @pytest.mark.asyncio
    async def test_failed_tree_api(self):
        from src.tools.github_files import _index_single_repo

        repo_resp = _make_response(200, {"default_branch": "main"})
        tree_resp = _make_response(404, {})

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[repo_resp, tree_resp])

        result = await _index_single_repo(client, "rhpds", "agnosticv", {})
        assert result == {}

    @pytest.mark.asyncio
    async def test_no_common_yaml_excluded(self):
        from src.tools.github_files import _index_single_repo

        repo_resp = _make_response(200, {"default_branch": "main"})
        tree_resp = _make_response(
            200,
            {
                "tree": [
                    {"path": "sandboxes-gpte/NO_COMMON", "type": "tree"},
                    {"path": "sandboxes-gpte/NO_COMMON/dev.yaml", "type": "blob"},
                ]
            },
        )

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[repo_resp, tree_resp])

        result = await _index_single_repo(client, "rhpds", "agnosticv", {})
        assert result == {}

    @pytest.mark.asyncio
    async def test_underscore_to_dash_normalization(self):
        from src.tools.github_files import _index_single_repo

        repo_resp = _make_response(200, {"default_branch": "dev"})
        tree_resp = _make_response(
            200,
            {
                "tree": [
                    {"path": "acct/ANS_BU_WKSP_RHEL_90", "type": "tree"},
                    {"path": "acct/ANS_BU_WKSP_RHEL_90/common.yaml", "type": "blob"},
                ]
            },
        )

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[repo_resp, tree_resp])

        result = await _index_single_repo(client, "rhpds", "agnosticv", {})
        assert "ans-bu-wksp-rhel-90" in result
        assert result["ans-bu-wksp-rhel-90"]["default_branch"] == "dev"

    @pytest.mark.asyncio
    async def test_repo_api_non_200_uses_default_branch(self):
        from src.tools.github_files import _index_single_repo

        repo_resp = _make_response(403, {})
        tree_resp = _make_response(
            200,
            {
                "tree": [
                    {"path": "acct/ITEM", "type": "tree"},
                    {"path": "acct/ITEM/common.yaml", "type": "blob"},
                ]
            },
        )

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[repo_resp, tree_resp])

        result = await _index_single_repo(client, "rhpds", "agnosticv", {})
        assert "item" in result
        assert result["item"]["default_branch"] == "main"


# ---------------------------------------------------------------------------
# _build_catalog_index
# ---------------------------------------------------------------------------


class TestBuildCatalogIndex:
    @pytest.mark.asyncio
    async def test_no_token_warns(self, monkeypatch):
        import src.tools.github_files as mod

        monkeypatch.setattr(mod, "get_token", lambda: None)
        # Reset state
        monkeypatch.setattr(mod, "_catalog_index", {})
        monkeypatch.setattr(mod, "_index_built_at", 0.0)

        await _build_catalog_index()

        assert mod._catalog_index == {}

    @pytest.mark.asyncio
    async def test_builds_index_from_repos(self, monkeypatch):
        import src.tools.github_files as mod

        monkeypatch.setattr(mod, "get_token", lambda: "fake-token")
        monkeypatch.setattr(mod, "_catalog_index", {})
        monkeypatch.setattr(mod, "_index_built_at", 0.0)

        async def mock_index_single_repo(client, owner, repo, headers):
            if repo == "agnosticv":
                return {"item-a": {"owner": owner, "repo": repo}}
            elif repo == "partner-agnosticv":
                return {"item-b": {"owner": owner, "repo": repo}}
            return {}

        monkeypatch.setattr(mod, "_index_single_repo", mock_index_single_repo)

        await _build_catalog_index()

        assert "item-a" in mod._catalog_index
        assert "item-b" in mod._catalog_index
        assert mod._index_built_at > 0.0

    @pytest.mark.asyncio
    async def test_one_repo_failing_still_indexes_others(self, monkeypatch):
        import src.tools.github_files as mod

        monkeypatch.setattr(mod, "get_token", lambda: "fake-token")
        monkeypatch.setattr(mod, "_catalog_index", {})
        monkeypatch.setattr(mod, "_index_built_at", 0.0)

        call_count = 0

        async def mock_index_single_repo(client, owner, repo, headers):
            nonlocal call_count
            call_count += 1
            if repo == "agnosticv":
                raise RuntimeError("network error")
            return {"good-item": {"owner": owner, "repo": repo}}

        monkeypatch.setattr(mod, "_index_single_repo", mock_index_single_repo)

        await _build_catalog_index()

        # Despite agnosticv failing, other repos should be indexed
        assert "good-item" in mod._catalog_index
        assert mod._index_built_at > 0.0


# ---------------------------------------------------------------------------
# _fetch_pr_changed_files
# ---------------------------------------------------------------------------


class TestFetchPrChangedFiles:
    @pytest.mark.asyncio
    async def test_successful_response(self):
        resp = _make_response(
            200,
            [
                {"filename": "sandboxes-gpte/ITEM/common.yaml"},
                {"filename": "sandboxes-gpte/ITEM/dev.yaml"},
            ],
        )
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        result = await _fetch_pr_changed_files(client, "rhpds", "agnosticv", 42, "tok")
        assert result == [
            "sandboxes-gpte/ITEM/common.yaml",
            "sandboxes-gpte/ITEM/dev.yaml",
        ]

    @pytest.mark.asyncio
    async def test_non_200_returns_empty(self):
        resp = _make_response(404, [])
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        result = await _fetch_pr_changed_files(client, "rhpds", "agnosticv", 42, "tok")
        assert result == []

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=RuntimeError("connection failed"))

        result = await _fetch_pr_changed_files(client, "rhpds", "agnosticv", 42, "tok")
        assert result == []

    @pytest.mark.asyncio
    async def test_missing_filename_key(self):
        resp = _make_response(200, [{"sha": "abc123"}, {"filename": "real.yaml"}])
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)

        result = await _fetch_pr_changed_files(client, "rhpds", "agnosticv", 1, "tok")
        assert result == ["", "real.yaml"]


# ---------------------------------------------------------------------------
# _redact_secrets
# ---------------------------------------------------------------------------


class TestRedactSecrets:
    def test_no_secrets_unchanged(self):
        text = "name: my-item\nversion: 1.0"
        assert _redact_secrets(text) == text

    def test_password_redacted(self):
        text = "db_password: supersecret123"
        result = _redact_secrets(text)
        assert "supersecret123" not in result
        assert "db_password: <REDACTED>" in result

    def test_access_key_redacted(self):
        text = "access_key=AKIA_FAKE_TEST_KEY_"
        result = _redact_secrets(text)
        assert "AKIA_FAKE_TEST_KEY_" not in result
        assert "access_key: <REDACTED>" in result

    def test_multiple_lines_selective(self):
        text = "name: my-item\napi_key: sk-12345\nversion: 2.0\nsecret_key: abcdef"
        result = _redact_secrets(text)
        lines = result.split("\n")
        assert lines[0] == "name: my-item"
        assert lines[1] == "api_key: <REDACTED>"
        assert lines[2] == "version: 2.0"
        assert lines[3] == "secret_key: <REDACTED>"

    def test_token_redacted(self):
        text = "auth_token: ghp_FAKE_TEST_TOKEN"
        result = _redact_secrets(text)
        assert "ghp_FAKE_TEST_TOKEN" not in result
        assert "auth_token: <REDACTED>" in result

    def test_pull_secret_redacted(self):
        text = "pull_secret: '{\"auths\": ...}'"
        result = _redact_secrets(text)
        assert "auths" not in result
        assert "pull_secret: <REDACTED>" in result

    def test_client_secret_redacted(self):
        text = "client_secret: my-oauth-secret"
        result = _redact_secrets(text)
        assert "my-oauth-secret" not in result
        assert "client_secret: <REDACTED>" in result

    def test_case_insensitive(self):
        text = "AWS_SECRET_KEY: mysecret"
        result = _redact_secrets(text)
        assert "mysecret" not in result
        assert "<REDACTED>" in result

    def test_hmac_key_redacted(self):
        text = "hmac_key: some-hmac-value"
        result = _redact_secrets(text)
        assert "some-hmac-value" not in result

    def test_activationkey_redacted(self):
        text = "activationkey: test-value"
        result = _redact_secrets(text)
        assert "test-value" not in result

    def test_empty_string(self):
        assert _redact_secrets("") == ""


# ---------------------------------------------------------------------------
# _simplify_directory_listing
# ---------------------------------------------------------------------------


class TestSimplifyDirectoryListing:
    def test_non_json_returned_as_is(self):
        text = "This is plain text content"
        assert _simplify_directory_listing(text) == text

    def test_json_dir_entries_simplified(self):
        entries = [
            {"name": "common.yaml", "type": "file"},
            {"name": "dev.yaml", "type": "file"},
            {"name": "subdir", "type": "dir"},
        ]
        result = _simplify_directory_listing(json.dumps(entries))
        lines = result.split("\n")
        assert "common.yaml" in lines
        assert "dev.yaml" in lines
        assert "subdir/" in lines

    def test_empty_array_returned_as_is(self):
        text = "[]"
        assert _simplify_directory_listing(text) == text

    def test_non_list_json_returned_as_is(self):
        text = '{"key": "value"}'
        assert _simplify_directory_listing(text) == text

    def test_list_without_name_key_returned_as_is(self):
        text = '[{"id": 1, "value": "foo"}]'
        assert _simplify_directory_listing(text) == text

    def test_sorted_output(self):
        entries = [
            {"name": "z_last.yaml", "type": "file"},
            {"name": "a_first.yaml", "type": "file"},
            {"name": "m_middle", "type": "dir"},
        ]
        result = _simplify_directory_listing(json.dumps(entries))
        lines = result.split("\n")
        assert lines[0] == "a_first.yaml"
        assert lines[1] == "m_middle/"
        assert lines[2] == "z_last.yaml"

    def test_invalid_json_returned_as_is(self):
        text = "{not valid json"
        assert _simplify_directory_listing(text) == text

    def test_entry_missing_type_defaults_to_file(self):
        entries = [{"name": "README.md"}]
        result = _simplify_directory_listing(json.dumps(entries))
        assert result == "README.md"

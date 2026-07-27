"""Extended tests for src/tools/icinga.py — covers dispatch logic,
alias resolution, write action validation, and edge cases."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.tools.icinga import (
    _ACTION_ALIASES,
    _build_read_args,
    _dispatch_write,
    query_icinga,
)

# ---------------------------------------------------------------------------
# _build_read_args
# ---------------------------------------------------------------------------


class TestBuildReadArgs:
    def test_all_params(self):
        args = _build_read_args("cpu", "host1", "http", 'host.name=="host1"', True)
        assert args["search"] == "cpu"
        assert args["host"] == "host1"
        assert args["service"] == "http"
        assert args["filter_expr"] == 'host.name=="host1"'
        assert args["detailed"] is True

    def test_empty_params(self):
        args = _build_read_args("", "", "", "", False)
        assert "search" not in args
        assert "host" not in args
        assert "service" not in args
        assert "filter_expr" not in args
        assert args["detailed"] is False

    def test_partial_params(self):
        args = _build_read_args("", "host1", "", "", True)
        assert "search" not in args
        assert args["host"] == "host1"
        assert args["detailed"] is True


# ---------------------------------------------------------------------------
# query_icinga — read actions
# ---------------------------------------------------------------------------


class TestQueryIcingaReadActions:
    @pytest.mark.asyncio
    @patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
    async def test_get_hosts(self, mock_call):
        mock_call.return_value = {"results": []}
        await query_icinga("get_hosts", host="myhost")
        mock_call.assert_called_once_with("get_hosts", {"host": "myhost", "detailed": False})

    @pytest.mark.asyncio
    @patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
    async def test_get_services(self, mock_call):
        mock_call.return_value = {"results": []}
        await query_icinga("get_services", search="http", detailed=True)
        mock_call.assert_called_once_with("get_services", {"search": "http", "detailed": True})

    @pytest.mark.asyncio
    @patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
    async def test_get_problems(self, mock_call):
        mock_call.return_value = {"results": []}
        await query_icinga("get_problems")
        mock_call.assert_called_once_with("get_problems", {})

    @pytest.mark.asyncio
    @patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
    async def test_get_downtimes(self, mock_call):
        mock_call.return_value = {"results": []}
        await query_icinga("get_downtimes", host="web1")
        mock_call.assert_called_once_with("get_downtimes", {"host": "web1"})

    @pytest.mark.asyncio
    @patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
    async def test_get_comments(self, mock_call):
        mock_call.return_value = {"results": []}
        await query_icinga("get_comments", service="http")
        mock_call.assert_called_once_with("get_comments", {"service": "http"})


# ---------------------------------------------------------------------------
# query_icinga — alias resolution
# ---------------------------------------------------------------------------


class TestQueryIcingaAliases:
    @pytest.mark.asyncio
    @patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
    async def test_search_alerts_alias(self, mock_call):
        mock_call.return_value = {"results": []}
        await query_icinga("search_alerts")
        mock_call.assert_called_once_with("get_problems", {})

    @pytest.mark.asyncio
    @patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
    async def test_get_service_details_alias_sets_detailed(self, mock_call):
        mock_call.return_value = {"results": []}
        await query_icinga("get_service_details", host="web1")
        # "detail" in original_action -> detailed=True
        call_args = mock_call.call_args[0]
        assert call_args[0] == "get_services"
        assert mock_call.call_args[0][1]["detailed"] is True

    @pytest.mark.asyncio
    @patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
    async def test_list_hosts_alias(self, mock_call):
        mock_call.return_value = {"results": []}
        await query_icinga("list_hosts")
        assert mock_call.call_args[0][0] == "get_hosts"

    @pytest.mark.asyncio
    @patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
    async def test_get_host_details_sets_detailed(self, mock_call):
        mock_call.return_value = {"results": []}
        await query_icinga("get_host_details", host="web1")
        assert mock_call.call_args[0][0] == "get_hosts"
        assert mock_call.call_args[0][1]["detailed"] is True


# ---------------------------------------------------------------------------
# query_icinga — write actions
# ---------------------------------------------------------------------------


class TestQueryIcingaWriteActions:
    @pytest.mark.asyncio
    async def test_unknown_action(self):
        result = await query_icinga("totally_invalid_action")
        assert "error" in result

    @pytest.mark.asyncio
    @patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
    async def test_remove_comment(self, mock_call):
        mock_call.return_value = {"status": "ok"}
        await query_icinga("remove_comment", comment_name="comment-123")
        mock_call.assert_called_once_with("remove_comment", {"comment_name": "comment-123"})

    @pytest.mark.asyncio
    async def test_remove_comment_missing_name(self):
        result = await query_icinga("remove_comment")
        assert "error" in result
        assert "comment_name" in result["error"]


# ---------------------------------------------------------------------------
# _dispatch_write
# ---------------------------------------------------------------------------


class TestDispatchWrite:
    @pytest.mark.asyncio
    async def test_missing_object_type(self):
        result = await _dispatch_write(
            "acknowledge_problem", "", "host1", "admin", "ack", None, None
        )
        assert "error" in result
        assert "object_type" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_name(self):
        result = await _dispatch_write(
            "acknowledge_problem", "Host", "", "admin", "ack", None, None
        )
        assert "error" in result
        assert "object_type" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_comment_for_ack(self):
        result = await _dispatch_write(
            "acknowledge_problem", "Host", "web1", "admin", "", None, None
        )
        assert "error" in result
        assert "comment" in result["error"]

    @pytest.mark.asyncio
    async def test_schedule_downtime_missing_times(self):
        result = await _dispatch_write(
            "schedule_downtime", "Host", "web1", "admin", "maintenance", None, None
        )
        assert "error" in result
        assert "start_time" in result["error"]

    @pytest.mark.asyncio
    @patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
    async def test_schedule_downtime_success(self, mock_call):
        mock_call.return_value = {"status": "ok"}
        await _dispatch_write(
            "schedule_downtime", "Host", "web1", "admin", "maintenance", 1000.0, 2000.0
        )
        mock_call.assert_called_once_with(
            "schedule_downtime",
            {
                "object_type": "Host",
                "name": "web1",
                "author": "admin",
                "comment": "maintenance",
                "start_time": 1000.0,
                "end_time": 2000.0,
            },
        )

    @pytest.mark.asyncio
    @patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
    async def test_acknowledge_problem_success(self, mock_call):
        mock_call.return_value = {"status": "ok"}
        await _dispatch_write(
            "acknowledge_problem", "Host", "web1", "admin", "acknowledged", None, None
        )
        mock_call.assert_called_once_with(
            "acknowledge_problem",
            {
                "object_type": "Host",
                "name": "web1",
                "author": "admin",
                "comment": "acknowledged",
            },
        )

    @pytest.mark.asyncio
    @patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
    async def test_reschedule_check_no_comment_needed(self, mock_call):
        mock_call.return_value = {"status": "ok"}
        await _dispatch_write("reschedule_check", "Host", "web1", "admin", "", None, None)
        # reschedule_check is not in _WRITE_ACTIONS_REQUIRING_COMMENT
        mock_call.assert_called_once_with(
            "reschedule_check",
            {"object_type": "Host", "name": "web1"},
        )

    @pytest.mark.asyncio
    @patch("src.tools.icinga.call_tool", new_callable=AsyncMock)
    async def test_send_custom_notification(self, mock_call):
        mock_call.return_value = {"status": "ok"}
        await _dispatch_write(
            "send_custom_notification", "Service", "http!web1", "admin", "check this", None, None
        )
        mock_call.assert_called_once_with(
            "send_custom_notification",
            {
                "object_type": "Service",
                "name": "http!web1",
                "author": "admin",
                "comment": "check this",
            },
        )


# ---------------------------------------------------------------------------
# Alias mapping completeness
# ---------------------------------------------------------------------------


class TestActionAliases:
    def test_all_aliases_resolve_to_valid_actions(self):
        valid_actions = {
            "get_hosts",
            "get_services",
            "get_problems",
            "get_downtimes",
            "get_comments",
        }
        for alias, target in _ACTION_ALIASES.items():
            assert target in valid_actions, f"Alias {alias} -> {target} not in valid actions"

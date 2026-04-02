"""Tool: query_icinga — query Icinga2 monitoring via the MCP sidecar server."""

import logging
from typing import Any

from src.connections.icinga_mcp import call_tool

logger = logging.getLogger(__name__)


def _build_read_args(
    search: str, host: str, service: str, filter_expr: str, detailed: bool
) -> dict[str, Any]:
    """Build arguments dict for read-only query actions, omitting empty values."""
    args: dict[str, Any] = {}
    if search:
        args["search"] = search
    if host:
        args["host"] = host
    if service:
        args["service"] = service
    if filter_expr:
        args["filter_expr"] = filter_expr
    args["detailed"] = detailed
    return args


_WRITE_ACTIONS_REQUIRING_OBJECT: set[str] = {
    "acknowledge_problem",
    "schedule_downtime",
    "reschedule_check",
    "add_comment",
    "remove_downtime",
    "remove_acknowledgement",
    "send_custom_notification",
}

_WRITE_ACTIONS_REQUIRING_COMMENT: set[str] = {
    "acknowledge_problem",
    "schedule_downtime",
    "add_comment",
    "send_custom_notification",
}


async def query_icinga(
    action: str,
    search: str = "",
    host: str = "",
    service: str = "",
    filter_expr: str = "",
    detailed: bool = False,
    object_type: str = "",
    name: str = "",
    author: str = "parsec",
    comment: str = "",
    comment_name: str = "",
    start_time: float | None = None,
    end_time: float | None = None,
) -> dict:
    """Dispatch an Icinga query to the appropriate MCP tool.

    Read-only actions: get_hosts, get_services, get_problems, get_downtimes,
    get_comments.

    Write actions: acknowledge_problem, schedule_downtime, reschedule_check,
    add_comment, remove_comment, remove_downtime, remove_acknowledgement,
    send_custom_notification.
    """
    if action in ("get_hosts", "get_services"):
        args = _build_read_args(search, host, service, filter_expr, detailed)
        return await call_tool(action, args)

    if action == "get_problems":
        return await call_tool("get_problems", {})

    if action in ("get_downtimes", "get_comments"):
        filter_args: dict[str, Any] = {}
        if host:
            filter_args["host"] = host
        if service:
            filter_args["service"] = service
        return await call_tool(action, filter_args)

    if action == "remove_comment":
        if not comment_name:
            return {"error": "remove_comment requires comment_name"}
        return await call_tool("remove_comment", {"comment_name": comment_name})

    if action in _WRITE_ACTIONS_REQUIRING_OBJECT:
        return await _dispatch_write(
            action, object_type, name, author, comment, start_time, end_time
        )

    return {"error": f"Unknown icinga action: {action}"}


async def _dispatch_write(
    action: str,
    object_type: str,
    name: str,
    author: str,
    comment: str,
    start_time: float | None,
    end_time: float | None,
) -> dict:
    """Validate and dispatch write actions that target a host or service."""
    if not object_type or not name:
        return {"error": f"{action} requires object_type and name"}

    if action in _WRITE_ACTIONS_REQUIRING_COMMENT and not comment:
        return {"error": f"{action} requires object_type, name, and comment"}

    if action == "schedule_downtime":
        if start_time is None or end_time is None:
            return {
                "error": "schedule_downtime requires object_type, name, comment, start_time, end_time"
            }
        return await call_tool(
            action,
            {
                "object_type": object_type,
                "name": name,
                "author": author,
                "comment": comment,
                "start_time": start_time,
                "end_time": end_time,
            },
        )

    payload: dict[str, Any] = {"object_type": object_type, "name": name}
    if action in _WRITE_ACTIONS_REQUIRING_COMMENT:
        payload["author"] = author
        payload["comment"] = comment
    return await call_tool(action, payload)

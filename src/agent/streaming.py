"""SSE (Server-Sent Events) helpers for streaming agent responses."""

import json


def sse_event(event: str, data: dict | str) -> str:
    """Format a single SSE event."""
    if isinstance(data, dict):
        data = json.dumps(data)
    return f"event: {event}\ndata: {data}\n\n"


def sse_text(text: str) -> str:
    """Stream a text chunk to the client."""
    return sse_event("text", {"content": text})


def sse_tool_start(tool_name: str, tool_input: dict) -> str:
    """Notify client that a tool call is starting."""
    return sse_event("tool_start", {"tool": tool_name, "input": tool_input})


def sse_tool_result(tool_name: str, result: dict) -> str:
    """Send tool call result to client."""
    return sse_event("tool_result", {"tool": tool_name, "result": result})


def sse_report(filename: str, format: str, download_url: str) -> str:
    """Notify client that a report is available for download."""
    return sse_event("report", {"filename": filename, "format": format, "url": download_url})


def sse_error(message: str) -> str:
    """Send an error event."""
    return sse_event("error", {"message": message})


def sse_done() -> str:
    """Signal that the stream is complete."""
    return sse_event("done", {})

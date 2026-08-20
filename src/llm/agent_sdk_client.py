"""Thin async adapter around ``claude_agent_sdk.query``.

The Claude Agent SDK is imported lazily so this module can be imported
in environments where the SDK isn't installed (CI without the optional
dependency, unit tests with a mocked sys.modules). Attempting to call
:meth:`AgentSdkClient.complete` without the SDK installed raises
:class:`AgentSdkUnavailableError`.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from src.llm.config_section import section
from src.llm.sdk_tracing import build_tracing_env
from src.llm.types import SdkResult, SdkUsage

logger = logging.getLogger(__name__)


class AgentSdkUnavailableError(RuntimeError):
    """Raised when ``claude_agent_sdk`` is not importable but was requested."""


# --------------------------------------------------------------- subprocess env
#
# The SDK runs the ``claude`` CLI as a child process, so whatever we hand it as
# ``env`` is readable by the model through the CLI's own tools. Parsec's pod
# inherits every cloud credential the app needs (``envFrom`` on
# ``parsec-cloud-credentials`` and ``parsec-aap2-credentials``: AWS secret key,
# Azure client secret, Cosmos key, the AAP2 controller passwords, plus the
# GitHub and reporting-MCP tokens), and customer-controlled text reaches the
# model verbatim via Splunk pod logs and AAP2 job stdout. So the subprocess gets
# an allowlist, not ``os.environ``.
#
# Anything an operator genuinely needs beyond this list can be added explicitly
# through ``agent.sdk.env`` in config, which is merged on top.

_ENV_ALLOWLIST_EXACT = frozenset(
    {
        # process basics
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TZ",
        # Vertex / GCP backend (region + ADC path, not app credentials)
        "CLOUD_ML_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        # Bedrock backend region. Deliberately NOT AWS_ACCESS_KEY_ID /
        # AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN — a Bedrock deployment adds
        # those through agent.sdk.env so the grant is explicit and reviewable.
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        # corporate TLS + egress
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "NODE_OPTIONS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)

#: Namespaces the CLI and its telemetry read directly.
_ENV_ALLOWLIST_PREFIXES = ("CLAUDE_", "ANTHROPIC_", "OTEL_", "MLFLOW_")

#: Parsec's own settings namespace (Dynaconf ``PARSEC_`` prefix). Every app
#: secret lands here, so it is denied unconditionally — belt and braces, since
#: no allowlist entry above starts with it.
_ENV_DENY_PREFIX = "PARSEC_"


# ------------------------------------------------------------ backend auth
#
# The CLI authenticates itself. It reads ``ANTHROPIC_*`` / ``CLAUDE_CODE_*`` and
# knows nothing about Parsec's ``anthropic.*`` settings — which arrive as
# ``PARSEC_ANTHROPIC__*`` and are denied above, that namespace being where every
# app secret lives.
#
# Nothing bridged the two until this function. The SDK runtime was proven on a
# deployment that happened to set ``CLAUDE_CODE_USE_VERTEX`` and
# ``GOOGLE_APPLICATION_CREDENTIALS`` by hand on the pod; parsec-dev runs the
# LiteLLM backend and sets neither, so the first SDK-routed question there died
# with ``the agent runtime failed: Not logged in · Please run /login``. The app
# was authenticated the whole time — the subprocess never was.
#
# Backend is read top-level rather than per-component, matching how the SDK path
# picks its model (``agent.sdk.model`` or ``anthropic.model``); it does not
# consult ``anthropic.overrides.*``.

#: Keys this module owns once a backend resolves. They are cleared from the
#: inherited environment before the derived values land, so a leftover
#: ``CLAUDE_CODE_USE_VERTEX`` cannot send the CLI to Vertex while the app itself
#: talks to a LiteLLM gateway — a split that would silently bill two accounts
#: and answer from two different models.
_MANAGED_CLI_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLOUD_ML_REGION",
    }
)

#: Ambient variables that, on their own, are enough for the CLI to authenticate.
#: Used only to decide whether an unresolvable backend is worth shouting about.
_AMBIENT_AUTH_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _incomplete(backend: str, needs: str) -> dict[str, str]:
    logger.warning(
        "agent SDK: backend %r is selected but %s is not configured — the claude "
        "CLI subprocess will fall back to whatever auth the pod environment "
        "carries, and fail with 'Not logged in' if it carries none",
        backend,
        needs,
    )
    return {}


def _litellm_cli_env(anthropic_cfg: dict[str, Any]) -> dict[str, str]:
    base_url = str(anthropic_cfg.get("litellm_base_url", "") or "").strip()
    api_key = str(anthropic_cfg.get("litellm_api_key", "") or "").strip()
    if not (base_url and api_key):
        return _incomplete("litellm", "anthropic.litellm_base_url + anthropic.litellm_api_key")
    # A bearer token rather than ANTHROPIC_API_KEY's x-api-key header: this
    # is a gateway, not the Anthropic API. LiteLLM accepts either.
    return {"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_AUTH_TOKEN": api_key}


def _vertex_cli_env(anthropic_cfg: dict[str, Any], config: Any) -> dict[str, str]:
    gcp_cfg = section(config, "gcp") or {}
    project = (
        str(anthropic_cfg.get("vertex_project_id", "") or "").strip()
        or str(gcp_cfg.get("project_id", "") or "").strip()
    )
    if not project:
        return _incomplete("vertex", "anthropic.vertex_project_id or gcp.project_id")
    env = {
        "CLAUDE_CODE_USE_VERTEX": "1",
        "ANTHROPIC_VERTEX_PROJECT_ID": project,
        "CLOUD_ML_REGION": str(anthropic_cfg.get("vertex_region", "") or "us-east5").strip(),
    }
    creds = str(anthropic_cfg.get("vertex_credentials_path", "") or "").strip()
    if creds:
        env["GOOGLE_APPLICATION_CREDENTIALS"] = creds
    return env


def _bedrock_cli_env(anthropic_cfg: dict[str, Any], config: Any) -> dict[str, str]:
    aws_cfg = section(config, "aws") or {}
    region = (
        str(anthropic_cfg.get("bedrock_region", "") or "").strip()
        or str(aws_cfg.get("region", "") or "").strip()
        or "us-east-1"
    )
    return {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": region}


def _api_cli_env(anthropic_cfg: dict[str, Any]) -> dict[str, str]:
    api_key = str(anthropic_cfg.get("api_key", "") or "").strip()
    if not api_key:
        return _incomplete("api", "anthropic.api_key")
    return {"ANTHROPIC_API_KEY": api_key}


def backend_cli_env(config: Any) -> dict[str, str]:
    """Translate Parsec's LLM backend settings into the CLI's own auth env.

    Returns an empty dict when the selected backend is not configured well
    enough to derive credentials, which leaves any hand-wired pod environment
    untouched rather than replacing working auth with nothing.

    Bedrock is the one partial case: the region is derived, but AWS credentials
    are deliberately not forwarded (see the allowlist above). A Bedrock
    deployment grants those explicitly through ``agent.sdk.env``.
    """
    anthropic_cfg = section(config, "anthropic") or {}
    backend = str(anthropic_cfg.get("backend", "api") or "api").strip().lower()

    if backend == "litellm":
        return _litellm_cli_env(anthropic_cfg)
    if backend == "vertex":
        return _vertex_cli_env(anthropic_cfg, config)
    if backend == "bedrock":
        return _bedrock_cli_env(anthropic_cfg, config)
    return _api_cli_env(anthropic_cfg)


def default_cli_path() -> str | None:
    """The pinned ``claude`` binary, or ``None`` to let the SDK choose.

    The SDK's own ``_find_cli()`` prefers a CLI bundled inside the Python wheel,
    so ``npm install -g @anthropic-ai/claude-code@<pin>`` in the Dockerfile
    decides nothing on its own — the wheel's copy floats with
    ``claude-agent-sdk`` and the pin is decorative.

    That is not cosmetic. The bundled CLI in the current image is 2.1.185 and
    sends ``anthropic-beta: thinking-token-count-2026-05-13``; Parsec's LiteLLM
    gateway forwards to Vertex, which rejects the header outright:

        API Error: 400 Unexpected value(s) `thinking-token-count-2026-05-13`
        for the `anthropic-beta` header

    So every SDK turn failed on the gateway backend while the pinned 2.1.169
    answered the same prompt fine. Preferring the pin puts the CLI version under
    the Dockerfile's control, where it can be tested before it changes.
    """
    return shutil.which("claude")


def build_subprocess_env(
    extra_env: dict[str, str] | None = None,
    backend_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the environment handed to the ``claude`` CLI subprocess.

    Allowlist-based: only the variables the CLI actually needs are inherited
    from the app process. ``backend_env`` (from :func:`backend_cli_env`) lands
    on top of the inherited set, and ``extra_env`` (tracing vars plus
    ``agent.sdk.env``) on top of that — neither is filtered, both being derived
    from config an operator wrote deliberately.
    """
    inherited = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_ENV_DENY_PREFIX)
        and (key in _ENV_ALLOWLIST_EXACT or key.startswith(_ENV_ALLOWLIST_PREFIXES))
    }
    if backend_env:
        # Config states the intent; ambient variables from some other backend
        # are stale, so they lose outright instead of blending.
        inherited = {k: v for k, v in inherited.items() if k not in _MANAGED_CLI_KEYS}
    elif not any(inherited.get(k) for k in _AMBIENT_AUTH_KEYS) and not any(
        inherited.get(k) for k in ("CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_BEDROCK")
    ):
        logger.error(
            "agent SDK: no credentials for the claude CLI subprocess — neither "
            "anthropic.* config nor the pod environment supplies any. Every SDK "
            "run will fail with 'Not logged in · Please run /login'."
        )
    return {**inherited, **(backend_env or {}), **(extra_env or {})}


@dataclass(frozen=True)
class AgentSdkConfig:
    """Resolved configuration for a single ``complete()`` call.

    Built once from Parsec config so we don't reach into Dynaconf during
    the hot path. Immutable so it's safe to share across concurrent calls.
    """

    model: str
    max_turns: int = 10
    cwd: str | None = None
    setting_sources: tuple[str, ...] = ("project",)
    extra_env: dict[str, str] = field(default_factory=dict)
    #: CLI auth derived from ``anthropic.backend`` — see :func:`backend_cli_env`.
    backend_env: dict[str, str] = field(default_factory=dict)
    # Absolute path to the ``claude`` binary. ``None`` leaves the choice to the
    # SDK, whose ``_find_cli()`` prefers a CLI bundled inside the Python wheel
    # over anything on ``PATH`` — so the ``npm install -g
    # @anthropic-ai/claude-code@<pin>`` in dockerfiles/Dockerfile does NOT
    # determine which binary runs. ``from_config`` therefore defaults this to
    # the pinned binary; see :func:`default_cli_path` for why that matters.
    cli_path: str | None = None
    # Built-in CLI tools the model may use. Parsec serves its own tools over the
    # in-process MCP bridge, so the default is just the two the SDK machinery
    # needs: ``ToolSearch`` to load deferred MCP schemas, and ``Skill`` to
    # activate a SKILL.md. Notably absent: Bash, Read, Write, Edit, WebFetch —
    # the subprocess runs in /app alongside mounted kubeconfigs and GCP
    # service-account JSON.
    builtin_tools: tuple[str, ...] = ("ToolSearch", "Skill")
    # ``dontAsk`` = "don't prompt for permissions; deny if not pre-approved".
    # A headless run has nobody to prompt, so the default mode's prompt would
    # resolve by accident rather than by policy.
    permission_mode: str = "dontAsk"
    # Wall-clock ceiling for a single complete() call. ``sdk.query()`` runs an
    # agentic loop (up to max_turns rounds of possibly-slow tools), so a hung
    # query would otherwise leak the coroutine and child CLI process. ``None``
    # disables the limit. Per-call ``complete(timeout=...)`` overrides this.
    timeout: float | None = 300.0


class AgentSdkClient:
    """Adapter that runs one agentic task via the Claude Agent SDK.

    Each call to :meth:`complete` runs a fresh SDK ``query()`` to completion
    and aggregates the streamed messages into a single :class:`SdkResult`.
    Stateless — safe to reuse across requests.

    The orchestrator's existing tool loop (``client.messages.create`` →
    inspect tool_use blocks → dispatch → append tool_result → repeat)
    is **not** modeled here. The SDK runs its own loop internally; the
    adapter surfaces the final aggregated outcome. Phase 1 wires this
    behind a feature flag for narrow tasks; the orchestrator integration
    in Phase 2 will pass full conversations through.
    """

    def __init__(self, sdk_config: AgentSdkConfig):
        self._cfg = sdk_config

    # ------------------------------------------------------------------ ctor

    @classmethod
    def from_config(cls, config: Any) -> AgentSdkClient:
        """Build from a Dynaconf-style config object.

        Reads:
        - ``anthropic.model`` (or the SDK-specific ``agent.sdk.model`` override)
        - ``anthropic.max_tool_rounds`` (renamed to ``max_turns`` for the SDK)
        - ``agent.sdk.cwd`` — working directory for the SDK subprocess
        - ``agent.sdk.setting_sources`` — defaults to ``["project"]`` so
          mounted skills under cwd are discovered
        - ``agent.sdk.timeout`` — per-call wall-clock ceiling in seconds
          (default 300; ``null``/``0`` disables it)
        - ``agent.sdk.cli_path`` — absolute path to the ``claude`` binary;
          empty falls back to the pinned binary on PATH (see
          :func:`default_cli_path`), not to the SDK's bundled copy
        """
        agent_section = section(config, "agent")
        sdk_section = section(agent_section, "sdk") if agent_section else {}
        anthropic_section = section(config, "anthropic")

        model = (
            sdk_section.get("model")
            or (anthropic_section.get("model") if anthropic_section else None)
            or "claude-sonnet-4-6"
        )
        max_turns = (
            sdk_section.get("max_turns")
            or (anthropic_section.get("max_tool_rounds") if anthropic_section else None)
            or 10
        )
        cwd = sdk_section.get("cwd") or os.getcwd()
        setting_sources_raw = sdk_section.get("setting_sources", ["project"]) or ["project"]
        # MLflow tracing env is derived from mlflow.* so the SDK subprocess
        # exports its own claude_code.* spans when tracking is enabled. An
        # explicit agent.sdk.env wins on conflict (operator override).
        explicit_env = sdk_section.get("env", {}) or {}
        extra_env = {**build_tracing_env(config), **explicit_env}
        timeout_raw = sdk_section.get("timeout", 300.0)
        timeout = float(timeout_raw) if timeout_raw else None
        cli_path = str(sdk_section.get("cli_path", "") or "").strip() or default_cli_path()
        builtin_raw = sdk_section.get("builtin_tools", None)
        builtin = (
            tuple(str(t) for t in builtin_raw)
            if isinstance(builtin_raw, list | tuple)
            else AgentSdkConfig.builtin_tools
        )
        permission_mode = (
            str(sdk_section.get("permission_mode", "") or "").strip()
            or AgentSdkConfig.permission_mode
        )

        return cls(
            AgentSdkConfig(
                model=str(model),
                max_turns=int(max_turns),
                cwd=str(cwd) if cwd else None,
                setting_sources=tuple(setting_sources_raw),
                extra_env=dict(extra_env),
                backend_env=backend_cli_env(config),
                cli_path=cli_path,
                builtin_tools=builtin,
                permission_mode=permission_mode,
                timeout=timeout,
            )
        )

    # ------------------------------------------------------------------ api

    def _build_options(
        self,
        sdk: Any,
        *,
        prompt: str,
        system: str | None,
        skills: list[str] | None,
        allowed_tools: list[str] | None,
        mcp_servers: dict[str, Any] | None,
        max_turns: int | None,
    ) -> Any:
        """Build ClaudeAgentOptions from call parameters and config.

        Note the difference between the two tool knobs, which is easy to get
        backwards: ``tools`` is the set of built-in tools that *exist*, while
        ``allowed_tools`` only says which may run *without prompting*. Setting
        ``allowed_tools`` alone therefore restricts nothing — the CLI's default
        built-ins (Bash, Write, Edit, WebFetch, …) stay available to a
        subprocess whose cwd is ``/app``. Parsec supplies its own tools over
        MCP, so the built-in set is emptied and permissions are set to deny
        anything not pre-approved.
        """
        options_kwargs: dict[str, Any] = {
            "model": self._cfg.model,
            "max_turns": max_turns or self._cfg.max_turns,
            "setting_sources": list(self._cfg.setting_sources),
            "env": build_subprocess_env(self._cfg.extra_env, self._cfg.backend_env),
            # Availability, not just auto-approval. `Skill` is re-added by the
            # SDK itself when `skills` is set, and `ToolSearch` is kept because
            # the model uses it to load deferred MCP schemas — without it, a
            # profile with ~24 bridged tools can end up unable to call any.
            "tools": list(self._cfg.builtin_tools),
            # Deny anything not explicitly allowed instead of prompting a user
            # who does not exist in a headless run.
            "permission_mode": self._cfg.permission_mode,
            # Ignore any stray .mcp.json in the image or working directory.
            "strict_mcp_config": True,
        }
        if self._cfg.cwd:
            options_kwargs["cwd"] = self._cfg.cwd
        if self._cfg.cli_path:
            options_kwargs["cli_path"] = self._cfg.cli_path
        if system:
            options_kwargs["system_prompt"] = system
        if skills is not None:
            options_kwargs["skills"] = skills
        if allowed_tools is not None:
            options_kwargs["allowed_tools"] = allowed_tools
        if mcp_servers:
            options_kwargs["mcp_servers"] = mcp_servers
        return sdk.ClaudeAgentOptions(**options_kwargs)

    async def complete(
        self,
        *,
        prompt: str,
        system: str | None = None,
        skills: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        mcp_servers: dict[str, Any] | None = None,
        max_turns: int | None = None,
        timeout: (
            float | None
        ) = None,  # NOSONAR — config parameter, not an httpx call; used with asyncio.timeout() below
    ) -> SdkResult:
        """Run a single agentic task via ``claude_agent_sdk.query()``.

        Args:
            prompt: User-visible task. Becomes the initial user message.
            system: System prompt prepended to the SDK conversation.
            skills: Skill names to enable. The SDK discovers them via
                ``setting_sources``; this list whitelists which to activate.
                ``None`` enables all discoverable skills (SDK default).
            allowed_tools: Tool name whitelist passed to ``ClaudeAgentOptions``.
            mcp_servers: MCP server config dict, passed through.
            max_turns: Override the per-call turn cap (defaults to config).
            timeout: Per-call wall-clock ceiling in seconds. Falls back to the
                configured ``agent.sdk.timeout`` (default 300) when ``None``.
                On expiry the query is cancelled and the result is marked an
                error instead of hanging and leaking the child CLI process.

        Raises:
            AgentSdkUnavailableError: if ``claude_agent_sdk`` isn't installed.
        """
        sdk = _import_sdk()
        options = self._build_options(
            sdk,
            prompt=prompt,
            system=system,
            skills=skills,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            max_turns=max_turns,
        )

        state = _new_ingest_state()
        effective_timeout = timeout if timeout is not None else self._cfg.timeout

        try:
            async with asyncio.timeout(effective_timeout):
                async for message in sdk.query(prompt=prompt, options=options):
                    if isinstance(message, sdk.AssistantMessage):
                        _ingest_assistant(sdk, message, state)
                    elif isinstance(message, sdk.UserMessage):
                        _ingest_user(sdk, message, state)
                    elif isinstance(message, sdk.ResultMessage):
                        _ingest_result(message, state)
        except TimeoutError:
            logger.warning("Claude Agent SDK query timed out (limit=%ss)", effective_timeout)
            state["is_error"] = True
            state["error_message"] = (
                f"SDK query timed out after {effective_timeout}s"
                if effective_timeout is not None
                else "SDK query timed out"
            )
        except Exception as e:
            logger.exception("Claude Agent SDK query failed")
            state["is_error"] = True
            state["error_message"] = f"{type(e).__name__}: {e}"

        return SdkResult(
            text="".join(state["text_parts"]),
            tool_invocations=tuple(state["tool_invocations"]),
            model=state["model"],
            session_id=state["session_id"],
            usage=state["usage"],
            is_error=state["is_error"],
            error_message=state["error_message"],
        )


# ---------------------------------------------------------------------- helpers


def _new_ingest_state() -> dict[str, Any]:
    """Create a fresh state dict for message ingestion."""
    return {
        "text_parts": [],
        "tool_invocations": [],
        "model": None,
        "session_id": None,
        "usage": SdkUsage(),
        "is_error": False,
        "error_message": None,
    }


def _import_sdk() -> Any:
    """Lazy import of ``claude_agent_sdk``; raises if missing."""
    try:
        return importlib.import_module("claude_agent_sdk")
    except ImportError as e:
        raise AgentSdkUnavailableError(
            "claude_agent_sdk is not installed. Install with "
            "'pip install claude-agent-sdk' to enable the SDK runtime."
        ) from e


def _ingest_assistant(sdk: Any, message: Any, state: dict[str, Any]) -> None:
    """Capture model/session_id and accumulate text + tool_use blocks.

    ``AssistantMessage.model`` is the source of truth for the running model
    name — ``ResultMessage`` (per SDK 0.2.x) carries cost/usage but has no
    ``model`` field. Last assistant message wins if the conversation switches
    models mid-flight; that's unexpected, so warn rather than silently overwrite.
    """
    msg_model = getattr(message, "model", None)
    if msg_model:
        if state["model"] and state["model"] != msg_model:
            logger.warning("Model changed mid-conversation: %s -> %s", state["model"], msg_model)
        state["model"] = msg_model
    msg_session = getattr(message, "session_id", None)
    if msg_session:
        state["session_id"] = msg_session

    for block in getattr(message, "content", []) or []:
        if isinstance(block, sdk.TextBlock):
            state["text_parts"].append(getattr(block, "text", ""))
        elif isinstance(block, sdk.ToolUseBlock):
            state["tool_invocations"].append(
                {
                    "name": getattr(block, "name", None),
                    "input": getattr(block, "input", {}),
                    "id": getattr(block, "id", None),
                }
            )


def _ingest_user(sdk: Any, message: Any, state: dict[str, Any]) -> None:
    """Pair tool_result blocks back to their tool_use entries by id."""
    for block in getattr(message, "content", []) or []:
        if not isinstance(block, sdk.ToolResultBlock):
            continue
        tool_id = getattr(block, "tool_use_id", None)
        for inv in state["tool_invocations"]:
            if inv.get("id") == tool_id and "result" not in inv:
                inv["result"] = getattr(block, "content", None)
                inv["is_error"] = bool(getattr(block, "is_error", False))
                break


def _ingest_result(message: Any, state: dict[str, Any]) -> None:
    """Capture usage/cost from ResultMessage.

    ``ResultMessage.session_id`` is a required ``str`` per the SDK spec, so take
    it when present, but don't clobber a valid id already captured from an
    AssistantMessage if it's somehow empty.
    """
    result_session = getattr(message, "session_id", None)
    if result_session:
        state["session_id"] = result_session
    raw_usage = getattr(message, "usage", None) or {}
    state["usage"] = _coerce_usage(
        raw_usage,
        total_cost_usd=float(getattr(message, "total_cost_usd", 0.0) or 0.0),
        num_turns=int(getattr(message, "num_turns", 0) or 0),
    )
    if bool(getattr(message, "is_error", False)):
        state["is_error"] = True
        state["error_message"] = getattr(message, "result", None) or "SDK reported error"


def _coerce_usage(raw: Any, *, total_cost_usd: float, num_turns: int) -> SdkUsage:
    """Normalize an SDK usage dict/object into our SdkUsage dataclass."""

    def _get(obj: Any, name: str, default: int = 0) -> int:
        if isinstance(obj, dict):
            return int(obj.get(name, default) or default)
        return int(getattr(obj, name, default) or default)

    return SdkUsage(
        input_tokens=_get(raw, "input_tokens"),
        output_tokens=_get(raw, "output_tokens"),
        cache_creation_input_tokens=_get(raw, "cache_creation_input_tokens"),
        cache_read_input_tokens=_get(raw, "cache_read_input_tokens"),
        total_cost_usd=total_cost_usd,
        num_turns=num_turns,
    )

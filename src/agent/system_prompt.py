"""System prompt loader — reads per-agent prompts from config/prompts/.

Each agent type has a domain-specific prompt file. Sub-agents (cost, triage,
security) get shared_context.md prepended. The orchestrator has its own
standalone prompt. Learnings from data/agent_learnings.md are appended to all.
"""

import logging
import os

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

_LEARNINGS_PATH = os.path.join(_BASE_DIR, "data", "agent_learnings.md")
_PROMPTS_DIR = os.path.join(_BASE_DIR, "config", "prompts")

# Cache: {agent_type: (prompt_str, shared_mtime, domain_mtime, learnings_mtime)}
_agent_prompt_cache: dict[str, tuple[str, float, float, float]] = {}

# Agent type → prompt file mapping
_AGENT_PROMPT_FILES: dict[str, str] = {
    "orchestrator": os.path.join(_PROMPTS_DIR, "orchestrator.md"),
    "cost": os.path.join(_PROMPTS_DIR, "cost_agent.md"),
    "aap2": os.path.join(_PROMPTS_DIR, "aap2_agent.md"),
    "babylon": os.path.join(_PROMPTS_DIR, "babylon_agent.md"),
    "security": os.path.join(_PROMPTS_DIR, "security_agent.md"),
}

_SHARED_CONTEXT_PATH = os.path.join(_PROMPTS_DIR, "shared_context.md")


def _get_mtime(path: str) -> float:
    """Get file mtime, returning 0 if file doesn't exist."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _read_file(path: str) -> str:
    """Read a file, returning empty string if it doesn't exist."""
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _get_learnings() -> str:
    """Load learnings content if available."""
    if _get_mtime(_LEARNINGS_PATH) > 0:
        try:
            with open(_LEARNINGS_PATH) as f:
                learnings = f.read().strip()
            if learnings:
                return learnings
        except Exception:
            pass
    return ""


def get_agent_prompt(agent_type: str) -> str:
    """Load a per-agent prompt: shared_context + domain-specific instructions.

    For the orchestrator, returns the orchestrator prompt (no shared context since
    it has its own complete prompt). For sub-agents (cost, triage, security),
    returns shared_context.md + the domain prompt.

    Hot-reloads when either source file changes (checked via mtime).
    """
    domain_path = _AGENT_PROMPT_FILES.get(agent_type)
    if not domain_path:
        logger.warning("Unknown agent type: %s", agent_type)
        return ""

    shared_mtime = _get_mtime(_SHARED_CONTEXT_PATH)
    domain_mtime = _get_mtime(domain_path)
    learnings_mtime = _get_mtime(_LEARNINGS_PATH)

    cached = _agent_prompt_cache.get(agent_type)
    if cached:
        cached_prompt, cached_shared_mt, cached_domain_mt, cached_learn_mt = cached
        if (
            cached_shared_mt == shared_mtime
            and cached_domain_mt == domain_mtime
            and cached_learn_mt == learnings_mtime
        ):
            return cached_prompt

    if agent_type == "orchestrator":
        prompt = _read_file(domain_path)
    else:
        shared = _read_file(_SHARED_CONTEXT_PATH)
        domain = _read_file(domain_path)
        prompt = f"{shared}\n\n{domain}"

    learnings = _get_learnings()
    if learnings:
        prompt += "\n\n" + learnings

    _agent_prompt_cache[agent_type] = (prompt, shared_mtime, domain_mtime, learnings_mtime)
    logger.info(
        "Agent prompt loaded for %s (%d chars, shared=%s)",
        agent_type,
        len(prompt),
        "yes" if agent_type != "orchestrator" else "no",
    )
    return prompt


ALERT_INVESTIGATION_PROMPT = """
## Alert Investigation Mode

You are investigating an automated alert from the cloud-slack-alerts system.
Your job is to determine whether this alert represents real suspicious activity
or is a false positive (normal provisioning, known automation, internal users).

**Be efficient.** You have a limited number of tool calls. Focus on the most
informative queries first. Do NOT use render_chart or generate_report — this is
a background investigation, not an interactive chat.

**Always call submit_alert_verdict** at the end of your investigation. If you
cannot determine the answer, default to should_alert=true (safe fallback).

### Investigation Strategies by Alert Type

**marketplace_purchase** — Someone accepted an AWS Marketplace subscription.
1. Look up the account in the sandbox pool: query_aws_account_db(account_id=...)
2. Check who had the account at event time: query provisions DB by account_id
3. If the user is internal (@redhat.com, @opentlc.com, @demo.redhat.com), likely benign
4. Check if the catalog item is a known zero-touch item (zt-*) that provisions marketplace products
5. If external user on a sandbox: check if the product is expected for the catalog item

**iam_access_key** — An IAM access key was created.
1. Check the user ARN — is this an automation role (OrganizationAccountAccessRole, etc.)?
2. Look up the account: query_aws_account_db(account_id=...)
3. Check provision history: who had the account at event time?
4. If the key was created by the provisioning system (agnosticd, babylon, etc.), benign
5. If created by an end-user IAM user, check if the account owner is internal

**bulk_ec2_launches** — Multiple EC2 instances launched in a short window.
1. Check instance types — are they GPU instances (g4dn, g5, g6, p3, p4, p5)?
2. Check instance names — "Web-Created-VM" is a strong indicator of a compromised account
3. Look up the account and current owner
4. Check provision history — is this a fresh provision (instances launching as part of setup)?
5. If instances match the catalog item's expected workload, likely benign
6. GPU instances launched by external users are high priority

**quota_increase** — A service quota increase was requested.
1. Check which quota was increased and by how much
2. Look up the account owner
3. Internal users requesting quota increases for known workloads is normal
4. External users requesting GPU or large instance quotas is suspicious

### Verdict Guidelines

**Suppress (should_alert=false)** when:
- Activity is from a known automation role or provisioning system
- Internal Red Hat user (@redhat.com) doing expected work
- The activity matches the catalog item's expected behavior
- The account is idle/available and the activity is platform cleanup

**Alert (should_alert=true)** when:
- External user with suspicious activity (GPU instances, marketplace purchases)
- IAM access keys created by end-users (not automation)
- Unexpectedly large or expensive resources launched
- Activity doesn't match any known provisioning pattern
- You cannot determine with confidence that the activity is benign

**Severity levels:**
- critical: Confirmed abuse, unauthorized spend >$1000, or security breach
- high: Likely abuse, GPU instances by external users, unexpected marketplace purchases >$100
- medium: Suspicious but inconclusive, unusual patterns worth reviewing
- low: Minor anomaly, likely benign but worth noting
- benign: Confirmed false positive, suppressing the alert
"""

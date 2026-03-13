"""System prompt loader — reads from config/agent_instructions.md + data/agent_learnings.md."""

import logging
import os

logger = logging.getLogger(__name__)

_PROMPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "config",
    "agent_instructions.md",
)

_LEARNINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data",
    "agent_learnings.md",
)

# Cache for the combined prompt
_cached_prompt: str = ""
_cached_instructions_mtime: float = 0
_cached_learnings_mtime: float = 0


def _get_mtime(path: str) -> float:
    """Get file mtime, returning 0 if file doesn't exist."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def get_system_prompt() -> str:
    """Load the system prompt, including learnings if available.

    Hot-reloads when either file changes (checked via mtime).
    """
    global _cached_prompt, _cached_instructions_mtime, _cached_learnings_mtime

    inst_mtime = _get_mtime(_PROMPT_PATH)
    learn_mtime = _get_mtime(_LEARNINGS_PATH)

    if (
        _cached_prompt
        and inst_mtime == _cached_instructions_mtime
        and learn_mtime == _cached_learnings_mtime
    ):
        return _cached_prompt

    with open(_PROMPT_PATH) as f:
        prompt = f.read()

    # Append learnings if available
    if learn_mtime > 0:
        try:
            with open(_LEARNINGS_PATH) as f:
                learnings = f.read().strip()
            if learnings:
                prompt += "\n\n" + learnings
                logger.info("System prompt updated with learnings (%d chars)", len(learnings))
        except Exception:
            pass

    _cached_prompt = prompt
    _cached_instructions_mtime = inst_mtime
    _cached_learnings_mtime = learn_mtime
    return prompt


# For backward compatibility
SYSTEM_PROMPT = get_system_prompt()

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

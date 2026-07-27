---
name: abuse-account-detection
description: Detect abusive or compromised sandbox accounts using Parsec's abuse indicators — GPU and oversized instances, suspicious instance names, high-volume external users, and disposable-email signups. Use when triaging a suspected crypto-mining, compromised-credential, or quota-abuse case.
license: MIT
allowed-tools:
  - mcp__reporting__*
metadata:
  author: parsec-team
  maturity: sample
parsec:
  version: "1.0.0"
  domain: security
  requires_mcp:
    - reporting
  cost_estimate_per_call_usd: 0.05
---

# Abuse Account Detection

Identify accounts that are likely being abused (crypto-mining, compromised
credentials, quota farming) using Parsec's established indicators.

## Indicators
- **AWS GPU instances:** `g4dn.*`, `g5.*`, `g6.*`, `p3.*`, `p4.*`, `p5.*`
- **AWS large / metal:** `*.metal`, `*.96xlarge`, `*.48xlarge`, `*.24xlarge`
- **AWS Lightsail:** large Windows instances, especially in `ap-south-1`
- **Instance names:** "Web-Created-VM" is a strong compromised-account signal
- **Azure GPU:** NC, ND, NV series (meterSubCategory)
- **High-volume external users:** non-Red Hat email with 50+ provisions in 90 days
- **Disposable emails:** multiple accounts from temporary email domains

## Procedure
1. **Pull candidates.** Query the provision DB (Reporting MCP) for provisions in
   the window. Flag external users (email NOT LIKE `%@redhat.com` /
   `%@opentlc.com` / `%@demo.redhat.com`).
2. **Match indicators.** Score each candidate account against the indicators
   above. Two or more independent hits is high confidence.
3. **Confirm with cost.** A real abuse case usually shows up as a cost spike —
   cross-check with the `cost-spike-investigation` skill.
4. **Report.** List the suspect accounts, the indicators each tripped, and a
   recommended action (suspend, revoke credentials, contact owner).

## Output
A ranked list of suspect accounts with the evidence per account and a
recommended containment action.

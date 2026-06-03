---
name: valid-with-parsec
description: >
  A skill that uses the Parsec extension block to declare version,
  domain, and MCP dependencies. Use when testing the parsec extension parser.
license: MIT
allowed-tools:
  - Bash
  - Read
  - mcp__reporting__*
metadata:
  author: parsec-team
  tags: [test, fixture]
parsec:
  version: 1.0.0
  domain: cost
  requires_mcp:
    - reporting
    - github
  permissions:
    bash:
      allowed_paths:
        - /tmp
        - ./reports
  cost_estimate_per_call_usd: 0.15
---

# Valid With Parsec Extensions

Demonstrates the full extension surface.

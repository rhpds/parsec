---
name: unknown-parsec-key
description: A skill with an unknown key under the parsec block — should warn, not fail.
parsec:
  version: 1.0.0
  this_is_not_a_real_key: 42
  another_unknown:
    nested: true
---

# Unknown parsec key

Loads successfully but emits a warning for the unknown keys.

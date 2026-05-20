"""
state package
=============
Manages persistent runtime state across extraction runs.

Modules:
  - run_state : Tracks extraction run metadata (run_id, timestamps, counts)

The state directory also holds:
  - tokens.enc  : Encrypted OAuth tokens (written by TokenManager)
"""

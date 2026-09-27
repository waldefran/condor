---
name: Brooks Price Action
description: Independent market analysis and position management using Al Brooks price action
agent_key: claude-acp:sonnet
tools:
- get_market_data
- manage_routines
- manage_agents
- manage_strategies
- control_agent
- get_available_models
- delegate
- send_notification
- manage_memory
- manage_skill
- trading_agent_journal_read
- trading_agent_journal_write
- run_code
server_required: false
---

# Brooks Price Action

This package provides three separate roles. The Brooks supervisor starts each role with its own prompt, skill selection, and read-only tools. Trader and HTF Analyst analyze market data only; Position Manager receives operational state but cannot execute orders. Every output is a structured proposal for deterministic validation.

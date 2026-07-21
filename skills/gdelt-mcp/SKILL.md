---
name: gdelt-mcp
description: "GDELT Cloud geopolitical intelligence: conflict events, news stories, entity graph, energy infrastructure, macro-finance, prediction markets, and web research via MCP Progressive Discovery."
version: 0.1.0
type: extension
runtime: python3
entry: plugin.py
permissions: [net, tool, route, widget, read_settings]
env_from_settings: [GDELT_API_KEY]
when_to_use: >
  User asks about geopolitical events, conflict data, news stories, entities,
  energy infrastructure, macro-finance indicators, prediction markets, or needs
  real-time structured intelligence. Covers: armed clashes, protests, disasters,
  political events, CAMEO+ coded events, clustered news stories with article
  evidence, entity profiles, GEM energy assets, macro-finance quotes/series,
  Kalshi prediction market probabilities, and web research with source extraction.
timeout_sec: 120
ui_tab:
  tab_id: gdelt
  title: GDELT Intelligence
  icon: globe
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: form
        route: query
        method: POST
        target: gdelt_results
        fields:
          - name: tool_name
            label: Tool name
            type: text
            placeholder: "search_events"
            required: true
          - name: arguments
            label: Arguments (JSON)
            type: text
            placeholder: '{"category":"Battles","country":"Lebanon","limit":5}'
        submit_label: Execute
      - type: json
        target: gdelt_results
---

# GDELT Cloud MCP

Connect Ouroboros to the [GDELT Cloud](https://gdeltcloud.com) geopolitical
intelligence platform via its MCP (Model Context Protocol) server.

## What is GDELT Cloud?

GDELT Cloud v2 provides structured, machine-readable geopolitical data:

- **Events** — Conflict and CAMEO+ coded incidents with actors, geography,
  fatalities, Goldstein scale, and significance scores.
- **Stories** — Clustered news narratives with top article evidence and source URLs.
- **Entities** — Linked people and organizations from events and stories.
- **Energy** — GEM energy asset search (power plants, pipelines, LNG terminals)
  by tracker, status, capacity, owner, or proximity.
- **Macro Finance** — Provider-backed quotes, FX, commodities, rates, and indicators.
- **Prediction Markets** — Kalshi contract search, probabilities, and market status.
- **Web Research** — Tavily-backed web search and page extraction for corroboration.

## Requirements

1. Sign up at [gdeltcloud.com](https://gdeltcloud.com/auth/sign-up)
2. Go to Dashboard → API Keys → Create New Key
3. Copy the `gdelt_sk_...` key (shown only once!)
4. Add it in Ouroboros: Settings → Secrets → add `GDELT_API_KEY`
5. Grant the key to this skill after review

## Tools

### `gdelt_discover`
Lists available tools across all categories. Call this first to see what
the GDELT Cloud catalog offers.

### `gdelt_inspect`
Get the exact schema, parameter descriptions, enum values, and usage
guidance for a specific tool. Call before first use of any tool.

### `gdelt_query`
Execute any GDELT tool by name with JSON arguments. Supports all
categories — the extension auto-routes to the correct MCP wrapper.

## Workflow

1. `gdelt_discover()` → see the full catalog
2. `gdelt_inspect(tool_name="search_events")` → get parameter schema
3. `gdelt_query(tool_name="search_events", arguments='{"category":"Battles","country":"Lebanon","limit":5}')` → get results

## Network policy

Connects only to `gdelt-cloud-mcp.fastmcp.app` (the official GDELT Cloud
MCP server) using HTTPS with bearer token authentication.

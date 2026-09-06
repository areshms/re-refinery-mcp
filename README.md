# RE Data Refinery MCP Server

An MCP (Model Context Protocol) server that gives AI agents real-time access to scored real estate investment data for Columbus, Ohio — including property listings, flip/wholesale scores, rental yields, foreclosure auctions, and tax sale listings. Agents query the data through MCP tools and pay per lookup using x402 micropayments (USDC on Base mainnet).

## What It Does

This server exposes 13 MCP tools that let any AI agent (Claude, Cursor, Hermes, etc.):

- **Search property listings** — query 184+ scored Columbus, OH properties with flip scores, wholesale scores, rental yield percentages, market heat ratings, and neighborhood comparisons
- **Get property details** — full property records by ZPID including price history, tax/assessment history, and school ratings
- **Search foreclosure auctions** — 192 Franklin County foreclosure listings with auction dates, addresses, sale status, and lot sizes (sourced from PropertyOnion, refreshed daily)
- **Search tax sale auctions** — 161 Franklin County tax sale listings with the same structure
- **Query combined auctions** — all 353 auction listings in one call, filterable by city, ZIP, status, or type

All paid lookups use the **x402 protocol** — agents send USDC micropayments on Base mainnet ($0.15–$0.50 per query) and receive data in response. No subscription, no API key — just pay per query via crypto.

## Data Sources

| Source | Data | Coverage |
|--------|------|----------|
| ZillAPI | Property listings, scores, price/tax/school history | 184 Columbus, OH properties |
| PropertyOnion | Foreclosure + tax sale auction listings | 353 Franklin County listings (daily refresh) |
| Franklin County GIS | Tax delinquency, permits, zoning | Enrichment layer (ongoing) |

## Tools

| Tool | What It Returns | Price |
|------|-----------------|-------|
| `refinery_health` | API status + cached property count | Free |
| `refinery_cache_stats` | Cache freshness + neighborhood count | Free |
| `refinery_credits` | ZillAPI credit balance | Free |
| `refinery_search_properties` | Property search results with scores | $0.50 |
| `refinery_list_properties` | All cached properties with flip/wholesale/rental scores | $0.35 |
| `refinery_get_property` | Full property detail by ZPID | $0.35 |
| `refinery_get_price_history` | Price history for a property | $0.25 |
| `refinery_get_tax_history` | Tax/assessment history for a property | $0.25 |
| `refinery_get_schools` | School ratings near a property | $0.25 |
| `refinery_search_foreclosures` | 192 foreclosure auction listings | $0.15 |
| `refinery_search_tax_sales` | 161 tax sale auction listings | $0.15 |
| `refinery_search_auctions` | All 353 auction listings combined | $0.25 |
| `refinery_payment_status` | x402 payment configuration status | Free |

## Setup

```bash
cd ~/projects/re-refinery/mcp-server
uv sync
```

### Environment variables

Create `.env` in this directory:

```bash
# Optional: override the worker or local API URLs
REFINERY_BASE_URL=https://re-data-refinery.ares-hms.workers.dev
REFINERY_LOCAL_URL=http://localhost:5004

# Required for paid x402 endpoints
EVM_PRIVATE_KEY=0x...

# Optional x402 spend cap (default $1 per payment)
X402_SPEND_CAP=$1

# Set to false to disable payments and use the free local API
REFINERY_ENABLE_X402=true
```

## Run

```bash
# stdio transport (default; for Claude Desktop, Hermes, etc.)
uv run python re_refinery_mcp.py

# SSE transport
uv run python re_refinery_mcp.py --transport sse
```

## MCP Configuration

Add to your MCP client config (Claude Desktop `claude_desktop_config.json`, Cursor, etc.):

```json
{
  "mcpServers": {
    "re-refinery-mcp": {
      "command": "uv",
      "args": ["run", "python", "re_refinery_mcp.py"],
      "cwd": "/path/to/re-refinery-mcp"
    }
  }
}
```

### Claude Desktop

```json
{
  "mcpServers": {
    "re-refinery-mcp": {
      "command": "uv",
      "args": ["run", "python", "re_refinery_mcp.py"],
      "cwd": "/path/to/re-refinery-mcp",
      "env": {
        "REFINERY_BASE_URL": "https://re-data-refinery.ares-hms.workers.dev",
        "REFINERY_ENABLE_X402": "true",
        "EVM_PRIVATE_KEY": "0x..."
      }
    }
  }
}
```

## Test with MCP Inspector

```bash
npx @modelcontextprotocol/inspector \
  uv run python /Users/ares.hmsgmail.com/projects/re-refinery/mcp-server/re_refinery_mcp.py
```

## Hermes configuration

Add to `~/.hermes/config.yaml` under `mcp_servers:`

```yaml
re_refinery:
  enabled: true
  command: /Users/ares.hmsgmail.com/projects/re-refinery/mcp-server/.venv/bin/python
  args:
    - /Users/ares.hmsgmail.com/projects/re-refinery/mcp-server/re_refinery_mcp.py
  timeout: 120
```

## Notes

- The server is read-only; no tools create or modify properties.
- All paid tools support `response_format: "json"` for machine-readable output.
- If `EVM_PRIVATE_KEY` is missing and x402 is enabled, the server still starts but paid calls return a clear 402 error message.

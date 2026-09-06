# RE Data Refinery MCP Server

MCP server that lets AI agents query the [RE Data Refinery](https://re-data-refinery.ares-hms.workers.dev) for scored Columbus, OH real estate data and pay per lookup via the x402 protocol.

## What it does

- Exposes refinery endpoints as MCP tools (`refinery_*`).
- Routes paid lookups through the x402-enabled Cloudflare Worker.
- Supports automatic USDC-on-Base micropayments when `EVM_PRIVATE_KEY` is set.
- Falls back to the free local API (`localhost:5004`) when `REFINERY_ENABLE_X402=false`.

## Tools

| Tool | Endpoint | Price |
|------|----------|-------|
| `refinery_health` | `GET /health` | Free |
| `refinery_cache_stats` | `GET /cache/stats` | Free |
| `refinery_credits` | `GET /credits` | Free |
| `refinery_search_properties` | `GET /search` | $0.50 |
| `refinery_list_properties` | `GET /properties` | $0.35 (live) / free (cached) |
| `refinery_get_property` | `GET /properties/{zpid}` | $0.35 |
| `refinery_get_price_history` | `GET /properties/{zpid}/price-history` | $0.25 |
| `refinery_get_tax_history` | `GET /properties/{zpid}/tax-history` | $0.25 |
| `refinery_get_schools` | `GET /properties/{zpid}/schools` | $0.25 |
| `refinery_search_foreclosures` | `GET /foreclosures` | $0.15 |
| `refinery_search_tax_sales` | `GET /tax-sales` | $0.15 |
| `refinery_search_auctions` | `GET /auctions` | $0.25 |
| `refinery_payment_status` | (status) | Free |

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

#!/usr/bin/env python3
"""
MCP Server for the Real Estate Data Refinery (re-refinery-mcp).

Provides AI agents with read-only tools to query scored Columbus, OH property
data. All paid lookups are routed through the x402-enabled Cloudflare Worker so
agents can pay per request via USDC on Base.

Transport: stdio (default) for local MCP clients; supports Hermes, Claude Desktop,
Claude Code, ChatGPT Desktop, etc.

Configuration (environment):
  REFINERY_BASE_URL       Worker or local API base URL (default: https://re-data-refinery.ares-hms.workers.dev)
  REFINERY_LOCAL_URL      Local API base for fallback/cached reads (default: http://localhost:5004)
  EVM_PRIVATE_KEY         Private key for x402 USDC-on-Base payments (required for paid endpoints)
  X402_SPEND_CAP          Max per-payment USD cap (default: $1)
  REFINERY_ENABLE_X402    'true' (default) to pay via x402; 'false' to use free local API
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Load .env from project root or current dir
# ---------------------------------------------------------------------------
_ENV_PATHS = [
    Path(__file__).resolve().parent / ".env",
    Path.cwd() / ".env",
]
for _env_path in _ENV_PATHS:
    if _env_path.exists():
        load_dotenv(_env_path)
        break

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
REFINERY_WORKER_URL = os.getenv(
    "REFINERY_BASE_URL", "https://re-data-refinery.ares-hms.workers.dev"
)
REFINERY_LOCAL_URL = os.getenv("REFINERY_LOCAL_URL", "http://localhost:5004")
EVM_PRIVATE_KEY = os.getenv("EVM_PRIVATE_KEY", "")
X402_SPEND_CAP = os.getenv("X402_SPEND_CAP", "$1")
ENABLE_X402 = os.getenv("REFINERY_ENABLE_X402", "true").lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP("re_refinery_mcp")


# ---------------------------------------------------------------------------
# Output format enum (shared)
# ---------------------------------------------------------------------------
class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


# ---------------------------------------------------------------------------
# x402 client setup (lazy, optional)
# ---------------------------------------------------------------------------
class PaymentClient:
    """Lazy x402 payment client for paid Worker endpoints."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._http: Any | None = None
        self._enabled = ENABLE_X402 and bool(EVM_PRIVATE_KEY)
        self._warning: str | None = None
        if ENABLE_X402 and not EVM_PRIVATE_KEY:
            self._warning = (
                "x402 is enabled but EVM_PRIVATE_KEY is not set. "
                "Paid endpoints will fail with 402 Payment Required until a key is configured."
            )

    def _ensure(self) -> tuple[Any, Any]:
        """Initialize the x402 + httpx client on first paid call."""
        if self._client is not None:
            return self._client, self._http
        try:
            from eth_account import Account  # type: ignore
            from x402 import x402Client  # type: ignore
            from x402.http import x402HTTPClient  # type: ignore
            from x402.http.clients import x402HttpxClient  # type: ignore
            from x402.mechanisms.evm import EthAccountSigner  # type: ignore
            from x402.mechanisms.evm.exact.register import register_exact_evm_client  # type: ignore

            account = Account.from_key(EVM_PRIVATE_KEY)
            client = (
                x402Client()
                .set_spend_controls({"max_amount_per_payment": X402_SPEND_CAP})
            )
            register_exact_evm_client(client, EthAccountSigner(account))
            http_client = x402HTTPClient(client)
            paid_http = x402HttpxClient(client)
            self._client = client
            self._http = (http_client, paid_http)
            return self._client, self._http
        except Exception as exc:
            raise RuntimeError(f"Failed to initialize x402 client: {exc}") from exc

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def warning(self) -> str | None:
        return self._warning

    async def get(self, url: str, params: Dict[str, Any] | None = None) -> httpx.Response:
        """Make a GET request, paying via x402 when enabled."""
        if not self._enabled:
            async with httpx.AsyncClient(timeout=30.0) as client:
                return await client.get(url, params=params)

        _, (http_client, paid_http) = self._ensure()
        # Override timeout for settle (on-chain tx can take 30s+)
        paid_http.timeout = httpx.Timeout(60.0)
        response = await paid_http.get(url, params=params)
        await response.aread()
        return response


PAYMENT = PaymentClient()


# ---------------------------------------------------------------------------
# Shared API helpers
# ---------------------------------------------------------------------------
def _get_base_url() -> str:
    """Return the active API base URL."""
    if ENABLE_X402:
        return REFINERY_WORKER_URL.rstrip("/")
    return REFINERY_LOCAL_URL.rstrip("/")


def _handle_http_error(response: httpx.Response) -> str:
    if response.status_code == 402:
        try:
            body = response.json()
            x402 = body.get("x402", {})
            amount = x402.get("amount", "?")
            currency = x402.get("currency", "USDC")
            network = x402.get("network", "base")
            desc = x402.get("description", "per lookup")
            return (
                f"Error 402 Payment Required: this endpoint costs {amount} {currency} "
                f"on {network} ({desc}). Set EVM_PRIVATE_KEY to enable automatic x402 payments."
            )
        except Exception:
            return f"Error 402 Payment Required: {response.text[:200]}"
    if response.status_code == 429:
        return "Error 429: Rate limit exceeded. Wait before making more requests."
    if response.status_code == 404:
        return f"Error 404: Not found ({response.url}). Check the property ID or endpoint."
    return f"Error {response.status_code}: {response.text[:500]}"


async def _api_get(path: str, params: Dict[str, Any] | None = None) -> Any:
    """Call the Refinery API (x402-paid Worker or free local fallback)."""
    base = _get_base_url()
    url = f"{base}{path}"
    response = await PAYMENT.get(url, params=params)

    if response.status_code >= 400:
        raise RuntimeError(_handle_http_error(response))

    try:
        return response.json()
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON response from Refinery API: {exc}") from exc


def _format_markdown_list(title: str, items: List[Dict[str, Any]], keys: List[str]) -> str:
    lines = [f"# {title}", ""]
    for item in items:
        primary = item.get(keys[0], "Unknown")
        lines.append(f"## {primary}")
        for key in keys[1:]:
            value = item.get(key)
            if value is not None and value != "":
                lines.append(f"- **{key.replace('_', ' ').title()}**: {value}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_property_markdown(prop: Dict[str, Any]) -> str:
    lines = [
        f"# {prop.get('address', 'Property Detail')}",
        "",
        f"- **ZPID**: {prop.get('zpid')}",
        f"- **City/State/ZIP**: {prop.get('city')}, {prop.get('state')} {prop.get('zipcode')}",
        f"- **Neighborhood**: {prop.get('neighborhood', 'Unknown')}",
        f"- **Price**: ${prop.get('price'):,}" if prop.get("price") else "- **Price**: N/A",
        f"- **Zestimate**: ${prop.get('zestimate'):,}" if prop.get("zestimate") else "- **Zestimate**: N/A",
        f"- **Rent Zestimate**: ${prop.get('rent_zestimate'):,}" if prop.get("rent_zestimate") else "- **Rent Zestimate**: N/A",
        f"- **Beds/Baths/Sqft**: {prop.get('beds')} / {prop.get('baths')} / {prop.get('sqft')}",
        f"- **Year Built**: {prop.get('year_built')}",
        f"- **Days on Market**: {prop.get('days_on_market')}",
        f"- **Lot Size**: {prop.get('lot_size')} sqft",
        "",
        "## Refinery Scores",
        f"- **Flip Score**: {prop.get('flip_score')}",
        f"- **Wholesale Score**: {prop.get('wholesale_score')}",
        f"- **Rental Yield**: {prop.get('rental_yield_pct')} %",
        f"- **Market Heat**: {prop.get('market_heat')}",
    ]
    if prop.get("school_scores") is not None:
        lines.append(f"- **School Score**: {prop.get('school_scores')}")
    if prop.get("tax_delinquent"):
        lines.append(f"- **Tax Delinquent**: Yes")
    return "\n".join(lines)


def _format_properties(data: Any, fmt: ResponseFormat) -> str:
    props = data.get("properties", []) if isinstance(data, dict) else []
    source = data.get("source", "unknown") if isinstance(data, dict) else "unknown"
    city = data.get("city", "Columbus") if isinstance(data, dict) else "Columbus"
    count = data.get("count", len(props)) if isinstance(data, dict) else len(props)

    if fmt == ResponseFormat.JSON:
        return json.dumps(data, indent=2)

    if not props:
        return f"No properties found in {city} (source: {source})."

    lines = [f"# Properties in {city}", f"Source: {source} | Count: {count}", ""]
    for prop in props[:50]:
        lines.append(
            f"- **{prop.get('address', 'N/A')}** (ZPID: {prop.get('zpid')}) — "
            f"${prop.get('price', 'N/A'):,} | Flip {prop.get('flip_score')} | "
            f"Wholesale {prop.get('wholesale_score')} | Yield {prop.get('rental_yield_pct')} % | "
            f"Heat {prop.get('market_heat')}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------
class HealthInput(BaseModel):
    """Input for refinery_health."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' (human-readable) or 'json' (raw).",
    )


class CacheStatsInput(BaseModel):
    """Input for refinery_cache_stats."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


class CreditsInput(BaseModel):
    """Input for refinery_credits."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


class SearchPropertiesInput(BaseModel):
    """Input for refinery_search_properties."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(
        default="Columbus OH undervalued",
        description="Search query, e.g. 'Columbus OH undervalued' or '43224 foreclosure'.",
        min_length=1,
        max_length=200,
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=50,
        description="Maximum number of results to return (1-50).",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )

    @field_validator("query")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class ListPropertiesInput(BaseModel):
    """Input for refinery_list_properties."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    city: str = Field(default="Columbus", description="City to filter by (default: Columbus).")
    min_price: Optional[int] = Field(
        default=None, ge=0, description="Minimum price filter."
    )
    max_price: Optional[int] = Field(
        default=None, ge=0, description="Maximum price filter."
    )
    live: bool = Field(
        default=True,
        description="If true, query live ZillAPI data (paid). If false, read cached data only (free).",
    )
    limit: int = Field(default=50, ge=1, le=150, description="Maximum results to return.")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format.")

    @field_validator("city")
    @classmethod
    def _strip_city(cls, v: str) -> str:
        return v.strip()


class PropertyDetailInput(BaseModel):
    """Input for refinery_get_property."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    zpid: str = Field(
        ...,
        description="Zillow Property ID (zpid), e.g. '33978111'.",
        min_length=1,
        max_length=20,
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format.")

    @field_validator("zpid")
    @classmethod
    def _strip_zpid(cls, v: str) -> str:
        return v.strip()


class PropertySubresourceInput(BaseModel):
    """Input for refinery_get_price_history, refinery_get_tax_history, refinery_get_schools."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    zpid: str = Field(..., description="Zillow Property ID.", min_length=1, max_length=20)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format.")

    @field_validator("zpid")
    @classmethod
    def _strip_zpid(cls, v: str) -> str:
        return v.strip()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool(
    name="refinery_health",
    annotations={
        "title": "Refinery Health Check",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def refinery_health(params: HealthInput) -> str:
    """Check the RE Data Refinery API health, cache status, and rate limits.

    Behavior: read-only, idempotent, no side effects. No authentication or payment
    required. The underlying API is rate-limited (see response for the current RPM
    cap). Use this tool first to verify connectivity before invoking paid tools.
    """
    data = await _api_get("/health")
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)

    lines = [
        "# RE Data Refinery Health",
        f"- **Status**: {data.get('status', 'unknown')}",
        f"- **API**: {data.get('api', 'Refinery API')}",
        f"- **Version**: {data.get('version', '1.0.0')}",
        f"- **Cached Properties**: {data.get('cached_properties', 0)}",
        f"- **Rate Limit**: {data.get('rate_limit_rpm', 20)} requests/min",
    ]
    if PAYMENT.warning:
        lines.append(f"\n⚠️ {PAYMENT.warning}")
    return "\n".join(lines)


@mcp.tool(
    name="refinery_cache_stats",
    annotations={
        "title": "Refinery Cache Statistics",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def refinery_cache_stats(params: CacheStatsInput) -> str:
    """Get cache statistics for the Refinery data store.

    Behavior: read-only, idempotent, no side effects. No authentication or payment
    required. Returns total cached properties, last update time, agent-funded count,
    and neighborhood coverage. Useful for deciding whether to use cached (free) or
    live (paid) data sources.
    """
    data = await _api_get("/cache/stats")
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)

    return (
        f"# Refinery Cache Stats\n\n"
        f"- **Total Cached**: {data.get('total_cached', 0)}\n"
        f"- **Last Updated**: {data.get('last_updated', 'N/A')}\n"
        f"- **Agent-Funded Count**: {data.get('agent_funded_count', 0)}\n"
        f"- **Neighborhoods**: {data.get('neighborhoods', 'N/A')}"
    )


@mcp.tool(
    name="refinery_credits",
    annotations={
        "title": "Refinery ZillAPI Credits",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def refinery_credits(params: CreditsInput) -> str:
    """Check remaining ZillAPI credit balance.

    Behavior: read-only, idempotent, no side effects. No authentication or payment
    required. Returns the upstream ZillAPI credit balance and grants for the current
    cycle. Low balances may cause live-data tools to return cached or partial data.
    """
    data = await _api_get("/credits")
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)

    credits = data.get("credits", {})
    return (
        f"# ZillAPI Credits\n\n"
        f"- **Provider**: {data.get('provider', 'ZillAPI')}\n"
        f"- **Balance**: {credits.get('balance', 'N/A')}\n"
        f"- **Granted This Cycle**: {credits.get('granted_this_cycle', 'N/A')}"
    )


@mcp.tool(
    name="refinery_search_properties",
    annotations={
        "title": "Search Refinery Properties",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def refinery_search_properties(params: SearchPropertiesInput) -> str:
    """Search for scored Columbus, OH properties using natural-language terms.

    Cost: paid lookup ($0.50 per call) charged via x402 USDC on Base when
    REFINERY_ENABLE_X402=true (default). Set REFINERY_ENABLE_X402=false to query
    the free local API (cached results only). Auth: requires EVM_PRIVATE_KEY to
    sign x402 payments; without it, paid calls return 402 Payment Required.
    Behavior: non-destructive but not idempotent — results may update the Refinery
    cache. Rate-limited by the upstream API; repeated calls may hit 429.
    """
    data = await _api_get("/search", params={"q": params.query, "limit": params.limit})
    return _format_properties(data, params.response_format)


@mcp.tool(
    name="refinery_list_properties",
    annotations={
        "title": "List Refinery Properties",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def refinery_list_properties(params: ListPropertiesInput) -> str:
    """List scored properties by city with optional price filtering.

    Cost: live=true (default) queries ZillAPI in real time and costs $0.35 per call
    via x402 USDC on Base when REFINERY_ENABLE_X402=true. Set live=false to read
    cached data only (free, no payment). Auth: requires EVM_PRIVATE_KEY for paid
    live lookups. Behavior: non-destructive but not idempotent — live queries may
    refresh the cache. Rate-limited by the upstream API.
    """
    query: Dict[str, Any] = {"city": params.city, "limit": params.limit}
    if params.min_price is not None:
        query["min_price"] = params.min_price
    if params.max_price is not None:
        query["max_price"] = params.max_price
    query["live"] = "1" if params.live else "0"

    data = await _api_get("/properties", params=query)

    props = data.get("properties", [])[: params.limit]
    if params.response_format == ResponseFormat.MARKDOWN:
        return _format_properties({**data, "properties": props}, ResponseFormat.MARKDOWN)

    return json.dumps({**data, "properties": props}, indent=2)


@mcp.tool(
    name="refinery_get_property",
    annotations={
        "title": "Get Refinery Property Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def refinery_get_property(params: PropertyDetailInput) -> str:
    """Get full scored details for a single property by ZPID.

    Cost: paid lookup ($0.35 per call) charged via x402 USDC on Base when
    REFINERY_ENABLE_X402=true (default). Set REFINERY_ENABLE_X402=false to use
    the free local API (cached data only). Auth: requires EVM_PRIVATE_KEY to sign
    x402 payments. Behavior: non-destructive but not idempotent — may enrich and
    refresh the Refinery cache with price, tax, and school data.
    """
    data = await _api_get(f"/properties/{params.zpid}")
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)
    return _format_property_markdown(data)


@mcp.tool(
    name="refinery_get_price_history",
    annotations={
        "title": "Get Refinery Price History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def refinery_get_price_history(params: PropertySubresourceInput) -> str:
    """Get the price-history timeline for a property by ZPID.

    Cost: paid lookup ($0.25 per call) charged via x402 USDC on Base when
    REFINERY_ENABLE_X402=true (default). Set REFINERY_ENABLE_X402=false to use
    cached history from the free local API. Auth: requires EVM_PRIVATE_KEY.
    Behavior: read-only for the caller; may refresh the cache on the backend.
    Rate-limited by the upstream API.
    """
    data = await _api_get(f"/properties/{params.zpid}/price-history")
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)

    payload = data.get("data", data)
    if not isinstance(payload, list) or not payload:
        return f"No price history available for ZPID {params.zpid}."
    return _format_markdown_list(
        f"Price History for ZPID {params.zpid}",
        payload,
        ["date", "price", "event"],
    )


@mcp.tool(
    name="refinery_get_tax_history",
    annotations={
        "title": "Get Refinery Tax History",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def refinery_get_tax_history(params: PropertySubresourceInput) -> str:
    """Get the tax/assessment history for a property by ZPID.

    Cost: paid lookup ($0.25 per call) charged via x402 USDC on Base when
    REFINERY_ENABLE_X402=true (default). Set REFINERY_ENABLE_X402=false to use
    cached tax history from the free local API. Auth: requires EVM_PRIVATE_KEY.
    Behavior: read-only for the caller; may refresh the cache on the backend.
    Rate-limited by the upstream API.
    """
    data = await _api_get(f"/properties/{params.zpid}/tax-history")
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)

    payload = data.get("data", data)
    if not isinstance(payload, list) or not payload:
        return f"No tax history available for ZPID {params.zpid}."
    return _format_markdown_list(
        f"Tax History for ZPID {params.zpid}",
        payload,
        ["time", "value", "taxPaid"],
    )


@mcp.tool(
    name="refinery_get_schools",
    annotations={
        "title": "Get Refinery School Ratings",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def refinery_get_schools(params: PropertySubresourceInput) -> str:
    """Get school ratings near a property by ZPID.

    Cost: paid lookup ($0.25 per call) charged via x402 USDC on Base when
    REFINERY_ENABLE_X402=true (default). Set REFINERY_ENABLE_X402=false to use
    cached school data from the free local API. Auth: requires EVM_PRIVATE_KEY.
    Behavior: read-only for the caller; may refresh the cache on the backend.
    Rate-limited by the upstream API.
    """
    data = await _api_get(f"/properties/{params.zpid}/schools")
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)

    payload = data.get("data", data)
    if not isinstance(payload, list) or not payload:
        return f"No school data available for ZPID {params.zpid}."
    return _format_markdown_list(
        f"Schools for ZPID {params.zpid}",
        payload,
        ["name", "rating", "level"],
    )


# ---------------------------------------------------------------------------
class AuctionSearchInput(BaseModel):
    """Input for auction listing queries."""
    status: str | None = Field(None, description="Filter by status: Sold, Canceled, Active, Pending, Upcoming")
    city: str | None = Field(None, description="Filter by city (e.g. Columbus, Hilliard)")
    zip: str | None = Field(None, description="Filter by ZIP code (e.g. 43211)")
    type: str | None = Field(None, description="Filter by type: Foreclosure Auction or Tax Sale")
    response_format: ResponseFormat = Field(ResponseFormat.MARKDOWN, description="Output format")


@mcp.tool(
    name="refinery_search_foreclosures",
    annotations={
        "title": "Search Franklin County Foreclosures",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def refinery_search_foreclosures(params: AuctionSearchInput) -> str:
    """Search Franklin County, OH foreclosure auction listings.

    Returns properties with auction date, address, city, ZIP, status
    (Sold/Canceled/Active/Pending/Upcoming), price, and lot size. Data is sourced
    from PropertyOnion and refreshed daily.

    Cost: paid lookup ($0.15 per call) charged via x402 USDC on Base when
    REFINERY_ENABLE_X402=true (default). Auth: requires EVM_PRIVATE_KEY.
    Behavior: read-only and idempotent. Rate-limited by the upstream API.
    """
    query = {k: v for k, v in {"status": params.status, "city": params.city, "zip": params.zip, "type": params.type}.items() if v}
    data = await _api_get("/foreclosures", params=query)
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)
    props = data.get("properties", [])
    if not props:
        return "No foreclosures found matching the criteria."
    return _format_markdown_list(
        f"Franklin County Foreclosures ({data.get('count', len(props))} results)",
        props,
        ["auction_date", "address", "city", "zip", "status", "price", "sqft"],
    )


@mcp.tool(
    name="refinery_search_tax_sales",
    annotations={
        "title": "Search Franklin County Tax Sales",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def refinery_search_tax_sales(params: AuctionSearchInput) -> str:
    """Search Franklin County, OH tax sale auction listings.

    Returns properties with auction date, address, city, ZIP, status
    (Sold/Canceled/Active/Pending/Upcoming), price, and lot size. Data is sourced
    from PropertyOnion and refreshed daily.

    Cost: paid lookup ($0.15 per call) charged via x402 USDC on Base when
    REFINERY_ENABLE_X402=true (default). Auth: requires EVM_PRIVATE_KEY.
    Behavior: read-only and idempotent. Rate-limited by the upstream API.
    """
    query = {k: v for k, v in {"status": params.status, "city": params.city, "zip": params.zip, "type": params.type}.items() if v}
    data = await _api_get("/tax-sales", params=query)
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)
    props = data.get("properties", [])
    if not props:
        return "No tax sale listings found matching the criteria."
    return _format_markdown_list(
        f"Franklin County Tax Sales ({data.get('count', len(props))} results)",
        props,
        ["auction_date", "address", "city", "zip", "status", "price", "sqft"],
    )


@mcp.tool(
    name="refinery_search_auctions",
    annotations={
        "title": "Search All Franklin County Auctions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def refinery_search_auctions(params: AuctionSearchInput) -> str:
    """Search all Franklin County, OH auction listings (foreclosures + tax sales).

    Returns combined foreclosure and tax-sale properties with auction date,
    address, city, ZIP, status (Sold/Canceled/Active/Pending/Upcoming), price, lot
    size, and listing type. Data is sourced from PropertyOnion and refreshed daily.

    Cost: paid lookup ($0.25 per call) charged via x402 USDC on Base when
    REFINERY_ENABLE_X402=true (default). Auth: requires EVM_PRIVATE_KEY.
    Behavior: read-only and idempotent. Rate-limited by the upstream API.
    """
    query = {k: v for k, v in {"status": params.status, "city": params.city, "zip": params.zip, "type": params.type}.items() if v}
    data = await _api_get("/auctions", params=query)
    if params.response_format == ResponseFormat.JSON:
        return json.dumps(data, indent=2)
    props = data.get("properties", [])
    if not props:
        return "No auction listings found matching the criteria."
    return _format_markdown_list(
        f"Franklin County Auctions ({data.get('count', len(props))} results — {data.get('foreclosures', 0)} foreclosures + {data.get('tax_sales', 0)} tax sales)",
        props,
        ["auction_date", "address", "city", "zip", "status", "price", "sqft", "type"],
    )


# ---------------------------------------------------------------------------
# Tool alias: discover pricing / payment status
# ---------------------------------------------------------------------------
@mcp.tool(
    name="refinery_payment_status",
    annotations={
        "title": "Refinery x402 Payment Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def refinery_payment_status() -> str:
    """Show whether x402 automatic payments are configured.

    Behavior: read-only, idempotent, no side effects. No payment required.
    Returns whether REFINERY_ENABLE_X402 is enabled, whether EVM_PRIVATE_KEY is set,
    the active API base URL, and the X402_SPEND_CAP. Use this before invoking paid
    tools to confirm payments will succeed.
    """
    status = {
        "x402_enabled": ENABLE_X402,
        "payment_client_enabled": PAYMENT.enabled,
        "evm_private_key_configured": bool(EVM_PRIVATE_KEY),
        "base_url": _get_base_url(),
        "spend_cap": X402_SPEND_CAP,
        "warning": PAYMENT.warning,
    }
    return json.dumps(status, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="RE Data Refinery MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport (stdio for local clients, sse for remote).",
    )
    args = parser.parse_args()
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()

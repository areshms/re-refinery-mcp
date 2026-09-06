FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml uv.lock ./
COPY re_refinery_mcp.py ./
COPY README.md ./

# Install dependencies (no playwright/chromium needed — scrapling fetchers
# are only used for local scraping scripts, not the MCP server itself)
RUN uv sync --no-dev --no-install-project

# Default to stdio transport
CMD ["uv", "run", "python", "re_refinery_mcp.py"]
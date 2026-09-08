FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml uv.lock ./
COPY re_refinery_mcp.py ./
COPY README.md ./
COPY requirements.txt ./

# Install dependencies — lightweight (httpx, mcp, python-dotenv, x402)
# No Playwright/Chromium needed; the MCP server only makes HTTP calls
RUN uv sync --no-dev --no-install-project

# Default to stdio transport
CMD ["uv", "run", "python", "re_refinery_mcp.py"]
FROM python:3.14-slim

# uv is the package manager everywhere, including in the image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# Dependency layer first so source edits don't reinstall the world.
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-install-project --no-dev 2>/dev/null || uv sync --no-install-project

COPY src ./src
RUN uv sync --no-editable 2>/dev/null || true

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "sellowl.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/src"]

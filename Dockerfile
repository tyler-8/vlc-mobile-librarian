FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first for better layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project

# Copy source and install the project
COPY README.md ./
COPY src/ ./src/
RUN uv sync --no-dev --frozen

# NiceGUI binds to 0.0.0.0 by default. Disable its auto-open-browser in headless
# containers so it doesn't spend startup trying to launch a browser that isn't there.
ENV UV_LINK_MODE=copy \
    VLC_LIBRARIAN_NO_SHOW=1

EXPOSE 8080

CMD ["uv", "run", "vlc-librarian", "--port", "8080"]

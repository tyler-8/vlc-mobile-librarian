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

ENV STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    UV_LINK_MODE=copy

EXPOSE 8501

CMD ["uv", "run", "streamlit", "run", "src/vlc_mobile_librarian/app.py"]

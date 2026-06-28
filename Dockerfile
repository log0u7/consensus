# Context-neutral image: no URL, no key, no cert baked in.
# All configuration comes from environment variables at runtime.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY tui/ ./tui/

ENV PYTHONUNBUFFERED=1

# Default: web server (chat UI + API). Override for CLI:
#   docker compose run --rm app python -m src.pipeline "your spec"
#   docker compose run --rm app python -m src.rag --index /app/docs-projet
EXPOSE 8000
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]

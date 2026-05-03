FROM python:3.13-slim

WORKDIR /app

# System deps for healthchecks + cert bundle
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps first (cache-friendly)
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .

# Default DB path inside the persistent volume
ENV CONDUCTOR_DB=/data/conductor.duckdb
RUN mkdir -p /data

# Railway sets $PORT
ENV PORT=8000

EXPOSE 8000

# Entrypoint handles optional one-time DB seed from $SEED_DB_URL.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# Bind to 0.0.0.0 so Railway's proxy can reach us.
# CLI honors $PORT + $HOST from env (set by Railway on the web service).
CMD ["python", "-m", "conductor.cli", "--db", "/data/conductor.duckdb", "politics", "web", "--host", "0.0.0.0"]

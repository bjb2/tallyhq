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

# Bind to 0.0.0.0 so Railway's proxy can reach us. Use $PORT.
CMD ["sh", "-c", "python -m conductor.cli --db ${CONDUCTOR_DB} politics web --host 0.0.0.0 --port ${PORT}"]

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System dependencies:
# - sqlite3 CLI: useful for backups/debugging inside container
RUN apt-get update && apt-get install -y --no-install-recommends \
      sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY pytest.ini ./pytest.ini

# Create data directory for the database and temp files
RUN mkdir -p /app/data

# Run as non-root user for security
RUN useradd -m -u 1000 botuser \
    && chown -R botuser:botuser /app
USER botuser

# Health check: verify DB is openable and Telegram API is reachable
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python scripts/healthcheck.py

CMD ["python", "-u", "src/main.py"]


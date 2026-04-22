FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install runtime Python dependencies (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY scripts/ ./scripts/

# Create data directory for the database and temp files
RUN mkdir -p /app/data

# Run as non-root user for security
RUN useradd -m -u 1000 botuser \
    && chown -R botuser:botuser /app


# --- Development image (adds test deps + pytest config). Build with --target dev ---
FROM base AS dev

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY pytest.ini ./pytest.ini
RUN chown botuser:botuser /app/pytest.ini

USER botuser
CMD ["python", "-u", "src/main.py"]


# --- Production image (default target when no --target is given) ---
FROM base AS prod
USER botuser

# Health check: verify DB is openable and Telegram API is reachable
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python scripts/healthcheck.py

CMD ["python", "-u", "src/main.py"]

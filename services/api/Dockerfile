# ---- builder stage ----
FROM python:3.11-slim AS builder

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- runtime stage ----
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages and binaries from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Create non-root user
RUN addgroup --system app && adduser --system --ingroup app app

# Copy application source
COPY potluck_api/ potluck_api/
COPY alembic.ini .

# Own everything by the app user
RUN chown -R app:app /app

USER app

EXPOSE 8080

CMD ["uvicorn", "potluck_api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]

# ---- builder stage ----
FROM python:3.11-slim AS builder

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- runtime stage ----
FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

RUN addgroup --system app && adduser --system --ingroup app app

COPY coordinator/ coordinator/
COPY alembic.ini .

RUN chown -R app:app /app

USER app

EXPOSE 8400

CMD ["uvicorn", "coordinator.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8400"]

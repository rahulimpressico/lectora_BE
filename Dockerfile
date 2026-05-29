# Lectora backend — dev API (default) or production API + worker via compose profiles.
FROM python:3.12-slim

WORKDIR /app

# pyodbc + python-docx/lxml build deps + LibreOffice for DOCX→PDF conversion
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        unixodbc \
        unixodbc-dev \
        gcc \
        libxml2-dev \
        libxslt1-dev \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini alembic/ ./
COPY lectora_backend/ lectora_backend/

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]

# Dev API: full pipeline via in-memory jobs (matches local `dev_app`)
CMD ["uvicorn", "lectora_backend.dev_app:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]

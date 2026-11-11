FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DOCUMENT_ROOT=/private/documents
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY invoicedesk invoicedesk
COPY migrations migrations
COPY alembic.ini policy.yaml ./
RUN useradd --uid 10001 --create-home pilot && mkdir -p /private/documents /private/extraction-cache && chown -R pilot:pilot /private
USER 10001:10001
CMD ["uvicorn", "invoicedesk.interfaces.api:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p templates

COPY main.py .
COPY templates/ templates/

RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

EXPOSE 8443

# Use shell form to handle environment variable
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8443}

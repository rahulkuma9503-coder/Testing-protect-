FROM python:3.11-slim

WORKDIR /app

# Install system dependencies including DNS tools
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    dnsutils \
    iputils-ping \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Update CA certificates
RUN update-ca-certificates

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p templates

COPY main.py .
COPY templates/ templates/

RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

EXPOSE 10000

# Use shell form for environment variable expansion
CMD uvicorn main:app --host 0.0.0.0 --port $PORT

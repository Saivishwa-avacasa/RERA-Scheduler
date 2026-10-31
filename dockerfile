FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# CockroachDB Cloud with sslmode=verify-full needs this CA (ISRG Root X1, public).
ENV PGSSLROOTCERT=/app/certs/root.crt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# .dockerignore keeps .env (the DB password), .venv and scraped data out of the image.
COPY . .

# Convert Windows CRLF line endings to Linux LF
RUN find . -type f -name "*.py" -exec sed -i 's/\r$//' {} +

ENTRYPOINT ["python"]

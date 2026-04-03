# Use the official Playwright Python image — all browser dependencies are pre-installed.
FROM mcr.microsoft.com/playwright/python:v1.50.0-noble

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install the Chromium browser used by Playwright
RUN playwright install chromium

# Copy application code
COPY . .

# Railway injects PORT at runtime; default to 8080
ENV PORT=8080
EXPOSE 8080

# Single worker — required because job state is held in memory.
# Increase timeout to 180s to allow for two Playwright logins + API calls.
CMD gunicorn app:app \
    --bind "0.0.0.0:${PORT}" \
    --workers 1 \
    --timeout 180 \
    --log-level info

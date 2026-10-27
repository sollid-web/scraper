FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY scraper.py /app/scraper.py
COPY worker/ /app/worker/

ENV LEADFORGE_WORKER_PORT=8080 \
    LEADFORGE_ARTIFACT_DIR=/data/artifacts
EXPOSE 8080
VOLUME ["/data/artifacts"]

# Starting the authenticated API never starts collection; every run requires
# an application dispatch and the operator's separate authorization reference.
CMD ["python3", "/app/worker/worker_api.py"]

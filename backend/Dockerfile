FROM python:3.12-slim

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd -g 10001 steward && useradd -u 10001 -g steward -m -d /home/steward steward

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN chmod +x /app/docker-entrypoint.sh && mkdir -p /data/fixtures && chown -R steward:steward /data

USER steward
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["api"]

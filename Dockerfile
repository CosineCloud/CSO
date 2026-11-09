FROM python:3.13-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/CosineCloud/CSO" \
      org.opencontainers.image.title="CSO"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CSO_DATA_DIR=/data \
    CSO_HOST=0.0.0.0 \
    PORT=10081

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates rclone \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 cso \
    && useradd --uid 10001 --gid cso --no-create-home --shell /usr/sbin/nologin cso \
    && mkdir /data && chown cso:cso /data && chmod 700 /data
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --only-binary=:all: -r requirements.txt
COPY *.py ./
COPY templates/ templates/
COPY static/ static/
USER 10001:10001
VOLUME ["/data"]
EXPOSE 10081
HEALTHCHECK --interval=30s --timeout=20s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:10081/', timeout=4).close()"
CMD ["python", "app.py"]

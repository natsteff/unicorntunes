FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FOXTUNES_DATA_DIR=/data FOXTUNES_OAUTH_BIND=0.0.0.0
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 foxtunes && useradd --uid 10001 --gid 10001 --no-create-home foxtunes && mkdir /data && chown foxtunes:foxtunes /data
ARG APP_VERSION=0.3.0-dev
ENV UNICORNTUNES_VERSION=$APP_VERSION
COPY foxtunes.py web_server.py room.py local_player.py mpd_agent.py local_loudness.py ./
COPY web/ ./web/
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8863
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8863/healthz', timeout=3)"
CMD ["python", "web_server.py", "--bind", "0.0.0.0"]

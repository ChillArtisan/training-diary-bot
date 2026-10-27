FROM python:3.12-slim

ARG APP_VERSION=dev
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Moscow APP_VERSION=$APP_VERSION
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt
# build matplotlib's font cache once, writable for the non-root runtime user
ENV MPLCONFIGDIR=/opt/mpl
RUN mkdir -p /opt/mpl && python -c "import matplotlib.font_manager" && chmod -R a+rwX /opt/mpl
# non-root runtime user; data/ is bind-mounted from the host (see README: mkdir -p data import)
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin app && mkdir -p /app/data && chown 1000:1000 /app/data
COPY diary ./diary
COPY bot ./bot
USER 1000:1000

# the scheduler touches data/heartbeat every 30 s while getUpdates succeeds; a stale file means a stuck loop or a deaf bot
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os,sys,time; sys.exit(0 if time.time() - os.path.getmtime('/app/data/heartbeat') < 300 else 1)"

CMD ["python", "-m", "bot"]

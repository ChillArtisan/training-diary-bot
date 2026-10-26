FROM python:3.12-slim

ARG APP_VERSION=dev
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Moscow APP_VERSION=$APP_VERSION
WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt
# build matplotlib's font cache once, writable for the non-root runtime user
ENV MPLCONFIGDIR=/opt/mpl
RUN mkdir -p /opt/mpl && python -c "import matplotlib.font_manager" && chmod -R a+rwX /opt/mpl
COPY diary ./diary
COPY bot ./bot

# the scheduler touches data/heartbeat every 30 s; a stale file means the event loop is stuck
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os,sys,time; sys.exit(0 if time.time() - os.path.getmtime('/app/data/heartbeat') < 300 else 1)"

CMD ["python", "-m", "bot"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg is what lets yt-dlp merge the separate video and audio tracks YouTube serves
# for anything above 720p back into a single playable file.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY openfeed ./openfeed

# The app shells out to yt-dlp/FFmpeg over internet-controlled content while holding
# the database credentials and a writable bind mount for downloads. Non-root by
# default; compose can pin the uid to the host's downloads directory owner.
RUN groupadd -g 10000 openfeed \
    && useradd -u 10000 -g openfeed -s /usr/sbin/nologin openfeed \
    && mkdir -p /data/downloads /data/postgres /data/redis \
    && chown -R openfeed:openfeed /app /data
USER openfeed

EXPOSE 8000
CMD ["uvicorn", "openfeed.main:app", "--host", "0.0.0.0", "--port", "8000"]

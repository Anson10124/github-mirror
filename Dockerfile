FROM python:3.13-alpine

# git-lfs is only used when ENABLE_LFS=true; tini forwards signals and reaps git children.
RUN apk add --no-cache git git-lfs tini ca-certificates \
 && addgroup -g 1000 mirror \
 && adduser -D -u 1000 -G mirror -h /home/mirror mirror \
 && mkdir -p /data \
 && chown mirror:mirror /data

WORKDIR /app
COPY mirror/ /app/mirror/
RUN chmod 0755 /app/mirror/askpass.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    GIT_TERMINAL_PROMPT=0

USER mirror

# Unhealthy if the last fully successful run is older than 3x SYNC_INTERVAL.
HEALTHCHECK --interval=5m --timeout=15s --start-period=2m --retries=2 \
  CMD ["python", "-m", "mirror", "healthcheck"]

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python", "-m", "mirror", "run"]

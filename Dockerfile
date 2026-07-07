FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY VERSION ./VERSION
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# The app runs as this unprivileged user. The entrypoint starts as root,
# chowns the bind-mounted /srv/data (which the host often owns as root),
# then drops to app — so writes work without manual chown on the host.
RUN useradd --uid 1000 --user-group --no-create-home app \
    && mkdir -p /srv/data && chown app:app /srv/data \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

# GIT_SHA identifies the exact commit baked into this image. It is NOT the
# human-facing "version" (that's the VERSION file above, read directly by
# the app and reported at /healthz and the page footer) — VERSION only
# bumps on a release, so it can't be used for anything that needs a value
# unique per commit. GIT_SHA covers those two cases instead: BUILD_ID
# (static-asset cache-busting) and the git_sha label (exact-commit deploy
# verification, read by deploy/autodeploy.sh via `docker inspect`).
ARG GIT_SHA=dev
ENV BUILD_ID=$GIT_SHA
LABEL git_sha=$GIT_SHA

ENV DATA_DIR=/srv/data
VOLUME /srv/data

EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# The app runs as this unprivileged user. The entrypoint starts as root,
# chowns the bind-mounted /srv/data (which the host often owns as root),
# then drops to app — so writes work without manual chown on the host.
RUN useradd --uid 1000 --user-group --no-create-home app \
    && mkdir -p /srv/data && chown app:app /srv/data \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

ARG GIT_SHA=dev
ENV APP_VERSION=$GIT_SHA

ENV DATA_DIR=/srv/data
VOLUME /srv/data

EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

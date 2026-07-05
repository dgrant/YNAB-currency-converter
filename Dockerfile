FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as an unprivileged user. uid 1000 matches the first user on a stock
# Debian host, so the bind-mounted ./data volume stays writable; if your host
# user has a different uid, chown the data dir (see DEPLOY.md).
RUN useradd --uid 1000 --user-group --no-create-home app \
    && mkdir -p /srv/data && chown app:app /srv/data
USER app

ARG GIT_SHA=dev
ENV APP_VERSION=$GIT_SHA

ENV DATA_DIR=/srv/data
VOLUME /srv/data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

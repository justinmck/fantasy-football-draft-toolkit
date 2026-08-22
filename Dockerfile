# The API only. The frontend is 1.3 MB of static files and belongs on a CDN,
# not on the box that holds the credential store - see docs/HOSTING.md.

# --- build ------------------------------------------------------------------
# A separate stage so pip's cache and any build scaffolding stay out of the
# shipped image; only the finished venv is copied forward.
FROM python:3.12-slim AS build

COPY requirements-server.txt /tmp/
# `--only-binary=:all:` refuses to build from source rather than falling back to
# it. cryptography needs rustc >= 1.83 to compile, which the slim image does not
# have - without this the build breaks confusingly on the C toolchain instead of
# saying plainly that a wheel is missing.
RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir --upgrade pip \
 && /venv/bin/pip install --no-cache-dir --no-compile --only-binary=:all: \
      -r /tmp/requirements-server.txt \
 # 138 MB of the venv, measured, is things a running server never reads.
 #
 # `--no-compile` above is the largest single piece: PYTHONDONTWRITEBYTECODE is
 # set in the runtime stage, but the venv is built *here*, where it isn't - so
 # pip left 93 MB of .pyc behind that the runtime was configured never to use.
 # The rest is scipy's and pandas' own test suites (~78 MB) and pip itself,
 # which builds the venv and is never needed once it exists.
 #
 # Deletions, not a smaller dependency set: every package in
 # requirements-server.txt is still installed and importable.
 && find /venv -name "__pycache__" -type d -prune -exec rm -rf {} + \
 && find /venv \( -path "*/tests" -o -path "*/test" \) -type d -prune -exec rm -rf {} + \
 && rm -rf /venv/lib/python3.12/site-packages/pip \
           /venv/lib/python3.12/site-packages/setuptools \
           /venv/lib/python3.12/site-packages/pkg_resources

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim

# Non-root. The volume is mounted at /data and has to be writable by this user,
# which is why it is created and chowned before the USER switch.
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /data \
 && chown app:app /data

WORKDIR /app
COPY --from=build /venv /venv

# Named explicitly rather than `COPY . .`. A wildcard copy would pull in
# data/runtime/ - the encrypted credential store and the pulled league history -
# and bake them into an image layer. .dockerignore covers that too; this is the
# second lock on the same door.
COPY --chown=app:app src/ ./src/
COPY --chown=app:app notebooks/config.py ./notebooks/config.py
# The shipped 0.4 MB subset, copied into the volume on first boot. Carries no
# account ids and no real team names - see notebooks/build_reference_db.py.
COPY --chown=app:app data/reference.db ./data/reference.db

USER app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:$PATH" \
    APP_ENV=prod \
    DATABASE_URL=sqlite:////data/fantasy_data.db \
    AUTH_DB_URL=sqlite:////data/auth.db

EXPOSE 8000

# No curl in the slim image, so probe with the interpreter that is already here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

# One worker, deliberately. Rate-limit buckets, draft sessions and the job pool
# are all in-process, so a second worker means two sets of limits and jobs that
# only one of them can report on.
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

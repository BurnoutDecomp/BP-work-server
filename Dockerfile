# BP Work Server image. Production runs it from compose.yaml under systemd
# (deploy/docker/README.md); the deploy workflow rebuilds it on every push to main.
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="bp-work-server" \
      org.opencontainers.image.source="https://github.com/BurnoutDecomp/BP-work-server"

# git reads the workflow and b5-decomp clones (imports, attribution, history);
# rclone mirrors the Drive assets merged into each build zip.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git rclone ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The app runs as the host's bpwork account, so everything it writes under
# /var/lib/bp-work-server keeps its owner. That folder is also bpwork's home, where git and
# rclone find their config.
ARG APP_UID=999
ARG APP_GID=990
RUN groupadd --system --gid "${APP_GID}" bpwork \
 && useradd --system --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home \
      --home-dir /var/lib/bp-work-server --shell /usr/sbin/nologin bpwork

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so a code-only change reuses this layer. requirements.lock pins the
# versions; a dependency it does not list installs at its latest version.
COPY pyproject.toml requirements.lock ./
RUN python -c "import tomllib; deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; open('/tmp/deps.txt', 'w').write('\n'.join(deps))" \
 && pip install -r /tmp/deps.txt -c requirements.lock \
 && rm /tmp/deps.txt

# Editable install, as before Docker: the app serves its static files from the source tree.
COPY . .
RUN pip install --no-deps -e . && pip check

USER bpwork
EXPOSE 8765
ENTRYPOINT ["bp-work-server"]
CMD ["serve", "--host", "127.0.0.1", "--port", "8765"]

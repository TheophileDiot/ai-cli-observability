FROM python:3.13.14-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

# Install dependencies via uv into a project-managed venv at /opt/venv.
# pyproject.toml + uv.lock is the single source of truth (hash-locked).
COPY --from=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock /tmp/project/
RUN cd /tmp/project && \
  UV_PROJECT_ENVIRONMENT=/opt/venv \
    uv sync --frozen --no-dev --no-install-project --compile-bytecode && \
  rm -rf /tmp/project /usr/local/bin/uv /usr/local/bin/uvx

ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app
RUN groupadd -g 10001 exporter && useradd -u 10001 -g exporter -M -s /usr/sbin/nologin exporter && \
  mkdir -p /pricing /data && chown exporter:exporter /pricing /data
COPY exporter.py .

USER exporter
EXPOSE 9101
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9101/', timeout=3)"
CMD ["python", "-u", "exporter.py"]

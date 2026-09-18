# One image, four entrypoints: api, worker, gateway, ingest.
# They differ by failure domain, scaling signal and security boundary -- not by
# build, so there is exactly one artifact to version and scan.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies only, so a source change does not re-resolve the dependency tree.
# Installing `.` here would fail: the project's source is copied below, and it
# is never installed as a package anyway -- everything runs off PYTHONPATH.
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir uv && \
    uv pip install --system --no-cache -r pyproject.toml

COPY src/ ./src/
COPY db/ ./db/

ENV PYTHONPATH=/app/src

# Nothing in this image needs to write to its own filesystem.
RUN useradd --create-home --uid 10001 faultline
USER faultline

# api serves 8080, gateway serves 8081; workers listen on neither. The health
# check therefore belongs to the service, not the image -- see docker-compose.yml.
EXPOSE 8080 8081

ENTRYPOINT ["python", "-m", "faultline.cli"]
CMD ["api"]

# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY runbooks ./runbooks
COPY policies ./policies
COPY evals ./evals
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DBR_STORE=sqlite \
    DBR_SQLITE_PATH=/data/dbremedy.db
RUN useradd --system --uid 10001 --home /app dbremedy && mkdir -p /app /data && chown dbremedy /app /data
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
USER 10001
WORKDIR /app
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["dbremedy", "serve", "--host", "0.0.0.0", "--port", "8080"]

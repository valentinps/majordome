# Majordome application image: API (+ built web UI), worker and admin CLI.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin majordome
WORKDIR /app/backend
COPY backend/pyproject.toml ./
COPY backend/majordome ./majordome
RUN pip install --no-cache-dir .
COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY --from=web /web/dist /app/web
ENV PYTHONUNBUFFERED=1 \
    MAJORDOME_WEB_DIST=/app/web
USER majordome
CMD ["uvicorn", "majordome.api.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN addgroup --system auremgrid \
    && adduser --system --ingroup auremgrid --home /app auremgrid

COPY pyproject.toml README.md LICENSE THIRD_PARTY.md ./
COPY src ./src
COPY fixtures ./fixtures

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir . \
    && mkdir -p /data \
    && chown -R auremgrid:auremgrid /app /data

USER auremgrid

VOLUME ["/data"]
EXPOSE 8791

CMD ["auremgrid", "serve", "--db", "/data/auremgrid.sqlite", "--host", "0.0.0.0", "--port", "8791"]

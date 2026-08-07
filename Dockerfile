FROM python:3.12-slim

# GDAL/GEOS/PROJ are GeoDjango's spatial library dependencies (docs/11-local-development.md
# calls this out explicitly as the most common local-dev failure point if skipped).
# libpq-dev is needed for the psycopg Postgres driver.
# libpango-1.0-0/libpangocairo-1.0-0/libcairo2/libgdk-pixbuf-2.0-0/fonts-dejavu-core
# are WeasyPrint's native rendering deps (Phase 7, docs/08-reporting.md) —
# pip installing the `weasyprint` package alone is not sufficient; it
# imports fine but fails at actual PDF-render time without these.
#
# NOTE on libgdk-pixbuf-2.0-0's name specifically: `python:3.12-slim`
# currently resolves to Debian trixie (13), not bookworm (12) — confirmed
# via web search, not assumed. Trixie dropped the `libgdk-pixbuf2.0-0`
# transitional package name that older Debian releases (and most install
# guides written before trixie shipped) still reference; the real package
# is `libgdk-pixbuf-2.0-0` (hyphen before "2.0"). Using the old name here
# caused `apt-get install` to fail with "Unable to locate package" (exit
# code 100) on a real `docker compose build` — confirmed by the user
# running it, not something this sandbox could catch (no Docker here).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    libpq-dev \
    build-essential \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

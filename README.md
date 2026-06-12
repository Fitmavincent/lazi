# Lazi API server

## Installation

Python 3.12 is required (matches the Docker image / `.python-version`).

With [uv](https://docs.astral.sh/uv/):

```
uv venv --python 3.12 .venv
uv pip install -p .venv/bin/python -r api/requirements.txt
```

or plain venv:

```
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r api/requirements.txt
```

Then install the crawler browsers (one-off):

```
playwright install firefox     # Woolies / V1 Coles crawlers
scrapling install              # Chromium for the Coles V2.5 stealth crawler
```

## Run this server

Run with `uvicorn` locally (from `api/`):

```
uvicorn main:app --reload
```

Or run inside docker via `setup.sh`:

- Spin up the server: `./setup.sh -r`
- Stop the server: `./setup.sh -s`
- Delete the container: `./setup.sh -d`

## Crawlers

The Coles half-price specials crawler (V2.5) uses Scrapling 0.4's
`AsyncStealthySession` (patched Chromium). Run it locally without R2:

```
cd api
python -m services.special_crawler.debug_coles_crawler_v2_5            # full crawl, headless
python -m services.special_crawler.debug_coles_crawler_v2_5 3 --headed # 3 pages, visible browser
```

Weekly specials reset Wednesday 00:00 AEST. Because the Fly.io machine sleeps,
data is refreshed via a **fetch-triggered background re-crawl**: any `GET` on a
data endpoint serves stored data immediately and, if it predates this week's
reset, kicks off a re-crawl in the background. Freshness and refresh
diagnostics are exposed on `GET /health`. See `design/adr-001-*.md` and
`design/adr-002-*.md`.

## Tests

```
cd api
pip install -r requirements-dev.txt
pytest
```

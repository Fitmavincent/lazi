# ADR-001: Upgrade crawling stack to Scrapling 0.4 (patched Chromium, persistent session)

**Status:** Accepted
**Date:** 2026-06-12
**Relates to:** Coles V2.5 crawler failure

## Context

The Coles V2.5 crawler (`coles_crawler_v2_5.py`) stopped producing data — the
production R2 file was last written 2026-03-10. The crawler was pinned to
`scrapling==0.2.99` + `camoufox==0.4.11` (a stealth Firefox build, last
fingerprint refresh early 2026).

Local reproduction on 2026-06-12 showed the old stack **still works from a
residential IP** (57 tiles/page, no blocks over 5 sequential pages). The
production failure is therefore environmental: Fly.io `syd` datacenter IPs get
much stricter Imperva/Incapsula scrutiny, where an outdated browser
fingerprint (old Firefox version reported by stale Camoufox) is a strong
block signal. A second compounding failure is that the machine sleeps, so the
in-process cron never fires (see ADR-002).

## Decision

Upgrade to `scrapling[fetchers]==0.4.9`:

1. **Engine change.** Scrapling 0.4's `StealthyFetcher` dropped Camoufox and
   now drives a patched Chromium via `patchright` with current, rotating
   browser fingerprints (browserforge + apify fingerprint datapoints).
   Camoufox is removed from requirements and the Docker image.
2. **Persistent session.** Use the new `AsyncStealthySession` so the whole
   crawl (warmup + all pages) happens in **one browser instance** with one
   consistent fingerprint and accumulated cookies — the V2.5 design goal that
   0.2.99 could not deliver (it launched ~20 independent browsers per crawl,
   a clear bot signal, and ~3× slower).
3. **Locale pinning.** `locale="en-AU"`, `timezone_id="Australia/Sydney"` to
   match an Australian shopper (Fly syd region also keeps geo-consistency).
4. **Early termination.** Stop crawling when a page yields no *new* products
   (pagination end) or after 3 consecutive failed pages — keeps the crawl
   short (~6 min full catalogue, ~20s/page), which matters because Fly can
   stop the machine a few minutes after the triggering HTTP request finishes.

### Alternatives considered

- **nodriver / zendriver** (CDP-direct, benchmark leader 2026): strongest
  stealth results, but a full rewrite of fetch + extraction layers and no
  built-in selector/parser conveniences; Scrapling 0.4 already passed local
  verification against Coles, so the migration cost isn't justified now.
- **Camoufox refresh only:** Camoufox upstream updates are infrequent
  (0.4.11 unchanged since early 2026) — the stale-fingerprint problem recurs.
- **Residential proxies / Firecrawl etc.:** explicitly out of scope per
  project owner (cost not justified by weekly crawl frequency). Remains the
  V3 escalation path in `coles-crawler-v3-design.md`.

## Supply-chain review (2026-06-12)

Checked via OSV.dev and web search:

| Package | Result |
|---|---|
| scrapling 0.4.9 | No known vulnerabilities or compromise reports |
| patchright 1.60.1 | No known vulnerabilities |
| playwright 1.60.0 | No known vulnerabilities |
| camoufox | (removed anyway) no known issues |
| curl_cffi 0.14.0 (old pin) | **GHSA-3vpc-4p9p-47hc** (bundled libcurl), **GHSA-qw2m-4pqf-rmpp** (SSRF) → bumped to 0.15.0, which OSV reports clean |
| setuptools 70.0.0 (old pin) | CVE-2025-47273 path traversal → bumped to >=78.1.1 |
| APScheduler 3.10.1 (old pin) | Not a CVE but a deploy-breaker: imports `pkg_resources`, which setuptools 82 removed — the Docker build (`pip install --upgrade setuptools`) would crash at startup. Bumped to 3.11.2 (uses importlib.metadata; the only OSV advisory GHSA-9cfw-f3f9-7mm7 affects 4.0 alphas only). Verified via clean-room install + full test suite + boot test |

`scrapling install` downloads Chromium through the official Playwright
distribution channel; patchright shares the same `ms-playwright` browser
cache (verified locally).

## Consequences

- Requirements slim down (camoufox, aiohttp, pytest-playwright removed from
  prod requirements; dev/test deps moved to `requirements-dev.txt`).
- Docker image build: `playwright install --with-deps firefox` (Woolies/V1
  crawlers) + `scrapling install` (Chromium + OS deps). Camoufox fetch step
  removed — smaller image.
- Scrapling 0.4 API differences handled in code: `css_first()` removed
  (now `css(...).first` via a `first()` helper), `Response.__str__` no longer
  returns HTML (use `.html_content`), fetch kwargs `humanize/geoip/
  disable_ads` removed (humanisation is built in).
- V1/V2 Coles crawler classes remain only as R2 readers; their crawl code is
  superseded and their `/sync` endpoints now route to the V2.5 crawler
  (see ADR-002 for the storage mirroring that keeps their GET endpoints fresh).
- Risk: production Fly IP may still be blocked despite fresh fingerprints —
  cannot be verified before deploying. Mitigated by retry/backoff, the
  stale-data trigger retrying on later fetches (ADR-002), and the V3
  escalation path if blocks persist.

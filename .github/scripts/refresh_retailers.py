"""Wake the production API, request stale refreshes and verify their saved data."""

import argparse
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


ENDPOINTS = {
    "coles": "/coles-data-v2-5",
    "woolies": "/woolies-data",
    "chemist_warehouse": "/chemist-warehouse-data",
    "priceline": "/priceline-data",
}


def refresh(api_url, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(api_url + "/health", timeout=30) as response:
                freshness = json.load(response)["data_freshness"]
        except (URLError, TimeoutError) as exc:
            print(f"Waiting for the API: {exc}", flush=True)
        else:
            stale = [name for name in ENDPOINTS if freshness[name]["is_stale"]]
            if not stale:
                print("All four retailers have current snapshots.", flush=True)
                return
            for name in stale:
                status = freshness[name]
                print(
                    f"{name}: last saved {status['synced_at']}, "
                    f"running={status['refresh_in_progress']}, queued={status['refresh_queued']}, "
                    f"last outcome={status['last_attempt_outcome']}",
                    flush=True,
                )
                if status["refresh_in_progress"] or status["refresh_queued"]:
                    continue
                age = status["last_attempt_age_seconds"]
                if age is not None and age < status["cooldown_seconds"]:
                    continue
                try:
                    with urlopen(api_url + ENDPOINTS[name], timeout=30):
                        pass
                except HTTPError as exc:
                    # A feed with no snapshot still queues work before returning 404.
                    if exc.code != 404:
                        print(f"Could not request {name}: {exc}", flush=True)
                except (URLError, TimeoutError) as exc:
                    print(f"Could not request {name}: {exc}", flush=True)
        time.sleep(35)
    raise SystemExit("Timed out before every retailer saved a current snapshot.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="https://lazi-api.fly.dev")
    parser.add_argument("--timeout", type=int, default=2700)
    args = parser.parse_args()
    refresh(args.api_url.rstrip("/"), args.timeout)

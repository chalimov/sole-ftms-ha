#!/usr/bin/env python
"""
Home Assistant log watcher.

Usage:
    python ha-logs.py                  # Show last 50 lines (all logs)
    python ha-logs.py -n 200           # Show last 200 lines
    python ha-logs.py -f               # Follow (tail -f style), all logs
    python ha-logs.py -f -g sole       # Follow, grep for "sole"
    python ha-logs.py -g ftms          # Last 50 lines matching "ftms"
    python ha-logs.py -g ftms -n 500   # Last 500 lines matching "ftms"
    python ha-logs.py --all            # Dump ALL logs to stdout
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request

HA_URL = "http://homeassistant.local:8123"
HA_USER = "chalimov"
HA_PASS = "Chalimov77!"

# Exclude noisy lines by default
_NOISE = {"homeassistant.loader"}


def get_token():
    req = urllib.request.Request(
        f"{HA_URL}/auth/login_flow",
        data=json.dumps({"client_id": f"{HA_URL}/", "handler": ["homeassistant", None], "redirect_uri": f"{HA_URL}/"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    flow_id = json.loads(urllib.request.urlopen(req).read())["flow_id"]
    req = urllib.request.Request(
        f"{HA_URL}/auth/login_flow/{flow_id}",
        data=json.dumps({"username": HA_USER, "password": HA_PASS, "client_id": f"{HA_URL}/"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    auth_code = json.loads(urllib.request.urlopen(req).read())["result"]
    data = urllib.parse.urlencode({"grant_type": "authorization_code", "code": auth_code, "client_id": f"{HA_URL}/"}).encode()
    req = urllib.request.Request(f"{HA_URL}/auth/token", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(urllib.request.urlopen(req).read())["access_token"]


def fetch_logs(token, range_bytes=500000):
    """Fetch logs from HA with Range header for large output."""
    req = urllib.request.Request(
        f"{HA_URL}/api/hassio/core/logs",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/plain",
            "Range": f"bytes=-{range_bytes}",
        },
    )
    raw = urllib.request.urlopen(req, timeout=30).read()
    clean = re.sub(rb"\x1b\[[0-9;]*m", b"", raw).decode("utf-8", errors="replace")
    return clean.splitlines()


def filter_lines(lines, grep=None, exclude_noise=True):
    """Filter log lines by grep pattern and noise exclusion."""
    result = []
    for line in lines:
        if exclude_noise and any(n in line for n in _NOISE):
            continue
        if grep and grep.lower() not in line.lower():
            continue
        result.append(line)
    return result


def main():
    parser = argparse.ArgumentParser(description="HA log watcher")
    parser.add_argument("-n", "--lines", type=int, default=50, help="Number of lines to show (default: 50)")
    parser.add_argument("-f", "--follow", action="store_true", help="Follow mode (poll every 2s)")
    parser.add_argument("-g", "--grep", type=str, default=None, help="Filter lines containing this string (case-insensitive)")
    parser.add_argument("--all", action="store_true", help="Dump all available logs")
    parser.add_argument("--raw", action="store_true", help="No filtering, show everything")
    parser.add_argument("--range", type=int, default=500000, help="Range bytes to request (default: 500000)")
    args = parser.parse_args()

    token = get_token()

    if args.follow:
        seen = set()
        print(f"Following HA logs{f' (grep: {args.grep})' if args.grep else ''}... Ctrl+C to stop", file=sys.stderr)
        try:
            while True:
                try:
                    lines = fetch_logs(token, args.range)
                    filtered = filter_lines(lines, args.grep, not args.raw)
                    for line in filtered:
                        h = hash(line)
                        if h not in seen:
                            seen.add(h)
                            print(line, flush=True)
                    # Prevent seen set from growing too large
                    if len(seen) > 100000:
                        seen = set(hash(l) for l in filtered[-10000:])
                except urllib.error.URLError:
                    pass  # HA might be restarting
                except Exception as e:
                    print(f"Error: {e}", file=sys.stderr)
                time.sleep(2)
        except KeyboardInterrupt:
            print("\nStopped.", file=sys.stderr)
    else:
        lines = fetch_logs(token, args.range)
        filtered = filter_lines(lines, args.grep, not args.raw)
        if args.all:
            for line in filtered:
                print(line)
            print(f"\n--- {len(filtered)} lines ---", file=sys.stderr)
        else:
            for line in filtered[-args.lines:]:
                print(line)
            print(f"\n--- Showing last {min(args.lines, len(filtered))} of {len(filtered)} matching lines ---", file=sys.stderr)


if __name__ == "__main__":
    main()

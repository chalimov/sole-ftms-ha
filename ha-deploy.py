#!/usr/bin/env python
"""
Home Assistant deployment helper for FTMS integration.
Automates: HACS redownload + HA restart + log monitoring.

Usage:
    python ha-deploy.py redownload   # HACS redownload only
    python ha-deploy.py restart      # Restart HA only
    python ha-deploy.py logs         # Tail ftms logs
    python ha-deploy.py deploy       # Full: redownload + restart + logs
    python ha-deploy.py entities     # List integration entities + states
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request

HA_URL = "http://homeassistant.local:8123"
HA_USER = "chalimov"
HA_PASS = "Chalimov77!"
HACS_REPO_ID = None  # Will be discovered automatically
HACS_REPO_NAME = "chalimov/sole-ftms-ha"
INTEGRATION = "ftms"


def get_token():
    """Authenticate via login flow and return access_token."""
    # Step 1: Initiate login flow
    req = urllib.request.Request(
        f"{HA_URL}/auth/login_flow",
        data=json.dumps({
            "client_id": f"{HA_URL}/",
            "handler": ["homeassistant", None],
            "redirect_uri": f"{HA_URL}/",
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    flow_id = json.loads(urllib.request.urlopen(req).read())["flow_id"]

    # Step 2: Submit credentials
    req = urllib.request.Request(
        f"{HA_URL}/auth/login_flow/{flow_id}",
        data=json.dumps({
            "username": HA_USER,
            "password": HA_PASS,
            "client_id": f"{HA_URL}/",
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    auth_code = json.loads(urllib.request.urlopen(req).read())["result"]

    # Step 3: Exchange for token
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": auth_code,
        "client_id": f"{HA_URL}/",
    }).encode()
    req = urllib.request.Request(
        f"{HA_URL}/auth/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = json.loads(urllib.request.urlopen(req).read())
    return resp["access_token"]


def api_get(token, path):
    """GET request to HA API."""
    req = urllib.request.Request(
        f"{HA_URL}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/plain",
        },
    )
    return urllib.request.urlopen(req).read().decode("utf-8", errors="replace")


def api_post(token, path, data=None, raw=False):
    """POST request to HA API. If raw=True, return text instead of JSON."""
    req = urllib.request.Request(
        f"{HA_URL}{path}",
        data=json.dumps(data or {}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    resp = urllib.request.urlopen(req).read().decode("utf-8", errors="replace")
    if raw:
        return resp
    return json.loads(resp)


def find_hacs_repo_id(token):
    """Find the HACS repo ID for our integration."""
    global HACS_REPO_ID
    if HACS_REPO_ID:
        return HACS_REPO_ID

    import websocket
    ws = websocket.create_connection(
        f"ws://homeassistant.local:8123/api/websocket", timeout=30
    )
    json.loads(ws.recv())  # auth_required
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    auth_resp = json.loads(ws.recv())
    if auth_resp.get("type") != "auth_ok":
        ws.close()
        raise RuntimeError(f"WebSocket auth failed: {auth_resp}")

    ws.send(json.dumps({"id": 1, "type": "hacs/repositories/list"}))
    result = json.loads(ws.recv())
    ws.close()

    for repo in result.get("result", []):
        full_name = repo.get("full_name", "")
        if HACS_REPO_NAME.lower() in full_name.lower():
            HACS_REPO_ID = repo["id"]
            print(f"  Found HACS repo: {full_name} (ID: {HACS_REPO_ID})")
            return HACS_REPO_ID

    raise RuntimeError(f"HACS repo '{HACS_REPO_NAME}' not found. Add it first via HACS UI.")


def hacs_redownload(token):
    """Trigger HACS redownload via WebSocket."""
    import websocket

    repo_id = find_hacs_repo_id(token)

    ws = websocket.create_connection(
        f"ws://homeassistant.local:8123/api/websocket", timeout=30
    )
    json.loads(ws.recv())  # auth_required
    ws.send(json.dumps({"type": "auth", "access_token": token}))
    auth_resp = json.loads(ws.recv())
    if auth_resp.get("type") != "auth_ok":
        print(f"WebSocket auth failed: {auth_resp}")
        ws.close()
        return False

    ws.send(json.dumps({
        "id": 1,
        "type": "hacs/repository/download",
        "repository": repo_id,
    }))
    result = json.loads(ws.recv())
    ws.close()
    return result.get("success", False)


def restart_ha(token):
    """Restart Home Assistant via service call."""
    req = urllib.request.Request(
        f"{HA_URL}/api/services/homeassistant/restart",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except urllib.error.URLError as e:
        if hasattr(e, 'code'):
            print(f"  Restart API returned HTTP {e.code}: {e.read().decode()}")
            return False
        return True
    except Exception:
        return True


def get_logs(token, keyword=INTEGRATION, lines=50):
    """Get filtered core logs."""
    raw = api_get(token, "/api/hassio/core/logs")
    clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    filtered = [l for l in clean.splitlines() if keyword.lower() in l.lower()]
    return filtered[-lines:]


def get_entities(token):
    """List integration entities and their states."""
    return api_post(token, "/api/template", {
        "template": (
            "{% for e in integration_entities('" + INTEGRATION + "') %}"
            "{{ e }}: {{ states(e) }} ({{ state_attr(e, 'friendly_name') }})\n"
            "{% endfor %}"
        )
    }, raw=True)


def cmd_redownload():
    print("Authenticating...")
    token = get_token()
    print("Triggering HACS redownload...")
    ok = hacs_redownload(token)
    print(f"Redownload: {'OK' if ok else 'FAILED'}")
    return ok


def cmd_restart():
    print("Authenticating...")
    token = get_token()
    print("Restarting HA...")
    restart_ha(token)
    print("Restart triggered. Waiting for HA to come back...")
    time.sleep(10)
    for i in range(12):
        try:
            resp = api_get(get_token(), "/api/")
            if "running" in resp.lower():
                print("HA is back up!")
                return True
        except Exception:
            pass
        time.sleep(5)
    print("HA did not come back within 60s")
    return False


def cmd_logs():
    token = get_token()
    lines = get_logs(token)
    if lines:
        print(f"\n--- Last {len(lines)} ftms log lines ---")
        for l in lines:
            print(l)
    else:
        print("No ftms log entries found.")


def cmd_entities():
    token = get_token()
    print(get_entities(token))


def cmd_deploy():
    print("=== DEPLOY START ===\n")

    print("[1/3] HACS Redownload...")
    token = get_token()
    ok = hacs_redownload(token)
    print(f"  Redownload: {'OK' if ok else 'FAILED'}")
    if not ok:
        print("Aborting.")
        return

    print("\n[2/3] Restarting HA...")
    token = get_token()
    ok = restart_ha(token)
    if not ok:
        print("  Restart failed! Aborting.")
        return
    print("  Restart triggered. Waiting for HA to go down...")
    time.sleep(5)
    ha_went_down = False
    for i in range(6):
        try:
            urllib.request.urlopen(f"{HA_URL}/api/", timeout=3)
        except Exception:
            ha_went_down = True
            print("  HA is down. Waiting for it to come back...")
            break
        time.sleep(5)
    if not ha_went_down:
        print("  WARNING: HA never went down — restart may have failed!")
    time.sleep(10)
    for i in range(12):
        try:
            new_token = get_token()
            resp = api_get(new_token, "/api/")
            if "running" in resp.lower():
                print("  HA is back up!")
                break
        except Exception:
            pass
        time.sleep(5)
    else:
        print("  HA did not come back within 60s")
        return

    print("\n[3/3] Checking logs...")
    time.sleep(5)
    new_token = get_token()
    lines = get_logs(new_token, lines=20)
    if lines:
        for l in lines:
            print(f"  {l}")
    else:
        print("  No ftms log entries yet.")

    print("\n  Entities:")
    print(get_entities(new_token))
    print("\n=== DEPLOY COMPLETE ===")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "deploy"
    cmds = {
        "redownload": cmd_redownload,
        "restart": cmd_restart,
        "logs": cmd_logs,
        "entities": cmd_entities,
        "deploy": cmd_deploy,
    }
    if cmd in cmds:
        cmds[cmd]()
    else:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(cmds)}")

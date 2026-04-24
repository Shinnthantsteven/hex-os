# HEX API — How Hex Bot Updates data.json via GitHub API

This document explains how your WhatsApp Hex bot can read and write `data.json` in this repo, keeping the HEX OS dashboard in sync with commands sent over WhatsApp.

---

## Overview

`data.json` is the single source of truth for all HEX OS data. The dashboard reads it on load and polls every 60 seconds for remote changes. The bot reads, mutates, and writes it back via the GitHub Contents API.

**Endpoint:**
```
GET/PUT https://api.github.com/repos/Shinnthantsteven/hex-os/contents/data.json
```

**Auth header required on every request:**
```
Authorization: Bearer <your_github_pat>
```

The PAT needs the `repo` scope (or fine-grained `contents: write` on this repo).

---

## data.json Schema

```json
{
  "todos": [
    {
      "id": "lc2kx4r9",
      "text": "Buy groceries",
      "priority": "high",
      "done": false
    }
  ],

  "events": {
    "2026-05-01": [
      { "id": "lc2kx4r9", "name": "Labour Day" }
    ],
    "2026-05-15": [
      { "id": "lc2kx4r9", "name": "Team meeting" }
    ]
  },

  "spend": {
    "2026-05": {
      "Food": 120.50,
      "Transport": 45.00,
      "Shopping": 200.00,
      "Bills": 150.00,
      "Health": 0,
      "Fun": 60.00,
      "entries": [
        { "id": "lc2kx4r9", "cat": "Food", "amount": 20.50, "note": "lunch" }
      ]
    }
  },

  "subs": [
    {
      "id": "lc2kx4r9",
      "name": "Netflix",
      "cost": "15.99",
      "renewal": "2026-05-14"
    }
  ],

  "updated": "2026-04-25T10:30:00.000Z"
}
```

### Field notes

| Field | Format | Notes |
|---|---|---|
| `todos[].priority` | `"high"` / `"mid"` / `"low"` | |
| `events` | Object keyed by `"YYYY-MM-DD"` | Each key holds an array of events |
| `spend` | Object keyed by `"YYYY-MM"` | Dashboard auto-creates month keys |
| `spend[month].entries` | Array of individual transactions | Category totals are aggregated from entries |
| `subs[].renewal` | `"YYYY-MM-DD"` | Dashboard auto-rolls expired dates by 30 days |
| `updated` | ISO 8601 timestamp | Dashboard uses this for change detection during polling |

---

## Read–Modify–Write Pattern

Every bot command that changes data follows the same 3-step pattern. **Always GET first** — you need the file's current `sha` to PUT.

### Step 1: GET current file + sha

```bash
curl -s \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/Shinnthantsteven/hex-os/contents/data.json" \
  | jq '{sha: .sha, content: (.content | gsub("\n";"") | @base64d | fromjson)}'
```

The response includes:
```json
{
  "sha": "abc123...",
  "content": "base64-encoded JSON..."
}
```

Decode content: `base64 -d <<< "$CONTENT"` (strip newlines first).

### Step 2: Mutate the JSON in memory

Apply your change to the decoded JSON object.

### Step 3: PUT updated file

```bash
UPDATED_JSON='{ ... mutated data ... }'
CONTENT_B64=$(echo -n "$UPDATED_JSON" | base64)

curl -s -X PUT \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/Shinnthantsteven/hex-os/contents/data.json" \
  -d "{
    \"message\": \"Hex bot: !todo Buy groceries\",
    \"content\": \"$CONTENT_B64\",
    \"sha\": \"$SHA_FROM_GET\"
  }"
```

---

## Command Implementations

### `!todo [task] [priority?]`

Prepend a new item to `todos`. Priority defaults to `"mid"` if omitted.

```python
import requests, base64, json, time, random, string

GH_TOKEN = "ghp_..."
API = "https://api.github.com/repos/Shinnthantsteven/hex-os/contents/data.json"
HEADERS = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}

def hex_todo(task_text, priority="mid"):
    r = requests.get(API, headers=HEADERS).json()
    sha  = r["sha"]
    data = json.loads(base64.b64decode(r["content"].replace("\n", "")))

    uid = f"{int(time.time()*1000):x}"
    data["todos"].insert(0, {"id": uid, "text": task_text, "priority": priority, "done": False})
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

    content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
    requests.put(API, headers=HEADERS, json={
        "message": f"Hex bot: !todo {task_text}",
        "content": content,
        "sha": sha
    })
```

### `!done [task #]`

Mark a to-do item as done (by index, 1-based, from the `!todos` list).

```python
def hex_done(index_1based):
    r = requests.get(API, headers=HEADERS).json()
    sha  = r["sha"]
    data = json.loads(base64.b64decode(r["content"].replace("\n", "")))

    active = [t for t in data["todos"] if not t["done"]]
    idx = index_1based - 1
    if 0 <= idx < len(active):
        todo_id = active[idx]["id"]
        for t in data["todos"]:
            if t["id"] == todo_id:
                t["done"] = True

    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
    requests.put(API, headers=HEADERS, json={"message": "Hex bot: !done", "content": content, "sha": sha})
```

### `!spend [amount] [category]`

Add a spending entry to the current month.

```python
def hex_spend(amount, category, note=""):
    CATS = ["Food", "Transport", "Shopping", "Bills", "Health", "Fun"]
    cat = next((c for c in CATS if c.lower() == category.lower()), None)
    if not cat:
        return f"Unknown category. Use: {', '.join(CATS)}"

    from datetime import datetime
    month_key = datetime.utcnow().strftime("%Y-%m")  # e.g. "2026-05"

    r = requests.get(API, headers=HEADERS).json()
    sha  = r["sha"]
    data = json.loads(base64.b64decode(r["content"].replace("\n", "")))

    if month_key not in data["spend"]:
        data["spend"][month_key] = {c: 0 for c in CATS}
        data["spend"][month_key]["entries"] = []

    uid = f"{int(time.time()*1000):x}"
    data["spend"][month_key]["entries"].append({
        "id": uid, "cat": cat, "amount": float(amount), "note": note
    })
    data["spend"][month_key][cat] = round(data["spend"][month_key].get(cat, 0) + float(amount), 2)
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

    content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
    requests.put(API, headers=HEADERS, json={
        "message": f"Hex bot: !spend {amount} {cat}",
        "content": content,
        "sha": sha
    })
    return f"Logged ${amount} → {cat}"
```

### `!todos` — Read and reply

```python
def hex_list_todos():
    r = requests.get(API, headers=HEADERS).json()
    data = json.loads(base64.b64decode(r["content"].replace("\n", "")))
    active = [t for t in data["todos"] if not t["done"]]
    if not active:
        return "No active tasks."
    lines = [f"{i+1}. [{t['priority'].upper()}] {t['text']}" for i, t in enumerate(active)]
    return "\n".join(lines)
```

### `!budget` — Read and reply

```python
def hex_budget():
    from datetime import datetime
    month_key = datetime.utcnow().strftime("%Y-%m")

    r = requests.get(API, headers=HEADERS).json()
    data = json.loads(base64.b64decode(r["content"].replace("\n", "")))

    month = data["spend"].get(month_key, {})
    CATS  = ["Food", "Transport", "Shopping", "Bills", "Health", "Fun"]
    lines = [f"📊 Budget — {month_key}"]
    total = 0
    for cat in CATS:
        amt = month.get(cat, 0)
        total += amt
        if amt > 0:
            lines.append(f"  {cat}: ${amt:.2f}")
    lines.append(f"  ────────────")
    lines.append(f"  TOTAL: ${total:.2f}")
    return "\n".join(lines)
```

### `!event [date] [name]`  (e.g. `!event 2026-05-20 Doctor appointment`)

```python
def hex_event(date_str, event_name):
    r = requests.get(API, headers=HEADERS).json()
    sha  = r["sha"]
    data = json.loads(base64.b64decode(r["content"].replace("\n", "")))

    if date_str not in data["events"]:
        data["events"][date_str] = []
    uid = f"{int(time.time()*1000):x}"
    data["events"][date_str].append({"id": uid, "name": event_name})
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

    content = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
    requests.put(API, headers=HEADERS, json={
        "message": f"Hex bot: !event {date_str} {event_name}",
        "content": content,
        "sha": sha
    })
```

---

## Error Handling

| HTTP status | Meaning | Fix |
|---|---|---|
| `401` | Bad token | Check PAT has `repo` scope |
| `404` | File/repo not found | Check `GH_OWNER` / `GH_REPO` / `GH_FILE` |
| `409` | SHA conflict | Another write happened — GET again, retry |
| `422` | Validation error | Check JSON is valid base64 + correct `sha` |

Always re-GET before a PUT if your process holds the file longer than a few seconds, to avoid 409 conflicts.

---

## Dashboard Sync Behaviour

- **On load**: fetches `data.json` from GitHub. Falls back to `localStorage` if the token is missing or the API is unreachable.
- **On every change**: saves to `localStorage` immediately, then writes to GitHub after an 800ms debounce.
- **Every 60 seconds**: compares `data.updated` timestamp with the in-memory version. If remote is newer (e.g. the bot wrote it), the dashboard re-renders automatically.

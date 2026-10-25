# Health MCP

Apple Health → this add-on → your AI assistants.

The iPhone app **Health Auto Export** pushes your HealthKit data (steps, heart
rate, HRV, sleep, weight, workouts, …) to this add-on roughly once an hour.
The add-on stores everything in a SQLite database inside the add-on's `/data`
folder (so it is part of your Home Assistant backups) and exposes it to any
MCP-capable assistant — Claude Desktop, Claude Code, Cursor, … — over
**MCP Streamable HTTP**.

Two endpoints, one port (default `8099`):

| Endpoint | Who talks to it | Auth |
|---|---|---|
| `POST /ingest` | Health Auto Export on the phone | `Authorization: Bearer <ingest_token>` |
| `/mcp` | Your AI assistants | `Authorization: Bearer <mcp_token>` |
| `GET /health` | You / monitoring | none |

Footprint: one Python process, ~70 MB RAM, writes a few hundred rows an hour.

---

## 1. Install the add-on (local add-on, no GitHub needed)

1. In Home Assistant install the **Samba share** add-on (Settings → Add-ons →
   Add-on Store → *Samba share*), set a username/password, start it.
2. From your PC open `\\192.168.2.104\addons` (Windows Explorer → address bar).
3. Copy the whole `health_mcp` folder in there, so you end up with
   `\\192.168.2.104\addons\health_mcp\config.yaml`.
4. Settings → Add-ons → Add-on Store → ⋮ (top right) → **Check for updates**.
   *Health MCP* now appears under **Local add-ons**. Open it → **Install**.
   The first install builds the image on the Pi; on a Pi 3 expect 3–6 minutes.
5. **Configuration** tab: set `ingest_token` and `mcp_token` to two long random
   strings (or leave them empty — the add-on generates them and prints them in
   the **Log** tab on every start until you set them). Set `timezone`.
6. **Start** it, enable *Start on boot* and *Watchdog*.
7. Check: open `http://192.168.2.104:8099/health` in a browser →
   `{"ok": true, ...}`.

---

## 2. Set up Health Auto Export on the iPhone

App Store: *Health Auto Export – JSON+CSV*. It needs the paid tier for
automations.

1. Open the app → **Automations** → **+** → choose **REST API**.
2. **URL**: `http://192.168.2.104:8099/ingest`
3. **Method**: `POST`
4. **Headers**: add `Authorization` with value `Bearer <your ingest_token>`
5. **Data type**: *Health Metrics* — then add a second automation (or a second
   export in the same one, depending on app version) for **Workouts**.
6. **Format**: `JSON`. **Aggregation / period**: *Hourly* is a good default for
   steps/energy; heart rate can stay at *Hourly* too (you'll get min/avg/max per
   hour). Pick *Raw* only if you want every single sample — fine for the add-on,
   but it makes each upload much bigger.
7. **Sync cadence**: as often as the app allows (typically hourly), and switch
   on **Enabled**.
8. Select the metrics you care about (steps, active energy, heart rate, resting
   HR, HRV, sleep analysis, weight/body mass, body fat, respiratory rate, …).
9. Tap **Run now** / **Export** once while on your home Wi-Fi. The add-on log
   shows a line like `ingested 1234 samples across 9 metrics, 2 workouts`.

**Backfilling history.** Automations only send new data. For the past months
use the app's manual export with a date range and the same REST destination
(if your version offers REST as a manual-export destination); otherwise export
JSON files and POST them yourself:

```
curl -X POST http://192.168.2.104:8099/ingest \
     -H "Authorization: Bearer <ingest_token>" \
     --data-binary @HealthAutoExport-2026-01-01-2026-06-30.json
```

Re-sending overlapping data is safe — rows are upserted, never duplicated.

**Reality check on iOS background sync.** Apple decides when background work
runs. In practice uploads land within an hour or two while the phone is
charged and on Wi-Fi, but a phone that hasn't opened the app in a while may
go quiet. Opening Health Auto Export now and then keeps it honest, and the
`ingest_status` MCP tool tells you when the last upload was.

---

## 3. Connect your assistants

### Claude Code

```
claude mcp add --transport http health http://192.168.2.104:8099/mcp \
  --header "Authorization: Bearer <mcp_token>"
```

### Claude Desktop (Windows)

Claude Desktop's *Custom connectors* don't support static headers, so go
through the `mcp-remote` bridge in `claude_desktop_config.json`
(Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "health": {
      "command": "cmd",
      "args": [
        "/c", "npx", "-y", "mcp-remote",
        "http://192.168.2.104:8099/mcp",
        "--header", "Authorization:${HEALTH_MCP_AUTH}",
        "--allow-http"
      ],
      "env": { "HEALTH_MCP_AUTH": "Bearer <mcp_token>" }
    }
  }
}
```

(The env-var indirection is what `mcp-remote` recommends on Windows, where a
space in an argument gets mangled.) Restart Claude Desktop; a hammer icon with
7 tools appears.

### Cursor / other MCP clients

Any client that supports Streamable HTTP with a custom header works the same
way: URL `http://192.168.2.104:8099/mcp`, header
`Authorization: Bearer <mcp_token>`.

### From outside your home network

Nothing here is reachable from the internet, on purpose. If you later want
claude.ai or the Claude mobile app to use it, put the Pi behind Tailscale or a
Cloudflare Tunnel with HTTPS — the add-on itself needs no change.

---

## 4. The MCP tools

| Tool | What it answers |
|---|---|
| `list_metrics` | What's in the database: metric names, units, counts, first/last timestamp. Start here. |
| `latest(metric)` | Most recent sample, e.g. this morning's weight. |
| `query(metric, start, end, aggregate, limit)` | One metric over a date range, bucketed `hourly` / `daily` / `weekly` / `monthly` (sum, avg, min, max per bucket) or `raw`. |
| `daily_summary(day)` | Every metric for one calendar day. |
| `workouts(start, end)` | Workouts with duration, kcal, distance, avg/max HR. |
| `sql(query)` | Read-only SQLite for anything else — rolling averages, regressions, joins. |
| `ingest_status` | Last uploads from the phone, totals, DB size. |

Which aggregate to read is in the server's instructions to the model: `sum`
for cumulative things (steps, energy, distance), `avg` for levels (heart rate,
HRV, weight). Timestamps are local time with offset, and "daily" means local
calendar days.

Example prompts once it's connected:

* "What was my 7-day rolling average weight each week this month?"
* "Compare my resting heart rate on days after < 6 h sleep vs ≥ 7 h."
* "Back-calculate my TDEE from weight trend and logged intake for September."

---

## 5. Data & operations

* Database: `/data/health.db` inside the add-on — included in HA backups.
  Expect roughly 50–150 MB per year with hourly aggregation.
* Schema: `samples(metric, ts, ts_utc, qty, units, source, fields)` where
  `fields` is the original JSON of the data point; `workouts(...)`;
  `ingest_log(...)`.
* To wipe and start over: uninstall and reinstall the add-on — that clears
  `/data` (the Samba share does not expose add-on data folders).
* Logs: the add-on **Log** tab. Set `log_level: debug` to see every request.

## Options

| Option | Default | Meaning |
|---|---|---|
| `ingest_token` | *(generated)* | Bearer token the phone must send. |
| `mcp_token` | *(generated)* | Bearer token your assistants must send. |
| `timezone` | `Europe/Berlin` | Used for "today" defaults in the tools. |
| `log_level` | `info` | `debug` logs every HTTP request. |

#!/usr/bin/env python3
"""Collect OpenCode usage into one display-ready JSON record.

This is the merged collector for the OpenCode Tracker widget. It keeps the
per-provider token/cost aggregation, per-model 7-day daily breakdown, and
OpenCode Go limit windows of the original bash collector, and adds the extras
the local DeepSeek setup carried:

  * USD -> EUR conversion (config override, cached ECB/Frankfurter feed)
  * DeepSeek prepaid credit balance
  * per-app (project) attribution from the file paths each tool touched

Everything comes from the local OpenCode SQLite database, read-only. The only
network calls are the DeepSeek balance endpoint (when a key exists), the
OpenCode Go usage endpoint (when an `opencode-go` key exists), and the daily
USD->EUR rate; each is bounded by a short timeout and fails soft.

The record is written to stdout and follows the shape Service.qml/Model.js and
Panel.qml expect.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

AGENT_ID = "deepseek"
FX_ENDPOINT = "https://api.frankfurter.app/latest?from=USD&to=EUR"
BALANCE_ENDPOINT = "https://api.deepseek.com/user/balance"
GO_URL = "https://opencode.ai/zen/go/v1/usage"
DEFAULT_USD_TO_EUR = 0.87696
NETWORK_TIMEOUT = 5

WEEK_DAYS = 7
MONTH_DAYS = 30
PROVIDER_ROW_LIMIT = 128
MODEL_ROW_LIMIT = 1000
MESSAGE_ROW_LIMIT = 200000
PART_ROW_LIMIT = 200000
APP_LIMIT = 12
COLLECTOR_MAX_BYTES = 262144
GO_MAX_BYTES = 262144

# Folders that hold files rather than projects; work done there is ad-hoc.
USER_DIRS = {
    "Downloads", "Documents", "Desktop", "Pictures", "Videos", "Music",
    "Public", "Templates",
}

# A shell command names the directory it works in; skip flags like -rf.
PATH_TOKEN = re.compile(r"/[A-Za-z0-9_.@+\-/]+")


# ---------------------------------------------------------------- environment

def home() -> Path:
  return Path(os.path.expanduser("~"))


def cache_home() -> Path:
  return Path(os.environ.get("XDG_CACHE_HOME") or (home() / ".cache"))


def config_home() -> Path:
  return Path(os.environ.get("XDG_CONFIG_HOME") or (home() / ".config"))


def data_home() -> Path:
  return Path(os.environ.get("XDG_DATA_HOME") or (home() / ".local" / "share"))


def opencode_db_path() -> Path:
  explicit = os.environ.get("OPENCODE_DB")
  if explicit:
    return Path(explicit)
  return data_home() / "opencode" / "opencode.db"


def opencode_auth_path() -> Path:
  explicit = os.environ.get("OPENCODE_AUTH_JSON")
  if explicit:
    return Path(explicit)
  return data_home() / "opencode" / "auth.json"


def deepseek_config_path() -> Path:
  explicit = os.environ.get("DEEPSEEK_CONFIG")
  if explicit:
    return Path(explicit)
  return config_home() / "omarchy" / "agents" / "deepseek.json"


# ------------------------------------------------------------------ utilities

def number(value: Any) -> int:
  try:
    return max(0, int(round(float(value or 0))))
  except (TypeError, ValueError):
    return 0


def money(value: Any) -> float:
  try:
    amount = float(value or 0)
  except (TypeError, ValueError):
    return 0.0
  return amount if amount > 0 else 0.0


def read_json(path: Path) -> Any:
  try:
    return json.loads(path.read_text())
  except (OSError, json.JSONDecodeError, ValueError):
    return None


def valid_override_path(raw: str) -> bool:
  if not raw:
    return False
  if raw == "/dev/null":
    return True
  if not raw.startswith("/"):
    return False
  if ".." in raw:
    return False
  return True


# ------------------------------------------------------------------- fx rate

def configured_rate() -> float:
  raw = os.environ.get("DEEPSEEK_USD_TO_EUR")
  if raw:
    try:
      rate = float(raw)
      if rate > 0:
        return rate
    except (TypeError, ValueError):
      pass
  config = read_json(deepseek_config_path())
  if isinstance(config, dict):
    try:
      rate = float(config.get("usdToEur"))
      if rate > 0:
        return rate
    except (TypeError, ValueError):
      pass
  return 0.0


def fx_cache_path() -> Path:
  return cache_home() / "omarchy" / "deepseek-usage" / "fx.json"


def fx_rate() -> float:
  override = configured_rate()
  if override > 0:
    return override

  cache = fx_cache_path()
  cached_rate = 0.0
  cached_day = ""
  saved = read_json(cache)
  if isinstance(saved, dict):
    cached_rate = money(saved.get("usdToEur"))
    cached_day = str(saved.get("date") or "")

  if cached_day == date.today().isoformat() and cached_rate > 0:
    return cached_rate

  try:
    request = urllib.request.Request(
      FX_ENDPOINT,
      headers={"Accept": "application/json",
               "User-Agent": "opencode-tracker/1.3"},
    )
    with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
      payload = json.load(response)
    rate = float(payload["rates"]["EUR"])
    if rate > 0:
      try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name("." + cache.name + ".tmp")
        tmp.write_text(json.dumps({"date": date.today().isoformat(),
                                   "usdToEur": rate}))
        os.replace(tmp, cache)
      except OSError:
        pass
      return rate
  except (urllib.error.URLError, urllib.error.HTTPError, OSError,
          json.JSONDecodeError, KeyError, TypeError, ValueError):
    pass

  return cached_rate if cached_rate > 0 else DEFAULT_USD_TO_EUR


# --------------------------------------------------------------------- auth

def auth_data(auth_path: Path) -> dict[str, Any]:
  parsed = read_json(auth_path)
  return parsed if isinstance(parsed, dict) else {}


def auth_keys(auth_path: Path) -> set[str]:
  keys: set[str] = set()
  for name, value in auth_data(auth_path).items():
    if isinstance(value, str):
      if value.strip():
        keys.add(name)
    elif isinstance(value, dict):
      if any(str(value.get(field) or "").strip()
             for field in ("key", "access", "refresh")):
        keys.add(name)
  return keys


def auth_key(auth_path: Path, name: str) -> str:
  entry = auth_data(auth_path).get(name)
  if isinstance(entry, str):
    return entry.strip()
  if isinstance(entry, dict):
    return str(entry.get("key") or entry.get("access") or "").strip()
  return ""


# -------------------------------------------------------------- deepseek API

def deepseek_api_key() -> str:
  key = str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()
  if key:
    return key
  return auth_key(opencode_auth_path(), "deepseek")


def fetch_balance(api_key: str) -> dict[str, Any] | None:
  if not api_key:
    return None
  try:
    request = urllib.request.Request(
      BALANCE_ENDPOINT,
      headers={
        "Accept": "application/json",
        "Authorization": "Bearer " + api_key,
        "User-Agent": "opencode-tracker/1.3",
      },
    )
    with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
      payload = json.load(response)
  except (urllib.error.URLError, urllib.error.HTTPError, OSError,
          json.JSONDecodeError, TimeoutError):
    return None

  infos = payload.get("balance_infos") if isinstance(payload, dict) else None
  if not isinstance(infos, list) or not infos:
    return None
  info = infos[0] if isinstance(infos[0], dict) else {}
  try:
    remaining = float(info.get("total_balance") or 0)
  except (TypeError, ValueError):
    return None
  return {
    "remaining": remaining,
    "funded": 0,
    "spent": 0,
    "currency": str(info.get("currency") or "CNY"),
    "estimated": False,
  }


# ------------------------------------------------------------------ go API

def collect_go(auth_path: Path) -> dict[str, Any]:
  key = auth_key(auth_path, "opencode-go")
  if not key:
    return {"status": "No API key"}
  request = urllib.request.Request(
    GO_URL,
    headers={"Accept": "application/json",
             "Authorization": "Bearer " + key,
             "User-Agent": "opencode-tracker/1.3"},
  )
  try:
    with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
      body = response.read(GO_MAX_BYTES + 1)
  except urllib.error.HTTPError as error:
    return {"status": f"HTTP {error.code}"}
  except (urllib.error.URLError, OSError, TimeoutError):
    return {"status": "network error"}

  if len(body) > GO_MAX_BYTES:
    return {"status": "response too large"}
  try:
    payload = json.loads(body)
  except (json.JSONDecodeError, ValueError):
    return {"status": "bad response"}
  usage = payload.get("usage") if isinstance(payload, dict) else None
  if not isinstance(usage, dict) or not all(
      kind in usage for kind in ("rolling", "weekly", "monthly")):
    return {"status": "bad response"}
  return {
    "status": "ok",
    "rolling": usage["rolling"],
    "weekly": usage["weekly"],
    "monthly": usage["monthly"],
  }


# ------------------------------------------------------------------ database

def connect(db: Path) -> sqlite3.Connection:
  connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
  connection.execute("pragma busy_timeout=2000")
  return connection


def provider_rows(connection: sqlite3.Connection, now_ms: int) -> list[tuple]:
  week_cutoff = now_ms - WEEK_DAYS * 86400000
  month_cutoff = now_ms - MONTH_DAYS * 86400000
  return connection.execute(
    """
    select json_extract(data, '$.providerID'),
      round(sum(case when time_created > ? then coalesce(json_extract(data, '$.tokens.total'), 0) else 0 end)),
      round(sum(case when time_created > ? then coalesce(json_extract(data, '$.tokens.total'), 0) else 0 end)),
      round(sum(case when time_created > ? then coalesce(json_extract(data, '$.cost'), 0) else 0 end), 4),
      round(sum(case when time_created > ? then coalesce(json_extract(data, '$.cost'), 0) else 0 end), 4)
    from message
    where json_extract(data, '$.providerID') is not null
      and json_extract(data, '$.providerID') != ''
    group by 1 order by 5 desc limit ?
    """,
    (month_cutoff, week_cutoff, month_cutoff, week_cutoff, PROVIDER_ROW_LIMIT),
  ).fetchall()


def model_rows(connection: sqlite3.Connection, now_ms: int) -> list[tuple]:
  week_cutoff = now_ms - WEEK_DAYS * 86400000
  return connection.execute(
    """
    select json_extract(data, '$.providerID'),
      coalesce(json_extract(data, '$.modelID'), '?'),
      date(time_created / 1000, 'unixepoch', 'localtime'),
      round(sum(coalesce(json_extract(data, '$.tokens.total'), 0))),
      round(sum(coalesce(json_extract(data, '$.cost'), 0)), 4)
    from message
    where time_created > ?
      and json_extract(data, '$.providerID') is not null
      and json_extract(data, '$.providerID') != ''
    group by 1, 2, 3 order by 4 desc limit ?
    """,
    (week_cutoff, MODEL_ROW_LIMIT),
  ).fetchall()


def recent_rows(connection: sqlite3.Connection) -> list[tuple]:
  return connection.execute(
    """
    with recursive days(d) as (
      select date('now', 'localtime', '-6 days')
      union all
      select date(d, '+1 day') from days where d < date('now', 'localtime')
    )
    select days.d,
      round(coalesce(sum(coalesce(json_extract(message.data, '$.tokens.total'), 0)), 0)),
      round(coalesce(sum(coalesce(json_extract(message.data, '$.cost'), 0)), 0), 4)
    from days left join message
      on date(message.time_created / 1000, 'unixepoch', 'localtime') = days.d
    group by days.d order by 1
    """,
  ).fetchall()


def usage_messages(connection: sqlite3.Connection) -> list[dict[str, Any]]:
  rows = connection.execute(
    """
    select m.session_id, s.directory, m.data
    from message m
    join session s on s.id = m.session_id
    where json_extract(m.data, '$.role') = 'assistant'
      and json_extract(m.data, '$.providerID') is not null
      and json_extract(m.data, '$.providerID') != ''
    order by m.time_updated asc
    limit ?
    """,
    (MESSAGE_ROW_LIMIT,),
  ).fetchall()

  messages: list[dict[str, Any]] = []
  for session_id, directory, raw in rows:
    try:
      data = json.loads(raw or "{}")
    except (TypeError, ValueError):
      continue
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    total = number(tokens.get("total"))
    if total <= 0:
      total = (number(tokens.get("input")) + number(tokens.get("output"))
               + number(tokens.get("reasoning")) + number(cache.get("read"))
               + number(cache.get("write")))
    messages.append({
      "session": str(session_id or ""),
      "directory": str(directory or ""),
      "total": total,
      "cost": money(data.get("cost")),
    })
  return messages


def tool_session_rows(connection: sqlite3.Connection) -> list[tuple]:
  return connection.execute(
    """
    select session_id, data
    from part
    where json_extract(data, '$.type') = 'tool'
    limit ?
    """,
    (PART_ROW_LIMIT,),
  ).fetchall()


# ------------------------------------------------------- app attribution

def app_for_path(raw: str) -> str:
  if not raw or not raw.startswith("/"):
    return ""
  try:
    parts = Path(raw).relative_to(home()).parts
  except (ValueError, OSError):
    return ""
  if not parts:
    return "Home"
  if parts[0] == "Projects" and len(parts) >= 2:
    return parts[1]
  if parts[0] == ".config" and len(parts) >= 4 \
      and parts[1] == "omarchy" and parts[2] == "plugins":
    return parts[3]
  if parts[0].startswith(".") or parts[0] in USER_DIRS:
    return ""
  # A top-level entry only counts as an app when it is a real directory; a
  # stray file under $HOME is misc, not a project.
  if len(parts) == 1 or not (home() / parts[0]).is_dir():
    return ""
  return parts[0]


def app_for_directory(directory: str) -> str:
  raw = str(directory or "")
  if not raw:
    return "Home"
  app = app_for_path(raw)
  if app:
    return app
  name = Path(raw).name
  return name or "Home"


def input_paths(inp: dict[str, Any]) -> list[str]:
  if not isinstance(inp, dict):
    return []
  paths: list[str] = []
  for key in ("filePath", "filepath", "path", "file"):
    value = inp.get(key)
    if isinstance(value, str) and value.startswith("/"):
      paths.append(value)
  files = inp.get("files")
  if isinstance(files, list):
    for item in files:
      if isinstance(item, str) and item.startswith("/"):
        paths.append(item)
  command = inp.get("command")
  if isinstance(command, str):
    paths.extend(token for token in PATH_TOKEN.findall(command)
                 if "/" in token[1:])
  return paths


def session_app_map(connection: sqlite3.Connection) -> dict[str, str]:
  counters: dict[str, Counter[str]] = {}
  for session_id, raw in tool_session_rows(connection):
    try:
      part = json.loads(raw or "{}")
    except (TypeError, ValueError):
      continue
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    inp = state.get("input") if isinstance(state.get("input"), dict) else {}
    for path in input_paths(inp):
      app = app_for_path(path)
      if app:
        counters.setdefault(str(session_id), Counter())[app] += 1
  return {sid: counter.most_common(1)[0][0] for sid, counter in counters.items()}


def summarize_apps(messages: list[dict[str, Any]], session_app: dict[str, str],
                   rate: float, limit: int = APP_LIMIT) -> list[dict[str, Any]]:
  buckets: dict[str, dict[str, Any]] = {}
  for message in messages:
    app = session_app.get(message["session"]) \
      or app_for_directory(message.get("directory", ""))
    bucket = buckets.setdefault(
      app, {"tokens": 0, "costUsd": 0.0, "messages": 0, "sessions": set()})
    bucket["tokens"] += message["total"]
    bucket["costUsd"] += message["cost"]
    bucket["messages"] += 1
    if message["session"]:
      bucket["sessions"].add(message["session"])

  rows = [
    {
      "name": name,
      "tokens": bucket["tokens"],
      "costUsd": round(bucket["costUsd"], 6),
      "costEur": round(bucket["costUsd"] * rate, 4),
      "messages": bucket["messages"],
      "sessions": len(bucket["sessions"]),
    }
    for name, bucket in buckets.items()
  ]
  rows.sort(key=lambda row: row["tokens"], reverse=True)
  if len(rows) > limit:
    tail = rows[limit:]
    rows = rows[:limit] + [{
      "name": "Other",
      "tokens": sum(row["tokens"] for row in tail),
      "costUsd": round(sum(row["costUsd"] for row in tail), 6),
      "costEur": round(sum(row["costEur"] for row in tail), 4),
      "messages": sum(row["messages"] for row in tail),
      "sessions": sum(row["sessions"] for row in tail),
    }]
  return rows


# ------------------------------------------------------------------ assembly

def model_map(rows: list[tuple]) -> dict[str, list[dict[str, Any]]]:
  providers: dict[str, dict[str, dict[str, Any]]] = {}
  for pid, model, day, tokens, cost in rows:
    if not pid:
      continue
    base = providers.setdefault(str(pid), {})
    entry = base.setdefault(str(model or "?"),
                           {"tokensWeek": 0, "costWeek": 0.0, "daily": []})
    entry["tokensWeek"] += number(tokens)
    entry["costWeek"] += money(cost)
    entry["daily"].append({"date": str(day or ""),
                           "tokens": number(tokens),
                           "cost": money(cost)})

  result: dict[str, list[dict[str, Any]]] = {}
  for pid, models in providers.items():
    model_list = [
      {
        "modelName": name,
        "tokensWeek": data["tokensWeek"],
        "costWeek": round(data["costWeek"], 4),
        "daily": sorted(data["daily"], key=lambda item: item["date"]),
      }
      for name, data in models.items()
    ]
    model_list.sort(key=lambda item: item["tokensWeek"], reverse=True)
    result[pid] = model_list
  return result


def empty_recent_days() -> list[dict[str, Any]]:
  today = datetime.now().astimezone().date()
  return [
    {"date": (today - timedelta(days=offset)).isoformat(),
     "tokens": 0, "cost": 0.0}
    for offset in range(WEEK_DAYS - 1, -1, -1)
  ]


def build(connection: sqlite3.Connection, auth_path: Path,
          now_ms: int) -> dict[str, Any]:
  rate = fx_rate()
  keys = auth_keys(auth_path)

  providers: list[dict[str, Any]] = []
  for pid, month_tokens, week_tokens, month_cost, week_cost in provider_rows(
      connection, now_ms):
    if not pid:
      continue
    providers.append({
      "pid": str(pid),
      "tokensMonth": number(month_tokens),
      "tokensWeek": number(week_tokens),
      "costMonth": money(month_cost),
      "costWeek": money(week_cost),
      "hasKey": str(pid) in keys,
      "modelList": [],
    })

  models = model_map(model_rows(connection, now_ms))
  for provider in providers:
    provider["modelList"] = models.get(provider["pid"], [])

  balance = fetch_balance(deepseek_api_key())
  if balance is not None:
    target = next((p for p in providers if p["pid"] == AGENT_ID), None)
    if target is None:
      target = {
        "pid": AGENT_ID,
        "tokensMonth": 0,
        "tokensWeek": 0,
        "costMonth": 0.0,
        "costWeek": 0.0,
        "hasKey": AGENT_ID in keys,
        "modelList": [],
      }
      providers.append(target)
    target["balance"] = balance

  recent = recent_rows(connection) or empty_recent_days()
  recent_days = [
    {"date": str(day or ""), "tokens": number(tokens), "cost": money(cost)}
    for day, tokens, cost in recent
  ]

  try:
    messages = usage_messages(connection)
    session_app = session_app_map(connection)
  except sqlite3.Error:
    messages, session_app = [], {}
  apps = summarize_apps(messages, session_app, rate) if messages else []

  return {
    "status": "ok",
    "providers": providers,
    "go": collect_go(auth_path),
    "recentDays": recent_days,
    "apps": apps,
    "usdToEur": rate,
    "updatedAt": datetime.now(timezone.utc).isoformat(),
  }


def failure_record(reason: str) -> dict[str, Any]:
  return {
    "status": "error",
    "error": reason,
    "providers": [],
    "go": {"status": reason},
    "recentDays": empty_recent_days(),
    "apps": [],
    "usdToEur": DEFAULT_USD_TO_EUR,
    "updatedAt": datetime.now(timezone.utc).isoformat(),
  }


def main() -> int:
  parser = argparse.ArgumentParser(
    description="Collect OpenCode usage as one JSON record")
  parser.add_argument("--db", default="", help="override the opencode database path")
  parser.add_argument("--auth", default="", help="override the opencode auth path")
  parser.add_argument("--output", default="", help="also write the record to this path")
  parser.add_argument("--pretty", action="store_true", help="pretty-print to stdout")
  args = parser.parse_args()

  db = Path(args.db).expanduser() if args.db else opencode_db_path()
  auth_path = Path(args.auth).expanduser() if args.auth else opencode_auth_path()
  now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

  if args.db and not valid_override_path(args.db):
    print("collector.py: refused db path", file=sys.stderr)
    return 1

  connection: sqlite3.Connection | None = None
  try:
    if db.is_file():
      connection = connect(db)
      record = build(connection, auth_path, now_ms)
    else:
      record = failure_record("No opencode database")
  except sqlite3.Error as error:
    record = failure_record(f"Could not read the opencode database: {error}")
  except Exception as error:  # never leave the widget without a record
    record = failure_record(f"{type(error).__name__}: {error}")
  finally:
    if connection is not None:
      connection.close()

  payload = json.dumps(record, indent=2 if args.pretty else None,
                       separators=None if args.pretty else (",", ":"))
  if len(payload.encode()) > COLLECTOR_MAX_BYTES:
    print(f"collector.py: output exceeds {COLLECTOR_MAX_BYTES} bytes",
          file=sys.stderr)
    return 1

  if args.output:
    target = Path(args.output).expanduser()
    try:
      target.parent.mkdir(parents=True, exist_ok=True)
      tmp = target.with_name("." + target.name + ".tmp")
      tmp.write_text(payload + "\n")
      os.replace(tmp, target)
    except OSError as error:
      print(f"collector.py: {error}", file=sys.stderr)

  print(payload)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())

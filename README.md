# opencode-tracker

OpenCode usage widget for the Omarchy shell (Quickshell): token and cost usage
per configured provider (DeepSeek, Zen, Go, Bedrock, ...) from the local
`opencode.db`, daily per-model breakdowns for the last 7 days, OpenCode Go
limit windows, plus a deep-dive on DeepSeek: USD -> EUR conversion, the prepaid
credit balance, and per-app (project) attribution of every token.

Plugin ID: `io.github.thenobody0817.opencode-tracker`

This is a fork of [NiloyTheSpd/opencode-tracker](https://github.com/NiloyTheSpd/opencode-tracker)
that replaces the hardened bash collector with a Python collector merging in the
DeepSeek extras from a local setup.

## Features

- **Per-provider usage** — week and month tokens/cost, share bars, local-data
  flag when no API key is present.
- **Per-model daily breakdown** — expand a provider for the last 7 days per
  model.
- **EUR costs** — opencode records USD; the collector fetches the daily
  USD -> EUR rate (ECB via frankfurter.app, cached) and the panel shows EUR.
- **DeepSeek balance** — prepaid credit remaining from the DeepSeek balance
  endpoint, shown on the DeepSeek card.
- **Top apps** — tokens and cost attributed to the project each session worked
  on, inferred from the file paths its tools touched.
- **OpenCode Go limits** — rolling / weekly / monthly windows with pace.

## Install

    omarchy plugin add https://github.com/thenobody0817/opencode-tracker --enable --yes

## Remove

    omarchy plugin remove io.github.thenobody0817.opencode-tracker

## Dependencies

- `python3` (standard library only) — reads the local OpenCode database
  (`~/.local/share/opencode/opencode.db`, read-only), the auth file
  (`~/.local/share/opencode/auth.json`), and makes the bounded network calls.

No `jq`, `sqlite3`, or `curl` binary is required.

The collector never sends your auth key anywhere except the official endpoint
(DeepSeek balance, OpenCode Go usage) and never prints its contents.

## Configuration

- `refreshIntervalSec` — seconds between scans (default 300).
- `weeklyBudgetDollars` — optional weekly budget in USD, `0` disables it.
- USD -> EUR override — set `DEEPSEEK_USD_TO_EUR`, or `{"usdToEur": 0.9}` in
  `~/.config/omarchy/agents/deepseek.json`, to skip the live rate.

## Files

- `manifest.json` — Omarchy shell plugin manifest (schemaVersion 1)
- `Panel.qml` — bar widget entry point
- `Service.qml` — refresh service running `collector.py` on a timer
- `Model.js` — formatting and normalization helpers
- `collector.py` — collects provider tokens/cost, Go limits, FX, balance, apps

Edits under the installed plugin directory hot-reload; force a rescan with
`omarchy-shell shell rescanPlugins` if needed.

## License

MIT — see `LICENSE`. Upstream copyright held by Dahep and thespd.

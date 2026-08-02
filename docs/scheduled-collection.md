# Unattended collection with Windows Task Scheduler

Social sources have no history endpoint: Reddit `/new`, X recent-search and
ApeWisdom's rolling 24h snapshot only ever serve the last few days. History is
accumulated **forward**, one collection at a time, and an hour that passes
without a collection is an hour permanently missing from every baseline. While
the desktop app or web API server is open, `claudetrade.scheduler`'s in-app
loop collects every hour on its own — but only while that process is running.
This page covers the alternative for when the app is closed: two per-user
Windows Scheduled Tasks that keep collection (and the daily refresh + scan)
running around the clock, registered and managed through
`claudetrade schedule ...`.

Windows-only. On Linux/macOS, use `cron` instead — see the message
`claudetrade schedule install` prints if run off Windows.

## What gets installed

```
claudetrade schedule install
```

registers two tasks, both per-user (`/RL LIMITED` — no administrator
elevation, no stored password prompt) and idempotent (`/F` — safe to re-run,
e.g. after moving or upgrading the venv):

| Task name                     | Schedule                              | Runs                          |
| ------------------------------ | -------------------------------------- | ------------------------------ |
| `ClaudeTrade Sentiment Collect` | Every hour, every day                  | `claudetrade sentiment collect` |
| `ClaudeTrade Daily Refresh`     | Weekdays (Mon–Fri) at 18:30 local      | `claudetrade schedule run-daily` |

`ClaudeTrade Sentiment Collect` is social + attention only — the same
one-shot collection `claudetrade sentiment collect` always ran on demand. It
runs every day including weekends because social chatter doesn't stop for a
market holiday.

`ClaudeTrade Daily Refresh` runs at 18:30 local, after the US market close and
enough settle time for the day's bars to be available from the data provider.
`schtasks.exe` cannot chain two console-script invocations into one task, so
it calls `claudetrade schedule run-daily` — a subcommand that runs
`refresh` then `scan` back-to-back, in-process, under the one task. Both the
hourly and daily tasks take the same cross-process single-flight lock every
other refresh-triggering entry point does (`db.refresh_state_store`): if
something else is already refreshing when a scheduled tick fires, that tick
skips cleanly (and exits `0` — a benign skip is not a task failure and Task
Scheduler's own history must not record it as one) rather than racing it.
Scheduled runs are recorded under `entry_point="task_scheduler"`, so
`claudetrade status`, `GET /api/system/refresh/status` and the MCP
`get_refresh_status` tool can all tell an unattended run apart from a person
at the keyboard (`"cli"`) or the in-app hourly loop (`"scheduler"`).

Task Scheduler discards a task's stdout, so nothing printed by either command
matters once it's running unattended — what's actually kept is the rotating
log files under `<app_dir>/logs` (`claudetrade-cli.log` for the hourly
collect, `claudetrade-task_scheduler.log` for the daily run).

## Verifying it's installed

Either the OS tool directly:

```
schtasks /Query /TN "ClaudeTrade Sentiment Collect" /FO LIST /V
schtasks /Query /TN "ClaudeTrade Daily Refresh" /FO LIST /V
```

or the wrapper, which also confirms the app can see the same registration:

```
claudetrade schedule status
```

Task Scheduler's own GUI (`taskschd.msc`) also shows both tasks under
`Task Scheduler Library`, with their last run result and next run time.

## The double-collection note

If you leave `claudetrade ui` (or the web API server) open all day *and* have
`ClaudeTrade Sentiment Collect` installed, both the in-app hourly loop and the
scheduled task will try to collect. This is harmless — both take the same
single-flight refresh lock, so whichever one loses a race skips for free
rather than duplicating any work — but it does mean social sources can be hit
up to 2x/hour instead of 1x/hour while both are active. `claudetrade schedule
install` prints this warning every time it runs. To avoid the extra cadence,
set `scheduler.social_collection_enabled = false` in `config.toml` so only the
scheduled task collects.

## The X session-mode warning

If X cookie-session mode is active (`x.session_enabled = true`, using a stored
browser session cookie rather than the official paid API), running it
unattended on an hourly schedule is a ToS / account-suspension risk for that
X account — X's terms do not sanction automated, unattended use of a logged-in
browser session. `claudetrade schedule install` prints this caveat every time
it runs (and names whether session mode is currently enabled, when it can
read your config). Installing the scheduled tasks does **not** change
`x.session_enabled` either way; review it yourself in `config.toml` before
relying on unattended hourly collection while session mode is on.

## Removal

```
claudetrade schedule uninstall
```

Removes both tasks. Safe to run even if they were never installed (each task
not found is reported, not treated as an error).

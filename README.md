# Artemis Desk – daily dashboard collector

Page: "Artemis Desk" (canvas_id `96a63bab-5fed-4d4c-8b82-f9c3311fbf0c`)
https://cursor.com/canvas/store-4ecc89af-e2d9-485f-8a34-019a9d7534d8/96a63bab-5fed-4d4c-8b82-f9c3311fbf0c

## Refresh

    python3 /workspace/artemis-dashboard/collect.py              # ~5 s, keeps the last test result
    python3 /workspace/artemis-dashboard/collect.py --run-tests  # also runs pytest tests/ (~60 s, 180 s timeout)

Writes `/workspace/artemis-dashboard/snapshot.json`. A daily routine then reads
snapshot.json and edits the Page (ReadCanvas + EditCanvas on the canvas_id above,
or WriteCanvas with the same canvas_id for a full rewrite).

## What it collects (all read-only)

| Section | Source |
|---|---|
| Vast credit, auto-recharge, invoices | `vastai show user --raw`, `vastai show invoices --raw` |
| GPU runs | `iid.txt`, `rent_epoch.txt`, `killer*.sh` (END=) in each kit dir in `RUNS`; `vastai show instance <id> --raw` (whitelisted fields only) |
| Training | `a567-dense/checkpoints/*/train_log.jsonl`, `gpu-run-20261025/medium_bpe24k/summary.json`, `eval_local.json`, `report_*.json` |
| Expand loop | `/workspace/artemis-567885` git + `config.json` + `runs/expand_log.jsonl` + `runs/supervisor_log.jsonl` |
| Repo | `a567-dense` git log/status, tracked `*.md` count, optional pytest `tests/` |
| Box | load average, disk free on /workspace, /proc/meminfo |

Safety: never rents, destroys or modifies anything; `show instances` is not used
(it is empty on this account), only per-ID lookups. Only whitelisted instance fields
are saved, long token-like strings in invoice text are redacted, and the script
refuses to write if `instance_api_key` appears in the output.

Spend per run = max(invoice charges for that instance, $/hr × running duration);
"worst case" = $/hr × hard-kill window, an estimate. To add a new run, append it to
`RUNS` in collect.py with its kit dir and cap.

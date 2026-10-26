#!/usr/bin/env python3
"""Artemis daily dashboard collector (read-only).

Gathers Vast.ai credit/charges/instance state, training metrics, the expand loop,
repo health and box health into snapshot.json. It never rents, destroys or
modifies anything, never pushes, and never writes API keys or tokens anywhere.

Usage:
  python3 /workspace/artemis-dashboard/collect.py              # fast, reuses last test result
  python3 /workspace/artemis-dashboard/collect.py --run-tests  # also runs pytest tests/ (timeout 180 s)
"""
import glob, json, os, re, shutil, subprocess, sys, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "snapshot.json")
VAST = "/workspace/vast-venv/bin/vastai"
DENSE = "/workspace/a567-dense"
CK = f"{DENSE}/checkpoints"
EXPAND = "/workspace/artemis-567885"
PY = "/workspace/artemis-dense-venv/bin/python"

# Run registry: kit dir holds iid.txt / rent_epoch.txt / killer*.sh written at rent time.
RUNS = [
    {"key": "run1", "name": "GPU run 1 (48.8M dense, BPE 24k)", "kit": "/workspace/gpu-run-kit", "cap": 2.0, "gpus_planned": 1},
    {"key": "run2", "name": "FineWeb-Edu run (run 2)", "kit": "/workspace/gpu-run-kit2", "cap": 3.0, "gpus_planned": 1},
    {"key": "qwen", "name": "Qwen2.5-7B QLoRA fine-tune", "kit": "/workspace/qwen-kit", "cap": 5.0, "gpus_planned": 2},
    {"key": "glimmer", "name": "Muse Glimmer 30B QLoRA fine-tune", "kit": "/workspace/glimmer-kit", "cap": 15.0, "gpus_planned": 2},
]
SAFE_INSTANCE_FIELDS = ["id", "actual_status", "intended_status", "gpu_name", "num_gpus", "dph_total",
                        "start_date", "duration", "geolocation", "gpu_util", "disk_usage", "status_msg"]
REDACT = re.compile(r"[A-Za-z0-9_\-]{32,}")


def et(ts):
    return datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d %H:%M ET")


def sh(cmd, timeout=60, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:  # noqa
        return -1, "", str(e)


def read(p, default=None):
    try:
        return open(p).read().strip()
    except Exception:
        return default


def jsonl(p):
    out = []
    try:
        for line in open(p):
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return out


def vast_json(*args):
    rc, out, _ = sh([VAST, *args, "--raw"])
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def collect_vast():
    v = {"ok": False}
    u = vast_json("show", "user")
    if isinstance(u, dict):
        v.update(ok=True, credit=round(float(u.get("credit") or 0), 4),
                 autobill=u.get("autobill"), credit_only=u.get("billing_creditonly"))
    inv = vast_json("show", "invoices") or []
    if isinstance(inv, dict):
        inv = inv.get("invoices", [])
    charges, payments = {}, []
    for x in inv:
        if x.get("type") == "payment":
            payments.append({"amount": float(x.get("amount") or 0), "when": et(float(x["timestamp"])) if x.get("timestamp") else None})
        elif x.get("type") == "charge":
            iid = x.get("instance_id")
            charges.setdefault(str(iid), []).append({"desc": REDACT.sub("<redacted>", x.get("description") or ""), "amount": float(x.get("amount") or 0)})
    v["payments"] = payments
    v["charges_by_instance"] = {k: {"total": round(sum(c["amount"] for c in vs), 4), "items": vs} for k, vs in charges.items()}
    v["charges_total"] = round(sum(c["total"] for c in v["charges_by_instance"].values()), 4)
    return v


def instance(iid):
    d = vast_json("show", "instance", str(iid))
    if isinstance(d, dict) and "instances" in d:
        d = d["instances"]
    if isinstance(d, list):
        d = d[0] if d else None
    if not d:
        return None
    return {k: d.get(k) for k in SAFE_INSTANCE_FIELDS}


def collect_runs(vast):
    now = time.time()
    runs, seen = [], set()
    for r in RUNS:
        k = r["kit"]
        iid = read(f"{k}/iid.txt")
        row = dict(r)
        row["instance_id"] = iid
        if not iid:
            row["status"] = "queued"
            runs.append(row); continue
        seen.add(iid)
        rent = read(f"{k}/rent_epoch.txt")
        row["rented"] = read(f"{k}/rent_start.txt")
        kill_end = None
        for kf in glob.glob(f"{k}/killer*.sh"):
            m = re.search(r"^END=(\d+)", read(kf, ""), re.M)
            if m:
                kill_end = int(m.group(1))
        row["hard_kill"] = et(kill_end) if kill_end else None
        inst = instance(iid)
        row["instance"] = inst
        billed = vast.get("charges_by_instance", {}).get(iid, {}).get("total", 0.0)
        row["billed"] = round(billed, 4)
        if inst and inst.get("actual_status") == "running":
            row["status"] = "running"
            dph = float(inst.get("dph_total") or 0)
            hrs = float(inst.get("duration") or 0) / 3600
            row["accrued_estimate"] = round(dph * hrs, 4)
            row["spend"] = round(max(billed, dph * hrs), 4)
            if kill_end and rent:
                row["worst_case_at_hard_kill_estimate"] = round(dph * (kill_end - int(rent)) / 3600, 3)
        else:
            harv = " ".join(read(p, "") for p in glob.glob(f"{k}/harvester*.log"))
            row["status"] = "destroyed" if "destroyed" in harv or inst is None else (inst or {}).get("actual_status", "unknown")
            row["spend"] = round(billed, 4)
        runs.append(row)
    other = {i: c["total"] for i, c in vast.get("charges_by_instance", {}).items() if i not in seen}
    return runs, other


def summarize_train(path):
    logs = jsonl(path)
    steps = [d for d in logs if "step" in d and "loss" in d]
    out = {"points": [{"stage": d.get("stage"), "step": d["step"], "loss": d["loss"], **({"val": d["val"]} if "val" in d else {})} for d in steps]}
    for d in logs:
        if d.get("event") in ("before", "done", "stage1_done", "plan"):
            out[d["event"]] = {k: v for k, v in d.items() if k not in ("samples", "event")}
    return out


def collect_training():
    t = {}
    t["gpu_run1"] = summarize_train(f"{CK}/gpu-run-20261025/medium_bpe24k/train_log.jsonl")
    s = read(f"{CK}/gpu-run-20261025/medium_bpe24k/summary.json")
    if s:
        s = json.loads(s)
        t["gpu_run1"]["summary"] = {k: s.get(k) for k in ("gpu", "torch", "params_total", "train_tokens", "before", "after_stage1", "after_stage2", "stage1", "stage2", "started", "finished")}
        t["gpu_run1"]["summary"]["seed"] = s.get("args", {}).get("seed")
        t["gpu_run1"]["summary"]["command"] = s.get("command")
    for name in ("byte_sft2", "bpe_sft"):
        t[name] = summarize_train(f"{CK}/{name}/train_log.jsonl")
    for name in ("byte", "bpe"):
        t[f"{name}_stage1"] = summarize_train(f"{CK}/{name}/stage1_train_log.jsonl")
    ev = read(f"{CK}/gpu-run-20261025/eval_local.json")
    if ev:
        ev = json.loads(ev)
        t["eval_local"] = {k: {kk: vv for kk, vv in v.items() if kk != "samples"} | {"samples": v.get("samples", {})} for k, v in ev.items()}
    for f in ("report_bpe.json", "report_byte.json"):
        r = read(f"{CK}/{f}")
        if r:
            t[f.replace(".json", "")] = [{k: x.get(k) for k in ("ckpt", "tokenizer", "vocab", "params", "before", "after")} for x in json.loads(r)]
    return t


def collect_expand():
    e = {}
    rc, br, _ = sh(["git", "-C", EXPAND, "branch", "--show-current"])
    rc, lg, _ = sh(["git", "-C", EXPAND, "log", "-1", "--format=%h|%ci|%s"])
    e["branch"] = br.strip()
    if lg:
        h, d, s = lg.strip().split("|", 2)
        e["commit"] = {"hash": h, "date": d, "subject": s}
    cfg = json.loads(read(f"{EXPAND}/config.json", "{}"))

    def find(o, key):
        if isinstance(o, dict):
            if key in o: return o[key]
            for v in o.values():
                r = find(v, key)
                if r is not None: return r
        return None
    e["caps"] = {"num_layers_max": find(cfg, "num_layers_max"), "capacity_factor_max": find(cfg, "capacity_factor_max"), "num_experts_max": find(cfg, "num_experts_max")}
    e["next_cycle"] = cfg.get("cycle")
    cyc = jsonl(f"{EXPAND}/runs/expand_log.jsonl")
    e["cycles"] = [{"cycle": c["cycle"], "loss": c["loss"], "layers": c.get("num_layers"), "cf": c.get("capacity_factor"), "experts": sum(c.get("num_experts") or []), "dropped": c.get("dropped"), "time": c.get("time")} for c in cyc]
    sup = jsonl(f"{EXPAND}/runs/supervisor_log.jsonl")
    ev = {}
    for s in sup:
        ev[s.get("event")] = ev.get(s.get("event"), 0) + 1
    e["supervisor_events"] = ev
    return e


def collect_repo(run_tests):
    r = {}
    _, br, _ = sh(["git", "-C", DENSE, "branch", "--show-current"])
    _, lg, _ = sh(["git", "-C", DENSE, "log", "-10", "--format=%h|%ci|%s"])
    r["branch"] = br.strip()
    r["commits"] = [dict(zip(("hash", "date", "subject"), l.split("|", 2))) for l in lg.strip().splitlines()]
    _, st, _ = sh(["git", "-C", DENSE, "status", "--short"])
    r["uncommitted_files"] = len([l for l in st.splitlines() if l.strip()])
    _, md, _ = sh(["git", "-C", DENSE, "ls-files", "*.md"])
    r["md_docs_tracked"] = len(md.split())
    prev = {}
    try:
        prev = json.load(open(OUT)).get("repo", {}).get("tests", {})
    except Exception:
        pass
    r["tests"] = prev
    if run_tests:
        t0 = time.time()
        rc, out, err = sh([PY, "-m", "pytest", "-q", "tests", "-p", "no:cacheprovider"], timeout=180, cwd=DENSE)
        last = (out.strip().splitlines() or [err.strip()])[-1]
        r["tests"] = {"summary": last, "exit": rc, "seconds": round(time.time() - t0, 1), "when": et(time.time()), "command": f"cd {DENSE} && {PY} -m pytest -q tests"}
    return r


def collect_box():
    l1, l5, l15 = os.getloadavg()
    du = shutil.disk_usage("/workspace")
    mem = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":")
        mem[k] = int(v.split()[0]) * 1024
    return {"load": [round(l1, 2), round(l5, 2), round(l15, 2)], "cpus": os.cpu_count(),
            "disk_total_gb": round(du.total / 1e9, 1), "disk_free_gb": round(du.free / 1e9, 1),
            "mem_total_gb": round(mem["MemTotal"] / 2**30, 1), "mem_available_gb": round(mem["MemAvailable"] / 2**30, 1),
            "uptime_h": round(float(open("/proc/uptime").read().split()[0]) / 3600, 1)}


def main():
    run_tests = "--run-tests" in sys.argv
    snap = {"as_of": datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"), "as_of_epoch": int(time.time())}
    vast = collect_vast()
    snap["vast"] = vast
    snap["runs"], snap["other_charges"] = collect_runs(vast)
    snap["training"] = collect_training()
    snap["expand"] = collect_expand()
    snap["repo"] = collect_repo(run_tests)
    snap["box"] = collect_box()
    txt = json.dumps(snap, indent=1, default=str)
    assert "instance_api_key" not in txt and "VAST_API_KEY=" not in txt
    open(OUT, "w").write(txt)
    print(f"wrote {OUT} ({len(txt)} bytes) as of {snap['as_of']}")


if __name__ == "__main__":
    main()

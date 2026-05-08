#!/usr/bin/env python3
"""
GPU monitor for denali-14 (local) and denali-15 (ssh).
Samples every SAMPLE_INTERVAL seconds and writes:
  - data/samples.jsonl   (append-only, one JSON record per sample)
  - data/latest.json     (compact snapshot of last 30 days, used by dashboard)
"""

from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------- config ----------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
JSONL_PATH = DATA_DIR / "samples.jsonl"
LATEST_PATH = DATA_DIR / "latest.json"
DASHBOARD_DATA = ROOT / "docs" / "data.json"

HOSTS = [
    {"name": "denali-14", "ssh": None},   # local
    {"name": "denali-15", "ssh": "denali-15"},
]

SAMPLE_INTERVAL = int(os.environ.get("GPU_MON_INTERVAL", "600"))  # seconds (10 min)
RETENTION_DAYS = int(os.environ.get("GPU_MON_RETENTION_DAYS", "60"))
DASHBOARD_WINDOW_DAYS = 31
# auto-publish to gh-pages every N seconds (0 = disabled). Default = every sample.
PUBLISH_INTERVAL = int(os.environ.get("GPU_MON_PUBLISH_INTERVAL", str(SAMPLE_INTERVAL)))
PUBLISH_REMOTE = os.environ.get(
    "GPU_MON_PUBLISH_REMOTE",
    "https://github.com/DeFisch/Unveil-Cluster-Status.git",
)
PUBLISH_BRANCH = os.environ.get("GPU_MON_PUBLISH_BRANCH", "gh-pages")
GIT_USER_NAME = os.environ.get("GPU_MON_GIT_USER", "DeFisch")
GIT_USER_EMAIL = os.environ.get("GPU_MON_GIT_EMAIL", "fengzhenyang47@gmail.com")

# columns we ask nvidia-smi for
GPU_QUERY = "index,name,utilization.gpu,memory.used,memory.total"
PROC_QUERY = "pid,gpu_uuid,used_memory,process_name"


# ---------- helpers ----------
def run(cmd: list[str], host_ssh: str | None, timeout: int = 30) -> str:
    """Run a command locally or over ssh; return stdout (empty on failure)."""
    if host_ssh:
        full = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
            host_ssh, " ".join(cmd),
        ]
    else:
        full = cmd
    try:
        out = subprocess.run(
            full, capture_output=True, text=True, timeout=timeout, check=False,
        )
        if out.returncode != 0:
            sys.stderr.write(
                f"[warn] {host_ssh or 'local'} {' '.join(cmd)} rc={out.returncode}: "
                f"{out.stderr.strip()[:200]}\n"
            )
            return ""
        return out.stdout
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[warn] subprocess error on {host_ssh or 'local'}: {e}\n")
        return ""


def parse_csv(text: str) -> list[list[str]]:
    rows = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append([c.strip() for c in line.split(",")])
    return rows


def parse_int(s: str) -> int:
    m = re.search(r"-?\d+", s)
    return int(m.group(0)) if m else 0


def query_host(host: dict) -> dict | None:
    """Return dict with gpus + processes for one host, or None on full failure."""
    name = host["name"]
    ssh = host["ssh"]

    gpu_csv = run(
        ["nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"],
        ssh,
    )
    if not gpu_csv:
        return None

    gpus = []
    for row in parse_csv(gpu_csv):
        if len(row) < 5:
            continue
        gpus.append({
            "index": parse_int(row[0]),
            "name": row[1],
            "util": parse_int(row[2]),
            "mem_used": parse_int(row[3]),
            "mem_total": parse_int(row[4]),
        })

    # uuid -> index map (proc query reports uuid only)
    uuid_csv = run(
        ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"], ssh,
    )
    uuid_to_index = {}
    for row in parse_csv(uuid_csv):
        if len(row) >= 2:
            uuid_to_index[row[1]] = parse_int(row[0])

    proc_csv = run(
        ["nvidia-smi", f"--query-compute-apps={PROC_QUERY}", "--format=csv,noheader,nounits"],
        ssh,
    )
    raw_procs = []
    pids = []
    for row in parse_csv(proc_csv):
        if len(row) < 4:
            continue
        pid = parse_int(row[0])
        if pid <= 0:
            continue
        gpu_idx = uuid_to_index.get(row[1], -1)
        raw_procs.append({
            "pid": pid,
            "gpu": gpu_idx,
            "mem": parse_int(row[2]),
            "name": row[3].split("/")[-1][:60],
        })
        pids.append(str(pid))

    # resolve users in one ps call
    pid_to_user: dict[int, str] = {}
    if pids:
        ps_out = run(["ps", "-o", "pid=,user=", "-p", ",".join(pids)], ssh)
        for line in ps_out.strip().splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                try:
                    pid_to_user[int(parts[0])] = parts[1].strip()
                except ValueError:
                    pass

    procs = []
    for p in raw_procs:
        procs.append({
            **p,
            "user": pid_to_user.get(p["pid"], "unknown"),
        })

    return {
        "host": name,
        "gpus": gpus,
        "procs": procs,
    }


def take_sample() -> dict:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    hosts_data = []
    for host in HOSTS:
        d = query_host(host)
        if d is None:
            hosts_data.append({"host": host["name"], "gpus": [], "procs": [], "error": True})
        else:
            hosts_data.append(d)
    return {"ts": ts, "hosts": hosts_data}


# ---------- aggregation for the dashboard ----------
def build_dashboard_payload(samples: list[dict]) -> dict:
    """Compact representation of recent samples for the static dashboard."""
    # summary lists per host: timestamps + per-gpu util series + per-sample user occupancy
    by_host: dict[str, dict] = {}
    timestamps: list[str] = []

    for s in samples:
        timestamps.append(s["ts"])
        for hd in s["hosts"]:
            h = hd["host"]
            entry = by_host.setdefault(h, {
                "host": h,
                "num_gpus": 0,
                "gpu_names": [],
                "util": [],          # list[list[int]]: per timestamp, per gpu
                "mem_used": [],      # MiB
                "mem_total": [],     # MiB (latest)
                "users_per_gpu": [], # list[list[list[str]]]: ts -> gpu -> users
                "user_mem": [],      # list[dict[user]->mb] per ts (sum across all gpus)
            })
            n = max(entry["num_gpus"], len(hd["gpus"]))
            entry["num_gpus"] = n
            if hd["gpus"]:
                entry["gpu_names"] = [g["name"] for g in hd["gpus"]]
                entry["mem_total"] = [g["mem_total"] for g in hd["gpus"]]
            util_row = [None] * n
            mem_row = [None] * n
            for g in hd["gpus"]:
                if 0 <= g["index"] < n:
                    util_row[g["index"]] = g["util"]
                    mem_row[g["index"]] = g["mem_used"]
            entry["util"].append(util_row)
            entry["mem_used"].append(mem_row)

            users_per_gpu: list[list[str]] = [[] for _ in range(n)]
            user_mem: dict[str, int] = {}
            for p in hd["procs"]:
                if 0 <= p["gpu"] < n:
                    if p["user"] not in users_per_gpu[p["gpu"]]:
                        users_per_gpu[p["gpu"]].append(p["user"])
                user_mem[p["user"]] = user_mem.get(p["user"], 0) + p["mem"]
            entry["users_per_gpu"].append(users_per_gpu)
            entry["user_mem"].append(user_mem)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "interval_sec": SAMPLE_INTERVAL,
        "timestamps": timestamps,
        "hosts": list(by_host.values()),
    }


def load_recent_samples(days: int) -> list[dict]:
    if not JSONL_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    with JSONL_PATH.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["ts"])
                if ts >= cutoff:
                    out.append(rec)
            except (ValueError, KeyError):
                continue
    return out


def prune_jsonl():
    """Trim samples older than RETENTION_DAYS to keep the file bounded."""
    if not JSONL_PATH.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    tmp = JSONL_PATH.with_suffix(".jsonl.tmp")
    kept = 0
    dropped = 0
    with JSONL_PATH.open("r") as fin, tmp.open("w") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["ts"])
            except (ValueError, KeyError):
                continue
            if ts >= cutoff:
                fout.write(line + "\n")
                kept += 1
            else:
                dropped += 1
    if dropped:
        tmp.replace(JSONL_PATH)
        sys.stderr.write(f"[info] pruned {dropped} old samples, kept {kept}\n")
    else:
        tmp.unlink(missing_ok=True)


def publish_gh_pages():
    """Force-push current docs/ to the configured publish branch as a fresh single commit.

    Uses a throwaway temp repo so main's working tree and history are untouched.
    Authentication is taken from ~/.netrc (HOME is inherited).
    """
    import tempfile
    src = ROOT / "docs"
    if not src.exists() or not any(src.iterdir()):
        return

    ts = datetime.now(timezone.utc).isoformat(timespec="minutes")
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"  # never block on a credential prompt

    with tempfile.TemporaryDirectory(prefix="gpumon-pub-") as tmp:
        td = Path(tmp)
        for child in src.iterdir():
            dst = td / child.name
            if child.is_dir():
                shutil.copytree(child, dst)
            else:
                shutil.copy2(child, dst)

        steps = [
            ["git", "init", "-q", "-b", PUBLISH_BRANCH],
            ["git", "config", "user.name", GIT_USER_NAME],
            ["git", "config", "user.email", GIT_USER_EMAIL],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", f"publish {ts}"],
            ["git", "push", "-q", "--force",
                PUBLISH_REMOTE, f"HEAD:{PUBLISH_BRANCH}"],
        ]
        for cmd in steps:
            r = subprocess.run(
                cmd, cwd=td, env=env, capture_output=True, timeout=60,
            )
            if r.returncode != 0:
                err = r.stderr.decode(errors="replace").strip()[:300]
                sys.stderr.write(
                    f"[warn] publish step failed ({' '.join(cmd[:2])}): {err}\n"
                )
                return
    print(f"[{ts}] published to {PUBLISH_BRANCH}", flush=True)


def write_dashboard():
    samples = load_recent_samples(DASHBOARD_WINDOW_DAYS)
    payload = build_dashboard_payload(samples)
    tmp = LATEST_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(payload, f, separators=(",", ":"))
    tmp.replace(LATEST_PATH)
    DASHBOARD_DATA.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LATEST_PATH, DASHBOARD_DATA)


# ---------- main loop ----------
def append_sample(sample: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("a") as f:
        f.write(json.dumps(sample, separators=(",", ":")) + "\n")


def main():
    once = "--once" in sys.argv
    rebuild = "--rebuild-dashboard" in sys.argv

    if rebuild:
        write_dashboard()
        print(f"wrote {LATEST_PATH}")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    last_prune = 0.0
    last_publish = 0.0

    while True:
        t0 = time.time()
        try:
            sample = take_sample()
            append_sample(sample)
            write_dashboard()
            n_users = sum(
                len({p["user"] for p in h["procs"]}) for h in sample["hosts"]
            )
            print(
                f"[{sample['ts']}] sampled "
                f"{sum(len(h['gpus']) for h in sample['hosts'])} GPUs, "
                f"{n_users} unique users",
                flush=True,
            )
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[error] sample failed: {e}\n")

        if time.time() - last_prune > 24 * 3600:
            try:
                prune_jsonl()
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[error] prune failed: {e}\n")
            last_prune = time.time()

        if PUBLISH_INTERVAL > 0 and time.time() - last_publish >= PUBLISH_INTERVAL:
            try:
                publish_gh_pages()
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[error] publish failed: {e}\n")
            last_publish = time.time()

        if once:
            break

        elapsed = time.time() - t0
        sleep_for = max(1.0, SAMPLE_INTERVAL - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()

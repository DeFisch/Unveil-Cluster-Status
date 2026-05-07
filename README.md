# Denali GPU Monitor

24/7 monitor of GPU utilization on `denali-14` (local) and `denali-15` (via ssh),
plus a static dashboard for the past 24h / week / month.

## Layout

```
gpu-monitor/
  monitor.py        # the sampler + dashboard JSON builder
  start.sh          # daemonize (auto-restart on crash)
  stop.sh           # stop daemon
  status.sh         # check daemon + last sample
  data/
    samples.jsonl   # append-only log of every sample
    latest.json     # rolling 30-day window for the dashboard
  dashboard/
    index.html      # static dashboard (Chart.js)
    data.json       # symlink-equivalent of latest.json (copied each cycle)
  run/
    monitor.pid
    monitor.log
```

## Running

```sh
./start.sh    # background daemon, samples every 10 min, auto-restarts on crash
./status.sh   # see if it's running + count of samples
./stop.sh
```

The daemon survives shell exit (`nohup`). It does **not** survive a host reboot.
After a reboot, just `./start.sh` again. (For boot-time autostart you'd need
systemd-user or a cron `@reboot` entry — let me know if you want that.)

## Tunables (env vars)

- `GPU_MON_INTERVAL` — seconds between samples (default 600)
- `GPU_MON_RETENTION_DAYS` — prune `samples.jsonl` older than this (default 60)

## Dashboard

It's a static HTML file. Two ways to view:

1. **Local browser via SSH tunnel.** From your laptop:
   ```sh
   ssh -L 8000:localhost:8000 denali-14
   # on denali-14:
   cd ~/gpu-monitor/dashboard && python3 -m http.server 8000
   ```
   then open http://localhost:8000

2. **Push to GitHub Pages.** Once you have a repo:
   ```sh
   cd ~/gpu-monitor
   git init && git add . && git commit -m "init"
   git remote add origin git@github.com:YOU/gpu-monitor.git
   git push -u origin main
   # enable Pages in repo settings, source = main branch /dashboard folder
   ```
   Add a cron entry (or extend the daemon) to commit + push every cycle.

## Notes on what gets tracked

- `nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total` per GPU.
- `nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory,process_name`,
  cross-referenced with `ps -o user=` to attribute each running GPU process
  to a Linux user.
- The dashboard colors each user uniquely (stable hash → HSL) so the same
  user keeps the same color across the page and across visits.

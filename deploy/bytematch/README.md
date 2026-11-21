# Byte-match runner

The work server measures every push to b5-decomp's `x360` branch with the Xbox 360 XDK's own
compiler, under Wine, in a sandboxed container, so the dashboard's byte-match panel follows the
native branch without anyone measuring by hand.

```
x360 push ─► BP-Decomp_Workflow bytematch.yml ─► POST /admin/bytematch/measure {sha}
                                                   │  (web app: writes queue/<sha>.json)
                                                   ▼
                     bp-bytematch.path ─► runner.py (root) ─► docker run … wine.py refresh
                                                   │  (writes jobs/<sha>/status.json, bundle.json.gz)
                                                   ▼
             bytematch.yml polls, adopts the records (tools/bytematch/remote.py), commits
             them with the summary, then POST /admin/sync refreshes the dashboard
```

The measuring code is BP-Decomp_Workflow's `tools/bytematch/wine.py` and its image is
`tools/bytematch/docker`. This folder holds the runner and its systemd units.

## Security model

- The web app (`bpwork`) never runs Docker and is not in the `docker` group. It writes a request
  naming a commit and serves the files the runner writes back (`bp_work_server/bytematch_jobs.py`).
- The runner runs as root but never trusts those files. It reads only request names, which must
  be full commit ids. It writes into the web app's folder as the web app's own user, and reads
  the container's output without following symlinks.
- It measures only the tip of `x360`, fetched over https from fixed URLs. Submodule URLs are fixed
  here, not taken from `.gitmodules`, and checkouts ignore symlinks and hooks.
- The container has no network and no capabilities, cannot gain new privileges, and runs as
  uid 1500 with CPU, memory and process limits. It sees only the native tree, the measuring code,
  the XDK subset and ARTIST's bytes (all read-only), plus its cache and its output folder.
- No code from the pushed tree runs. `wine.py` parses `tools/build_x360.py` without importing it
  and refuses any flag outside a compile-only allow-list. Only the XDK's `cl.exe` runs, over the
  source text, and compile errors keep their text only when they point into the tree.
- CI checks every record field by field before committing it (`tools/bytematch/remote.py`).
- Residual risk: a source crafted to exploit `cl.exe` or Wine itself could run code inside the
  container and read the XDK and ARTIST bytes it is given. It still has no network, and anything
  it writes reaches the public repository only as record fields CI has checked.

## Layout

| Path | Owner | What |
|---|---|---|
| `/var/lib/bp-bytematch/` | root, 0700 | the runner's private state |
| `inputs/xdk/` | 1500, 0500 | XDK `bin/win32` compiler files and `include/xbox` (`wine.py export-xdk`, ~14 MB) |
| `inputs/image/` | 1500, 0500 | ARTIST's bytes: `image.bin`, `image.json` (`wine.py export-image`, ~20 MB) |
| `workflow/`, `b5-decomp/` | root | the runner's own clones (blobless; the workflow one sparse) |
| `cache/` | 1500 | the measurer's object cache (`index.json`, `server-deps.json`, objects) |
| `runs/<sha>/` | root (`out/` 1500) | one run: `run.log`, the container's output |
| `/var/lib/bp-work-server/bytematch/` | bpwork | `queue/`, `jobs/<sha>/` shared with the web app |
| `/usr/local/lib/bp-bytematch/runner.py` | root | the runner, copied here from this folder |

The XDK and ARTIST files never enter git, a log or a CI artifact.

## One-time setup (as root)

```sh
install -d -m 700 /var/lib/bp-bytematch /var/lib/bp-bytematch/inputs
install -d -o 1500 -g 1500 -m 700 /var/lib/bp-bytematch/cache
install -d -o bpwork -g bpwork -m 755 /var/lib/bp-work-server/bytematch \
    /var/lib/bp-work-server/bytematch/queue /var/lib/bp-work-server/bytematch/jobs
# copy the exports from a maintainer's machine (python tools/bytematch/wine.py export-xdk / export-image)
tar -C /var/lib/bp-bytematch/inputs -xzf xdk.tgz && tar -C /var/lib/bp-bytematch/inputs -xzf image.tgz
chown -R 1500:1500 /var/lib/bp-bytematch/inputs/xdk /var/lib/bp-bytematch/inputs/image
chmod -R u=rX,go= /var/lib/bp-bytematch/inputs/xdk /var/lib/bp-bytematch/inputs/image
install -d -m 755 /usr/local/lib/bp-bytematch
install -m 755 /opt/bp-work-server/deploy/bytematch/runner.py /usr/local/lib/bp-bytematch/runner.py
install -m 644 /opt/bp-work-server/deploy/bytematch/bp-bytematch.service \
    /opt/bp-work-server/deploy/bytematch/bp-bytematch.path \
    /opt/bp-work-server/deploy/bytematch/bp-bytematch.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bp-bytematch.path bp-bytematch.timer
```

A cache seeded from a full sweep (its `index.json` and `server-deps.json`, made by `wine.py
refresh` in the same image) saves the first run from compiling all ~3,700 sources. Without it
the first run compiles everything, which takes about an hour on this host.

Deploys keep `/usr/local/lib/bp-bytematch/runner.py` and the units current once they exist
(`.github/workflows/deploy.yml`).

## Operating

- Queue a commit by hand: `curl -X POST -H "X-Work-Token: <admin id>" -H 'Content-Type: application/json'
  -d '{"sha":"<x360 tip>"}' http://127.0.0.1:8765/admin/bytematch/measure`
- Watch: `journalctl -u bp-bytematch.service -f`, `/var/lib/bp-bytematch/runs/<sha>/run.log`
- A failed job is queued again when CI asks again; a job whose runner died is re-queued after 30 minutes.
- Change limits with `Environment=` in a drop-in: `BP_BYTEMATCH_CPUS` (3), `BP_BYTEMATCH_MEMORY` (6g),
  `BP_BYTEMATCH_JOBS` (3), `BP_BYTEMATCH_TIMEOUT` (seconds, 5 h).
- Remove everything: `systemctl disable --now bp-bytematch.path bp-bytematch.timer`, delete the units,
  `/usr/local/lib/bp-bytematch`, `/var/lib/bp-bytematch` and the `bp-bytematch:*` images.

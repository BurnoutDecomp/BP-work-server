# Running in Docker

Production runs the server as a Docker container managed by systemd. The container runs the
same code on the same address as the old virtualenv service, so nginx and the byte-match
runner did not change.

| Piece | Where |
|---|---|
| Image | `bp-work-server:local`, built from this repo by `docker compose build` |
| Compose project | `/opt/bp-work-server/compose.yaml` |
| Unit | `/etc/systemd/system/bp-work-server.service`, from this folder |
| Settings | `/etc/bp-work-server/bp-work-server.env`, outside git (see `bp-work-server.env.example`) |
| Data | `/var/lib/bp-work-server`, mounted at the same path |

The container uses the host's network, so it still listens only on `127.0.0.1:8765` and nginx
keeps proxying `decomp.adriwin.fr` to it. It runs as `bpwork` (uid 999, gid 990) with no
capabilities, so the files it writes keep their owner and the byte-match runner reads the
queue as before. It has no access to the Docker socket.

## Deploys

A push to `main` runs `.github/workflows/deploy.yml`, which connects over SSH and:

1. updates `/opt/bp-work-server` and the UI submodule;
2. builds the new image while the old container keeps serving;
3. raises the maintenance page and refreshes the byte-match runner and this unit;
4. restarts `bp-work-server.service`, which recreates the container and waits for `/health`;
5. lowers the maintenance page and prunes the replaced image.

A failed build stops the deploy before step 3, so the running container is untouched.

## Operations

```bash
systemctl status bp-work-server                      # the unit
cd /opt/bp-work-server && docker compose ps          # the container and its health
cd /opt/bp-work-server && docker compose logs -f     # the app log
systemctl restart bp-work-server                     # restart; also picks up a rebuilt image
```

## First-time setup on a new server

1. Create the `bpwork` account and `/var/lib/bp-work-server` owned by it. If its ids are not
   999 and 990, export `BP_UID` and `BP_GID` before building.
2. Copy `bp-work-server.env.example` to `/etc/bp-work-server/bp-work-server.env` and adjust it.
3. Install and start the unit:

   ```bash
   install -m 644 deploy/docker/bp-work-server.service /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now bp-work-server
   ```

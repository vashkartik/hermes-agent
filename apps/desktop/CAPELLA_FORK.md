# Capella fork of Hermes

Capella embeds the Hermes desktop app. We carry a small set of local patches on
top of upstream `NousResearch/hermes-agent`, kept on the **`capella/patches`**
branch in the owner's fork so upstream updates rebase cleanly onto them.

## Remotes

| remote     | repo                              | role                         |
| ---------- | --------------------------------- | ---------------------------- |
| `upstream` | `NousResearch/hermes-agent`       | upstream — fetch only        |
| `fork`     | `vashkartik/hermes-agent`         | owner's fork — our patches    |

`capella/patches` tracks `fork/capella/patches`. Never push to `upstream`.

## Update Hermes (rebase our patches onto upstream)

```sh
git fetch upstream
git rebase upstream/main capella/patches   # replay our patches onto latest upstream
# resolve conflicts if any, then VERIFY our patches survived the rebase:
bash apps/desktop/scripts/capella-patch-guard.sh   # fails if any patch was clobbered
cd apps/desktop && npm run build            # rebuild the renderer
# rebuild + reinstall the embedded Hermes.app (see Deploy below)
git push --force-with-lease fork capella/patches
```

So: **Update Hermes = `git fetch upstream && git rebase upstream/main capella/patches` → patch-guard → rebuild → reinstall.**

## Patch guard (don't lose our changes on update)

`apps/desktop/scripts/capella-patch-guard.sh` asserts every Capella patch is
still present (hero emoji, per-profile session persistence, the restore-on-switch
controller change, the gateway patch) and exits non-zero if a rebase dropped one
— so an upstream update can never silently clobber our work. It also runs in CI
via `.github/workflows/capella-patches-guard.yml` (GitHub-hosted runner; enable
Actions on the fork to have it run on every push to `capella/**`).

## Deploy into Capella's embed

Capella spawns `hermes dashboard` with
`HERMES_WEB_DIST=<Hermes.app>/Contents/Resources/app.asar/dist` — i.e. it serves
the **packaged** app's dist, not `apps/desktop/dist`. A fresh `npm run build`
alone does NOT reach Capella; the `Hermes.app` must be repackaged.

```sh
cd apps/desktop && npm run dist:mac        # build + electron-builder --mac
# → apps/desktop/release/mac-arm64/Hermes.app  (the embedded app)
```

Then install that `Hermes.app` where Capella loads it from (the running app
location), and relaunch.

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
# resolve conflicts if any, then:
cd apps/desktop && npm run build            # rebuild the renderer
# rebuild + reinstall the embedded Hermes.app (see Deploy below)
git push --force-with-lease fork capella/patches
```

So: **Update Hermes = `git fetch upstream && git rebase upstream/main capella/patches` → rebuild → reinstall.**

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

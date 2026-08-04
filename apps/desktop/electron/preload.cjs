// Compatibility preload entry.
//
// The real preload is bundled to dist/electron-preload.js (see
// scripts/bundle-electron-main.mjs and main.ts's PRELOAD_PATH). This wrapper
// keeps the app.asar path `electron/preload.cjs` alive because external
// embedders — the Ace desktop shell in particular — resolve the desktop
// bundle by checking for exactly this file and load it as the webview
// preload. Requiring the bundle executes the same contextBridge setup the
// standalone app gets, so both entries stay one implementation.
module.exports = require('../dist/electron-preload.js');

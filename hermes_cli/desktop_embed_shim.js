// Serve-mode desktop bridge shim — injected by hermes_cli.web_server into the
// desktop renderer's index.html when the dashboard serves a desktop dist
// (HERMES_WEB_DIST pointing at an app bundle's renderer). Provides
// window.hermesDesktop over this origin's own HTTP/WS so the renderer boots
// in ANY host — a plain browser tab, an embedding shell's webview or iframe —
// with no dependency on the host's Electron bridge or proxy shim. Adapted
// from the Ace desktop shell's embed shim (same contract, proven on the
// mobile proxy path); a host-injected bridge that got there first wins via
// the `if (window.hermesDesktop) return` guard below.
(() => {
  if (window.hermesDesktop) return; // never clobber a real preload bridge
  const PREFIX = '';
  const token = () => String(window.__HERMES_SESSION_TOKEN__ || '');
  const remoteToken = () => {
    try {
      const cookie = String(window.document?.cookie || '');
      for (const part of cookie.split(';')) {
        const idx = part.indexOf('=');
        if (idx < 0) continue;
        if (part.slice(0, idx).trim() !== 'ace.remoteToken') continue;
        return decodeURIComponent(part.slice(idx + 1).trim());
      }
    } catch {}
    return '';
  };
  const normalizeProfile = (profile) => String(profile || '')
    .trim()
    .replace(/^ace-session:/i, '')
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '');
  const wsUrl = (basePath, sessionToken) =>
    (location.protocol === 'https:' ? 'wss://' : 'ws://') +
    location.host + basePath + '/api/ws?token=' + encodeURIComponent(sessionToken || '');
  // Single-sidecar, mirroring the desktop webview: EVERY profile is served by the one
  // app sidecar at the base /hermes-desktop proxy \u2014 no per-profile (/__profile/)
  // sub-proxy splitting the gateway socket across two paths (the mobile
  // session-busy desync). When a profile is requested we still resolve its
  // connection so backendProfile drives the operator-id -> 'default' API alias
  // and we carry the sidecar's fresh session token.
  const connection = async (profile) => {
    const normalized = normalizeProfile(profile);
    if (normalized) {
      const conn = await aceApi('hermesDesktopAppConnection', { profile: normalized });
      const sessionToken = conn?.token || token();
      return {
        baseUrl: location.origin + PREFIX,
        wsUrl: wsUrl(PREFIX, sessionToken),
        token: sessionToken,
        backendProfile: conn?.backendProfile,
        mode: 'local',
        authMode: 'token',
        source: 'local',
        profile: conn?.profile || normalized,
        logs: [],
        isFullscreen: false,
        nativeOverlayWidth: 0,
        windowButtonPosition: null,
      };
    }
    return {
      baseUrl: location.origin + PREFIX,
      wsUrl: wsUrl(PREFIX, token()),
      token: token(),
      mode: 'local',
      authMode: 'token',
      source: 'local',
      profile: undefined,
      logs: [],
      isFullscreen: false,
      nativeOverlayWidth: 0,
      windowButtonPosition: null,
    };
  };
  const ok = (value) => Promise.resolve(value);
  const subscription = () => () => {};
  const bootProgress = () => ({
    phase: 'backend.ready',
    message: 'Hermes Desktop is ready.',
    progress: 100,
    running: true,
    visible: false,
    error: null,
    at: Date.now(),
  });
  const connectionConfig = (profile) => ({
    envOverride: false,
    mode: 'local',
    profile: profile || null,
    remoteAuthMode: 'token',
    remoteOauthConnected: false,
    remoteTokenPreview: null,
    remoteTokenSet: false,
    remoteUrl: '',
  });
  const aceApi = async (method, payload) => {
    // Sidecar-served mode: no Ace control plane behind this origin. The few
    // backend calls the shim makes are answered locally from what the
    // dashboard itself provides.
    if (method === 'hermesDesktopAppConnection') {
      return { token: token(), profile: (payload && payload.profile) || undefined, backendProfile: undefined };
    }
    if (method === 'hermesDesktopGetActiveProfile') {
      return { profile: '' };
    }
    if (method === 'hermesDesktopSetActiveProfile' || method === 'hermesDesktopTouchAppProfile') {
      return { ok: true };
    }
    if (method === 'pasteClipboardImage') {
      return null;
    }
    throw new Error('unavailable in sidecar-served mode: ' + method);
  };
  const profileRootUrl = (profile) => {
    const url = new URL(location.href);
    const name = String(profile || '').trim();
    // LEGACY-COMPAT: the forked Hermes build still reads the pre-rename
    // capellaProfile params; mirror them until the fork is updated.
    if (name) {
      url.searchParams.set('aceProfile', name);
      url.searchParams.set('capellaProfile', name);
    } else {
      url.searchParams.delete('aceProfile');
      url.searchParams.delete('capellaProfile');
    }
    url.searchParams.set('aceProfileReload', String(Date.now()));
    url.searchParams.set('capellaProfileReload', String(Date.now()));
    url.hash = '/';
    return url.toString();
  };
  // A freshly (re)spawned sidecar answers the liveness route a beat before all
  // /api routes are registered, and the proxy returns 503 while it is still
  // coming up. The SPA's boot sequence ends with GET /api/profiles/sessions and
  // latches an un-retried "Hermes couldn't start" overlay on a single failure.
  // So idempotent GETs retry briefly on the transient boot statuses (404/502/
  // 503) and on transport errors; non-GET and persistent failures still throw.
  var BOOT_RETRY_STATUS = { 404: true, 502: true, 503: true };
  var BOOT_RETRY_BACKOFFS_MS = [200, 400, 800, 1200, 1600];
  var BOOT_RETRY_MAX_ATTEMPTS = 6;
  var sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // Alias the SPA's operator id ('<op>') to the profile the backend actually
  // serves ('default' for an identity-home sidecar) on profile-scoped paths \u2014
  // both the path segment (/api/profiles/<op>/soul) and the ?profile= query \u2014
  // so the call resolves instead of 404ing "Profile '<op>' does not exist".
  var ALIAS_RESERVED_SEGMENTS = { sessions: true, all: true, default: true, active: true };
  const PROFILES_PREFIX = '/api/profiles/';
  const aliasPathProfile = (apiPath, backendProfile) => {
    const target = String(backendProfile || '').trim();
    if (!target) return apiPath;
    let raw = String(apiPath || '');
    // Path segment: /api/profiles/<name>[/...]. String-split (no in-template
    // regex) to find the name segment and rewrite just that.
    if (raw.indexOf(PROFILES_PREFIX) === 0) {
      const after = raw.slice(PROFILES_PREFIX.length);
      let end = after.length;
      for (let i = 0; i < after.length; i += 1) {
        const ch = after.charAt(i);
        if (ch === '/' || ch === '?') { end = i; break; }
      }
      const segRaw = after.slice(0, end);
      const restAndQuery = after.slice(end);
      let decoded = segRaw;
      try { decoded = decodeURIComponent(segRaw); } catch (e) {}
      if (segRaw && !ALIAS_RESERVED_SEGMENTS[decoded] && decoded !== target) {
        raw = PROFILES_PREFIX + encodeURIComponent(target) + restAndQuery;
      }
    }
    // Query parameter: ?profile=<name>.
    const qIdx = raw.indexOf('?');
    if (qIdx >= 0) {
      const params = new URLSearchParams(raw.slice(qIdx + 1));
      const current = params.get('profile');
      if (current && current !== 'all' && current !== target) {
        params.set('profile', target);
        raw = raw.slice(0, qIdx) + '?' + params.toString();
      }
    }
    return raw;
  };
  // Does this path carry an operator id that needs aliasing to the backend
  // profile? True for a non-reserved /api/profiles/<name>[/...] segment or a
  // concrete ?profile=<op> (anything but 'all'). Used to decide whether to pay
  // for a backendProfile lookup; reserved segments ('sessions'/'active'/...) and
  // ?profile=all never need one.
  const pathNeedsProfileAlias = (apiPath) => {
    const raw = String(apiPath || '');
    if (raw.indexOf(PROFILES_PREFIX) === 0) {
      const after = raw.slice(PROFILES_PREFIX.length);
      let end = after.length;
      for (let i = 0; i < after.length; i += 1) {
        const ch = after.charAt(i);
        if (ch === '/' || ch === '?') { end = i; break; }
      }
      let seg = after.slice(0, end);
      try { seg = decodeURIComponent(seg); } catch (e) {}
      if (seg && !ALIAS_RESERVED_SEGMENTS[seg]) return true;
    }
    const qIdx = raw.indexOf('?');
    if (qIdx >= 0) {
      const current = new URLSearchParams(raw.slice(qIdx + 1)).get('profile');
      if (current && current !== 'all') return true;
    }
    return false;
  };
  // The active (no-profile) sidecar's backendProfile, resolved once and cached:
  // 'default' when it is rooted at an identity home (so the operator-id alias
  // still fires when the SPA omits request.profile \u2014 as listAllProfileSessions
  // does, baking ?profile=<op> straight into the URL), else null. The desktop
  // bridge gets this for free because appConnection derives it from the active
  // home; the mobile shim's no-profile connection() branch does not, so resolve
  // it here on demand instead of 404ing "Profile '<op>' does not exist".
  var activeBackendProfile;
  var activeOperatorProfile;
  var activeConnPromise = null;
  const resolveActiveConnection = () => {
    if (activeBackendProfile !== undefined) {
      return Promise.resolve({ backendProfile: activeBackendProfile, operatorProfile: activeOperatorProfile });
    }
    if (!activeConnPromise) {
      activeConnPromise = aceApi('hermesDesktopAppConnection', { profile: '' }).then(
        (conn) => {
          activeBackendProfile = (conn && conn.backendProfile) || null;
          activeOperatorProfile = (conn && conn.profile) || null;
          return { backendProfile: activeBackendProfile, operatorProfile: activeOperatorProfile };
        },
        () => {
          activeConnPromise = null; // transient: allow a later retry
          return { backendProfile: null, operatorProfile: null };
        }
      );
    }
    return activeConnPromise;
  };
  // Re-tag session rows in the RESPONSE with the operator id (logical profile),
  // mirroring hermes-desktop-bridge.cts tagSessionResponseProfile. The request
  // profile is aliased <op>\u2192'default' so the call resolves on an identity-home
  // sidecar, but the SPA scopes its session list to the active operator \u2014 so the
  // 'default'-tagged rows that come back would be filtered out and the sidebar
  // would show no history. The desktop bridge re-tags for free; the mobile shim
  // must do the same. No in-template regex (see aliasPathProfile).
  const normalizeLogicalProfile = (profile) => {
    const value = String(profile || '').trim();
    return value && value !== 'default' ? value : '';
  };
  const tagSessionProfile = (session, profile) => {
    if (!profile || !session || typeof session !== 'object' || Array.isArray(session)) return session;
    const existing = String(session.profile || '').trim();
    if (existing && existing !== 'default') return session;
    return { ...session, profile: profile };
  };
  const responseHasSessionRows = (r) =>
    !!(r && typeof r === 'object' && !Array.isArray(r) &&
      ((Array.isArray(r.sessions) && r.sessions.length) ||
        (r.session && typeof r.session === 'object' && !Array.isArray(r.session)) ||
        (Array.isArray(r.results) && r.results.length)));
  const shouldTagSessionResponse = (apiPath) => {
    let cleanPath = String(apiPath || '');
    const q = cleanPath.indexOf('?');
    if (q >= 0) cleanPath = cleanPath.slice(0, q);
    if (cleanPath === '/api/sessions' || cleanPath === '/api/profiles/sessions' || cleanPath === '/api/sessions/search') {
      return true;
    }
    const SINGLE = '/api/sessions/';
    if (cleanPath.indexOf(SINGLE) === 0) {
      const rest = cleanPath.slice(SINGLE.length);
      if (rest.length > 0 && rest.indexOf('/') < 0) return true;
    }
    return false;
  };
  const tagSessionResponseProfile = (apiPath, response, profile) => {
    const logicalProfile = normalizeLogicalProfile(profile);
    if (!logicalProfile || !shouldTagSessionResponse(apiPath)) return response;
    if (!response || typeof response !== 'object' || Array.isArray(response)) return response;
    let next = response;
    let hasRows = false;
    if (Array.isArray(response.sessions)) {
      hasRows = true;
      next = next === response ? { ...response } : next;
      next.sessions = response.sessions.map((s) => tagSessionProfile(s, logicalProfile));
    }
    if (response.session && typeof response.session === 'object' && !Array.isArray(response.session)) {
      hasRows = true;
      next = next === response ? { ...response } : next;
      next.session = tagSessionProfile(response.session, logicalProfile);
    }
    if (Array.isArray(response.results)) {
      const tagged = response.results.map((it) => tagSessionProfile(it, logicalProfile));
      if (tagged.some((it, i) => it !== response.results[i])) {
        hasRows = true;
        next = next === response ? { ...response } : next;
        next.results = tagged;
      }
    }
    if (hasRows && typeof response.total === 'number') {
      const totals = response.profile_totals && typeof response.profile_totals === 'object'
        ? { ...response.profile_totals } : {};
      if (totals[logicalProfile] == null) totals[logicalProfile] = response.total;
      next = next === response ? { ...response } : next;
      next.profile_totals = totals;
    } else if (hasRows && response.profile_totals && typeof response.profile_totals === 'object') {
      const totals = { ...response.profile_totals };
      if (totals[logicalProfile] == null && totals.default != null) {
        totals[logicalProfile] = totals.default;
        next = next === response ? { ...response } : next;
        next.profile_totals = totals;
      }
    }
    return next;
  };
  const api = async (request) => {
    const req = request || {};
    const rawPath = String(req.path || '');
    if (!rawPath.startsWith('/api/')) throw new Error('hermes api path must start with /api/');
    const method = String(req.method || 'GET').toUpperCase();
    const conn = await connection(req.profile);
    let backendProfile = conn.backendProfile;
    if (!backendProfile && pathNeedsProfileAlias(rawPath)) {
      backendProfile = (await resolveActiveConnection()).backendProfile;
    }
    const path = aliasPathProfile(rawPath, backendProfile);
    const headers = { authorization: 'Bearer ' + conn.token };
    let body;
    if (req.body !== undefined && req.body !== null) {
      headers['content-type'] = 'application/json';
      body = typeof req.body === 'string' ? req.body : JSON.stringify(req.body);
    }
    const attempts = method === 'GET' ? BOOT_RETRY_MAX_ATTEMPTS : 1;
    let lastErr = null;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      let res;
      try {
        res = await (window.fetch || fetch)(conn.baseUrl + path, { method, headers, body, credentials: 'include' });
      } catch (err) {
        // Transport-level failure (proxy/sidecar warming): retry GETs.
        lastErr = err;
        if (attempt === attempts - 1) throw err;
        await sleep(BOOT_RETRY_BACKOFFS_MS[Math.min(attempt, BOOT_RETRY_BACKOFFS_MS.length - 1)]);
        continue;
      }
      if (!res.ok) {
        if (method === 'GET' && BOOT_RETRY_STATUS[res.status] && attempt < attempts - 1) {
          await sleep(BOOT_RETRY_BACKOFFS_MS[Math.min(attempt, BOOT_RETRY_BACKOFFS_MS.length - 1)]);
          continue;
        }
        throw new Error('hermes api ' + path + ' failed: HTTP ' + res.status);
      }
      const text = await res.text();
      const parsed = text ? JSON.parse(text) : null;
      // Tag session rows with the active operator id so the SPA's profile-scoped
      // session list shows the identity home's history instead of filtering it
      // out as 'default'. conn.profile is set for profile-scoped calls; the
      // session-list calls omit request.profile, so resolve the active operator.
      let operatorProfile = conn.profile;
      if (!operatorProfile && shouldTagSessionResponse(rawPath) && responseHasSessionRows(parsed)) {
        operatorProfile = (await resolveActiveConnection()).operatorProfile;
      }
      return tagSessionResponseProfile(rawPath, parsed, operatorProfile);
    }
    throw lastErr || new Error('hermes api ' + path + ' failed');
  };
  const mobileFiles = new Map();
  const isBlobLike = (value) =>
    !!(value && typeof value === 'object' && typeof value.arrayBuffer === 'function');
  const safeExt = (ext) => {
    const clean = String(ext || '').replace(/^\\./, '').replace(/[^a-z0-9]/gi, '').toLowerCase();
    return clean || 'bin';
  };
  const extForBlob = (file, fallback) => {
    const name = String(file?.name || '');
    const match = name.match(/\\.([a-z0-9]{1,6})$/i);
    if (match) return safeExt(match[1]);
    const type = String(file?.type || '');
    if (type === 'application/pdf') return 'pdf';
    if (type.indexOf('image/') === 0 || type.indexOf('video/') === 0) {
      return safeExt(type.slice(type.indexOf('/') + 1));
    }
    return safeExt(fallback || 'bin');
  };
  const rememberFile = (file) => {
    if (!isBlobLike(file)) return '';
    const id =
      'ace-mobile-file://' +
      (window.crypto?.randomUUID?.() || Date.now() + '-' + Math.random().toString(16).slice(2)) +
      '/' +
      encodeURIComponent(file.name || 'attachment');
    mobileFiles.set(id, file);
    return id;
  };
  const fileFor = (target) => {
    if (isBlobLike(target)) return target;
    return mobileFiles.get(String(target || '')) || null;
  };
  const bytesToBase64 = (bytes) => {
    const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || []);
    let binary = '';
    for (let i = 0; i < view.length; i += 8192) {
      binary += String.fromCharCode.apply(null, Array.from(view.slice(i, i + 8192)));
    }
    const encode = window.btoa || (typeof btoa === 'function' ? btoa : null);
    if (!encode) throw new Error('Base64 encoder is unavailable');
    return encode(binary);
  };
  const payloadBytesBase64 = (data) => {
    if (typeof data === 'string') {
      const comma = data.indexOf(',');
      return comma >= 0 ? data.slice(comma + 1) : data;
    }
    if (data instanceof ArrayBuffer) return bytesToBase64(data);
    if (ArrayBuffer.isView(data)) return bytesToBase64(new Uint8Array(data.buffer, data.byteOffset, data.byteLength));
    if (Array.isArray(data)) return bytesToBase64(new Uint8Array(data));
    return '';
  };
  const blobToDataUrl = (file) =>
    new Promise((resolve, reject) => {
      const Reader = window.FileReader;
      if (typeof Reader !== 'function') {
        if (typeof file?.arrayBuffer === 'function') {
          file.arrayBuffer().then(
            (buffer) => resolve('data:' + (file.type || 'application/octet-stream') + ';base64,' + bytesToBase64(buffer)),
            reject
          );
          return;
        }
        reject(new Error('Mobile file reader is unavailable'));
        return;
      }
      const reader = new Reader();
      reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
      reader.onerror = () => reject(reader.error || new Error('Mobile file read failed'));
      reader.readAsDataURL(file);
    });
  const blobToText = (file) =>
    new Promise((resolve, reject) => {
      const target = file?.slice ? file.slice(0, 512 * 1024) : file;
      if (typeof target?.text === 'function') {
        target.text().then(resolve, reject);
        return;
      }
      const Reader = window.FileReader;
      if (typeof Reader !== 'function') {
        reject(new Error('Mobile file reader is unavailable'));
        return;
      }
      const reader = new Reader();
      reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
      reader.onerror = () => reject(reader.error || new Error('Mobile file read failed'));
      reader.readAsText(target);
    });
  const saveBlobAsPath = async (file, fallbackExt) => {
    const dataUrl = await blobToDataUrl(file);
    const bytes = payloadBytesBase64(dataUrl);
    if (!bytes) throw new Error('Attachment was empty');
    const result = await aceApi('pasteClipboardImage', {
      bytes,
      ext: extForBlob(file, fallbackExt),
    });
    if (result?.path) return result.path;
    throw new Error(result?.error || 'Could not write attachment to a temp file');
  };
  const saveImageBuffer = async (payload = {}, ext) => {
    const positional =
      typeof payload === 'string' ||
      payload instanceof ArrayBuffer ||
      ArrayBuffer.isView(payload) ||
      Array.isArray(payload);
    const bytes = payloadBytesBase64(positional ? payload : payload?.data);
    if (!bytes) throw new Error('Image data was empty');
    const result = await aceApi('pasteClipboardImage', {
      bytes,
      ext: safeExt((positional ? ext : payload?.ext) || 'png'),
    });
    if (result?.path) return result.path;
    throw new Error(result?.error || 'Could not write attachment to a temp file');
  };
  const selectPaths = (options = {}) => {
    if (options?.directories) return ok([]);
    return new Promise((resolve) => {
      const doc = window.document;
      if (!doc?.createElement || !doc.body) {
        resolve([]);
        return;
      }
      const input = doc.createElement('input');
      input.type = 'file';
      input.multiple = options?.multiple !== false;
      input.style.position = 'fixed';
      input.style.left = '-10000px';
      input.style.top = '0';
      const cleanup = () => {
        try { input.remove(); } catch {}
      };
      input.addEventListener('change', async () => {
        const files = Array.from(input.files || []);
        cleanup();
        const paths = [];
        for (const file of files) {
          try {
            paths.push(await saveBlobAsPath(file));
          } catch {
            const virtualPath = rememberFile(file);
            if (virtualPath) paths.push(virtualPath);
          }
        }
        resolve(paths);
      }, { once: true });
      input.addEventListener('cancel', () => {
        cleanup();
        resolve([]);
      }, { once: true });
      doc.body.appendChild(input);
      input.click();
    });
  };
  window.hermesDesktop = {
    getConnection: (profile) => connection(profile),
    touchBackend: (profile) =>
      aceApi('hermesDesktopTouchAppProfile', { profile: normalizeProfile(profile) }).then(
        (touched) => ({ ok: true, touched: Boolean(touched) }),
        () => ({ ok: true, touched: false })
      ),
    getGatewayWsUrl: (profile) => connection(profile).then((conn) => conn.wsUrl),
    getBootProgress: () => ok(bootProgress()),
    onBootProgress: subscription,
    getBootstrapState: () =>
      ok({
        active: false,
        manifest: null,
        stages: {},
        error: null,
        log: [],
        startedAt: null,
        completedAt: Date.now(),
        unsupportedPlatform: null,
      }),
    resetBootstrap: () => ok({ ok: true }),
    repairBootstrap: () => ok({ ok: true }),
    cancelBootstrap: () => ok({ ok: false, cancelled: false }),
    onBootstrapEvent: subscription,
    getConnectionConfig: (profile) => ok(connectionConfig(profile)),
    saveConnectionConfig: (payload) => ok(connectionConfig(payload && payload.profile)),
    applyConnectionConfig: (payload) => ok(connectionConfig(payload && payload.profile)),
    testConnectionConfig: () => ok({ ok: true, baseUrl: location.origin + PREFIX, version: null }),
    probeConnectionConfig: (rawUrl) =>
      ok({ baseUrl: String(rawUrl || ''), reachable: false, authMode: 'unknown' }),
    oauthLoginConnectionConfig: (rawUrl) =>
      ok({ ok: false, baseUrl: String(rawUrl || ''), connected: false }),
    oauthLogoutConnectionConfig: () => ok({ ok: true, connected: false }),
    profile: {
      get: () => aceApi('hermesDesktopGetActiveProfile', null).catch(() => ({ profile: null })),
      set: async (profile) => {
        const result = await aceApi('hermesDesktopSetActiveProfile', { profile });
        if (result?.changed !== false) {
          window.setTimeout(() => {
            try {
              location.assign(profileRootUrl(result?.profile || profile));
            } catch {
              try { location.reload(); } catch {}
            }
          }, 50);
        }
        return result;
      },
    },
    api,
    notify: () => ok(false),
    requestMicrophoneAccess: () => ok(false),
    readFileDataUrl: (target) => {
      const file = fileFor(target);
      return file ? blobToDataUrl(file) : Promise.reject(new Error('Mobile file is not available'));
    },
    readFileText: async (target) => {
      const file = fileFor(target);
      if (!file) throw new Error('Mobile file is not available');
      const text = await blobToText(file);
      return {
        binary: String(text || '').indexOf(String.fromCharCode(0)) >= 0,
        byteSize: file.size || 0,
        language: 'text',
        mimeType: file.type || 'application/octet-stream',
        path: String(target || ''),
        text,
        truncated: (file.size || 0) > 512 * 1024,
      };
    },
    selectPaths,
    writeClipboard: (text) => {
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          return navigator.clipboard.writeText(String(text == null ? '' : text)).then(() => true, () => false);
        }
      } catch {}
      return ok(false);
    },
    saveImageFromUrl: () => ok(false),
    saveImageBuffer,
    saveClipboardImage: async () => {
      try {
        const items = await navigator.clipboard?.read?.();
        for (const item of items || []) {
          const type = (item.types || []).find((t) => String(t || '').indexOf('image/') === 0);
          if (!type) continue;
          return await saveBlobAsPath(await item.getType(type), type.slice(type.indexOf('/') + 1));
        }
      } catch {}
      return '';
    },
    getPathForFile: (file) => rememberFile(file),
    normalizePreviewTarget: () => ok(null),
    watchPreviewFile: () => Promise.reject(new Error('File watching is unavailable in the mobile embed')),
    stopPreviewFileWatch: () => ok(false),
    setTitleBarTheme: () => {},
    setPreviewShortcutActive: () => {},
    openExternal: (url) => {
      try {
        window.open(String(url || ''), '_blank', 'noopener');
      } catch {}
      return ok(undefined);
    },
    fetchLinkTitle: () => ok(''),
    sanitizeWorkspaceCwd: (cwd) => {
      const trimmed = typeof cwd === 'string' ? cwd.trim() : '';
      return ok({ cwd: trimmed, sanitized: false });
    },
    settings: {
      getDefaultProjectDir: () => ok({ defaultLabel: '', dir: null }),
      pickDefaultProjectDir: () => ok({ canceled: true, dir: null }),
      setDefaultProjectDir: (dir) => ok({ dir: dir || null }),
    },
    revealLogs: () => ok({ ok: false, path: '', error: 'Logs live on the desktop host.' }),
    getRecentLogs: () => ok({ path: '', lines: [] }),
    readDir: () => Promise.reject(new Error('Directory reads are unavailable in the mobile embed')),
    gitRoot: () => ok(null),
    terminal: {
      dispose: () => ok(false),
      onData: () => () => {},
      onExit: () => () => {},
      resize: () => ok(false),
      start: () => Promise.reject(new Error('Terminals are unavailable in the mobile embed')),
      write: () => ok(false),
    },
    onClosePreviewRequested: subscription,
    onOpenUpdatesRequested: subscription,
    onWindowStateChanged: subscription,
    onPreviewFileChanged: subscription,
    onBackendExit: subscription,
    onPowerResume: subscription,
  };
})();
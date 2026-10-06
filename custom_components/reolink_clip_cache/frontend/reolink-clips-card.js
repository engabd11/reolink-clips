/**
 * Reolink Clips Card v2.1 (Cyborg dark and coffee themes)
 *
 * Companion card for the Reolink Clip Cache integration. A single
 * `reolink_clip_cache/clips` call returns a whole camera-day with signed local
 * URLs and poster thumbnails already attached, so a cached clip starts playing
 * immediately instead of waiting for the NVR to seek and remux it.
 *
 * The card falls back to browsing the Reolink media source directly when the
 * integration is absent, so it still works (at NVR speed) on its own.
 *
 *   type: custom:reolink-clips-card
 *   cameras: [carport, back_door]   # optional, omit for every camera
 *   default_event_type: all
 *   thumbnails: true
 *   autoplay: false
 */

const CARD_VERSION = '2.1.0';

// Signed clip and thumbnail URLs live for 30 minutes; refresh the list before then
// so a wall tablet left on this card keeps working.
const LIST_REFRESH_MS = 25 * 60 * 1000;

const EVENT_META = {
  person:  { label: 'Person',  color: 'sand'  },
  vehicle: { label: 'Vehicle', color: 'rust'  },
  animal:  { label: 'Animal',  color: 'moss'  },
  package: { label: 'Package', color: 'clay'  },
  visitor: { label: 'Visitor', color: 'sand'  },
  face:    { label: 'Face',    color: 'sand'  },
  motion:  { label: 'Motion',  color: 'clay'  },
  other:   { label: 'Event',   color: 'taupe' },
};

const PLURALS = {
  all: 'events', person: 'people', vehicle: 'vehicles', animal: 'animals',
  package: 'packages', visitor: 'visitors', face: 'faces', motion: 'motion events',
};

const slug = (value) =>
  String(value || '').toLowerCase().trim().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');

const meta = (type) => EVENT_META[type] || EVENT_META.other;

// Browse results carry a full media source URI; the Reolink identifiers we
// parse and rebuild are the bare part after the slash.
const MS_PREFIX = 'media-source://reolink/';
const bare = (id) => {
  const value = String(id == null ? '' : id);
  return value.startsWith(MS_PREFIX) ? value.slice(MS_PREFIX.length) : value;
};
const uri = (id) => (String(id).startsWith(MS_PREFIX) ? String(id) : MS_PREFIX + id);

// Camera names come from the device registry and the title from user config,
// so they are escaped before going anywhere near innerHTML.
const esc = (value) => String(value == null ? '' : value).replace(
  /[&<>"']/g,
  (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]),
);

class ReolinkClipsCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._hass = null;
    this._config = null;

    this._cameras = [];            // [{key, name, event_types, sensors}]
    this._cameraIndex = 0;
    this._dates = [];              // [{date, title}]
    this._selectedDate = null;     // ISO yyyy-mm-dd
    this._eventType = 'all';

    this._allClips = [];
    this._clips = [];
    this._clipIndex = 0;

    this._integration = true;      // false once we know it is not installed
    this._ready = false;
    this._loading = false;
    this._sensorSnapshot = '';
    this._prefetched = new Set();
    this._retriedClip = null;
    this._currentClipId = null;

    this._unsubEvents = null;
    this._detectionTimer = null;
    this._refreshTimer = null;
    this._loadToken = 0;           // newest _loadClips call wins
    this._selectToken = 0;         // newest _selectClip call wins
    this._pendingClip = null;      // uncached clip waiting for Play
    this._docClick = () => this._closeDropdowns();
  }

  // ── Lovelace plumbing ───────────────────────────────────────────────

  static getConfigElement() {
    return document.createElement('reolink-clips-card-editor');
  }

  static async getStubConfig(hass) {
    let cameras = [];
    try {
      const result = await hass.callWS({ type: 'reolink_clip_cache/cameras' });
      cameras = (result.cameras || []).map((camera) => camera.key);
    } catch (err) {
      cameras = [];
    }
    return { type: 'custom:reolink-clips-card', cameras, thumbnails: true };
  }

  setConfig(config) {
    this._config = {
      title: 'Events',
      default_event_type: 'all',
      thumbnails: true,
      autoplay: false,
      theme: 'dark',
      ...config,
    };
    this.setAttribute('theme', this._config.theme === 'coffee' ? 'coffee' : 'dark');
    // Accept the 1.x shape (a list of {name, sensors}) as well as plain keys.
    this._cameraFilter = (this._config.cameras || [])
      .map((camera) => (typeof camera === 'string' ? camera : camera && camera.name))
      .filter(Boolean);
    this._eventType = this._config.default_event_type || 'all';
    this._render();
    if (this._hass) this._init();
  }

  getCardSize() {
    return this._config && this._config.thumbnails ? 7 : 5;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._init();
    else this._refreshDetections();
  }

  connectedCallback() {
    document.addEventListener('click', this._docClick);
    if (this._hass && this._ready) {
      this._subscribe();
      this._startDetectionTicker();
    }
  }

  disconnectedCallback() {
    document.removeEventListener('click', this._docClick);
    if (this._detectionTimer) clearInterval(this._detectionTimer);
    this._detectionTimer = null;
    if (this._refreshTimer) clearInterval(this._refreshTimer);
    this._refreshTimer = null;
    if (this._unsubEvents) {
      this._unsubEvents.then((unsub) => unsub && unsub()).catch(() => {});
      this._unsubEvents = null;
    }
  }

  // ── Bootstrapping ───────────────────────────────────────────────────

  async _init() {
    if (!this._hass || !this._config || this._ready) return;
    this._ready = true;

    try {
      const result = await this._hass.callWS({ type: 'reolink_clip_cache/cameras' });
      this._cameras = this._applyFilter(result.cameras || []);
    } catch (err) {
      // Integration missing or not loaded — browse the media source ourselves.
      this._integration = false;
      this._cameras = await this._fallbackCameras();
    }

    if (!this._cameras.length) {
      this._fail(
        this._integration
          ? 'No Reolink cameras found. Check the Reolink integration and that your NVR has a working hard disk.'
          : 'Reolink Clip Cache is not installed, and no Reolink cameras were found in the media source.',
      );
      return;
    }

    this._render();
    this._subscribe();
    this._startDetectionTicker();
    await this._loadDates();
  }

  _applyFilter(cameras) {
    if (!this._cameraFilter.length) return cameras;
    const wanted = this._cameraFilter.map(slug);
    const ordered = [];
    for (const want of wanted) {
      const match = cameras.find(
        (camera) => camera.key === want || slug(camera.name) === want,
      );
      if (match) ordered.push(match);
    }
    return ordered.length ? ordered : cameras;
  }

  _subscribe() {
    if (this._unsubEvents || !this._hass || !this._integration) return;
    this._unsubEvents = this._hass.connection
      .subscribeEvents((event) => this._onClipCached(event), 'reolink_clip_cache_clip_cached')
      .catch(() => null);
  }

  _startDetectionTicker() {
    if (this._detectionTimer) clearInterval(this._detectionTimer);
    // The "3m ago" labels need to tick even when no state changes arrive.
    this._detectionTimer = setInterval(() => this._refreshDetections(true), 30000);
    if (this._refreshTimer) clearInterval(this._refreshTimer);
    if (this._integration) {
      this._refreshTimer = setInterval(() => {
        const video = this.$('video');
        // Leave a clip that is playing alone; refresh around it next time.
        if (video && !video.paused) return;
        this._loadClips({ silent: true, keepPosition: true });
      }, LIST_REFRESH_MS);
    }
  }

  _onClipCached(event) {
    const camera = this._camera();
    if (!camera || !event.data || event.data.camera !== camera.key) return;
    // A clip we are already showing just became cached, or a new one landed.
    this._loadClips({ silent: true, keepPosition: true });
  }

  // ── Data loading ────────────────────────────────────────────────────

  _camera() {
    return this._cameras[this._cameraIndex] || null;
  }

  async _loadDates() {
    const camera = this._camera();
    if (!camera) return;
    this._setLoading(true);

    try {
      this._dates = this._integration
        ? (await this._hass.callWS({ type: 'reolink_clip_cache/dates', camera: camera.key })).dates
        : await this._fallbackDates(camera);
    } catch (err) {
      this._dates = [];
    }

    if (!this._dates.length) {
      this._setLoading(false);
      this._fail('No recordings found for this camera.');
      this._renderDates();
      return;
    }

    const today = this._todayISO();
    const match = this._dates.find((entry) => entry.date === today);
    this._selectedDate = (match || this._dates[0]).date;
    this._renderDates();
    await this._loadClips();
  }

  async _loadClips({ silent = false, keepPosition = false } = {}) {
    const camera = this._camera();
    if (!camera || !this._selectedDate) return;

    // The newest request wins: switching camera or day mid-load discards the
    // older answer instead of being blocked by it.
    const token = ++this._loadToken;
    const date = this._selectedDate;
    this._loading = true;
    if (!silent) this._setLoading(true);
    const previousId = keepPosition && this._clips[this._clipIndex]
      ? this._clips[this._clipIndex].media_content_id
      : null;

    let clips;
    try {
      clips = this._integration
        ? (await this._hass.callWS({ type: 'reolink_clip_cache/clips', camera: camera.key, date })).clips
        : await this._fallbackClips(camera, date);
    } catch (err) {
      if (token !== this._loadToken) return;
      this._allClips = [];
      this._loading = false;
      if (!silent) this._fail(err.message || 'Could not load clips.');
      return;
    }
    if (token !== this._loadToken || camera !== this._camera()) return;

    this._allClips = clips || [];
    this._loading = false;
    this._setLoading(false);
    this._applyFilterAndRender(previousId);
  }

  _applyFilterAndRender(restoreId) {
    this._clips = this._eventType === 'all'
      ? this._allClips.slice()
      : this._allClips.filter((clip) => (clip.event_types || []).includes(this._eventType));

    this._renderEventChips();
    this._renderSummary();
    this._renderFilmstrip();

    if (!this._clips.length) {
      this._showEmpty();
      return;
    }

    let index = 0;
    if (restoreId) {
      const found = this._clips.findIndex((clip) => clip.media_content_id === restoreId);
      if (found >= 0) index = found;
    }

    // Reloading the source under a clip that is already playing would be
    // jarring, so only refresh the chrome around it.
    const target = this._clips[index];
    if (target && this._currentClipId === target.media_content_id) {
      this._clipIndex = index;
      this._renderClipChrome(target);
      this._renderFilmstrip();
      this._renderNav();
      return;
    }
    this._selectClip(index);
  }

  // ── Playback ────────────────────────────────────────────────────────

  async _selectClip(index, { play = false } = {}) {
    if (index < 0 || index >= this._clips.length) return;
    const token = ++this._selectToken;
    this._clipIndex = index;
    this._retriedClip = null;
    const clip = this._clips[index];
    this._currentClipId = null;
    this._pendingClip = null;
    this._setError(null);

    const video = this.$('video');
    const placeholder = this.$('placeholder');
    const playBtn = this.$('play-btn');

    this._renderClipChrome(clip);
    this._renderNav();
    this._renderFilmstrip();

    video.pause();
    video.removeAttribute('src');
    video.load();
    video.poster = clip.thumbnail || '';
    video.style.display = 'block';
    placeholder.style.display = 'none';
    playBtn.classList.remove('hidden');

    // A cached clip is a local, faststarted file: load it now so it starts at
    // once. An uncached clip streams from the NVR, which is slow and holds one of
    // its few playback sessions, so it is only fetched when Play is pressed.
    if (!clip.url) {
      this._pendingClip = clip;
      this._setHint('Plays from the NVR, so it can take a few seconds to start');
      if (play || this._config.autoplay) this._play();
      return;
    }
    this._setHint('');
    video.preload = 'metadata';
    video.src = clip.url;
    this._currentClipId = clip.media_content_id;
    if (token !== this._selectToken) return;
    if (play || this._config.autoplay) this._play();
    this._prefetchNeighbours();
  }

  /** Resolve a clip that has no URL yet, then play it. */
  async _playPending() {
    const clip = this._pendingClip;
    if (!clip) return false;
    const token = this._selectToken;
    this._setLoading(true);
    const url = await this._resolve(clip);
    if (token !== this._selectToken) return true;
    this._pendingClip = null;
    if (!url) {
      this._setLoading(false);
      this._setError('This clip could not be loaded from the NVR.');
      return true;
    }
    const video = this.$('video');
    video.preload = 'auto';
    video.src = url;
    this._currentClipId = clip.media_content_id;
    this._setHint('');
    video.play().then(() => this.$('play-btn').classList.add('hidden')).catch(() => {});
    return true;
  }

  async _resolve(clip) {
    if (!this._integration) {
      try {
        const resolved = await this._hass.callWS({
          type: 'media_source/resolve_media',
          media_content_id: clip.media_content_id,
        });
        return resolved.url;
      } catch (err) {
        return null;
      }
    }
    try {
      const result = await this._hass.callWS({
        type: 'reolink_clip_cache/resolve',
        media_content_id: clip.media_content_id,
        clip_id: clip.clip_id || null,
      });
      if (result && result.url) {
        clip.url = result.cached ? result.url : null;
        return result.url;
      }
    } catch (err) {
      /* fall through */
    }
    return null;
  }

  _prefetchNeighbours() {
    if (!this._config.thumbnails) return;
    for (const offset of [1, -1]) {
      const clip = this._clips[this._clipIndex + offset];
      if (!clip || !clip.url || this._prefetched.has(clip.url)) continue;
      this._prefetched.add(clip.url);
      // Faststarted clips carry their index up front, so the first slice is
      // enough to make the next clip start instantly.
      fetch(clip.url, { headers: { Range: 'bytes=0-262143' } }).catch(() => {});
    }
  }

  _onVideoError() {
    const video = this.$('video');
    if (!video.getAttribute('src')) return;
    const clip = this._clips[this._clipIndex];
    this._setLoading(false);
    const code = video.error && video.error.code;
    // MEDIA_ERR_SRC_NOT_SUPPORTED on a file that downloaded fine is almost always
    // an H.265 recording on a browser without a hardware decoder.
    if (code === 4 && this._retriedClip === (clip && clip.media_content_id)) {
      this._setError('This browser cannot play this clip. It is probably H.265: set the integration to the low resolution stream.');
      return;
    }
    if (!clip || this._retriedClip === clip.media_content_id) {
      this._setError('The clip stopped loading.');
      return;
    }
    // Signed URLs expire; ask for a fresh one once before giving up.
    this._retriedClip = clip.media_content_id;
    clip.url = null;
    const retry = clip.media_content_id;
    this._selectClip(this._clipIndex).then(() => {
      if (this._clips[this._clipIndex]?.media_content_id === retry) {
        this._retriedClip = retry;
        this._playPending();
      }
    });
  }

  _play() {
    if (this._pendingClip) { this._playPending(); return; }
    const video = this.$('video');
    if (!video.getAttribute('src')) return;
    video.play().then(() => this.$('play-btn').classList.add('hidden')).catch(() => {});
  }

  _setHint(text) {
    const el = this.$('hint');
    if (!el) return;
    el.textContent = text || '';
    el.style.display = text ? 'block' : 'none';
  }

  _setError(text) {
    const el = this.$('error');
    if (!el) return;
    el.style.display = text ? 'flex' : 'none';
    if (text) {
      el.querySelector('span').textContent = text;
      this.$('play-btn').classList.add('hidden');
    }
  }

  _togglePlay() {
    const video = this.$('video');
    if (video.paused) this._play();
    else {
      video.pause();
      this.$('play-btn').classList.remove('hidden');
    }
  }

  // In fullscreen, moving to the next clip keeps playing.
  _isFullscreen() { return this.shadowRoot.fullscreenElement === this.$('player') || document.fullscreenElement === this; }
  _prev() { if (this._clipIndex < this._clips.length - 1) this._selectClip(this._clipIndex + 1, { play: this._isFullscreen() }); }
  _next() { if (this._clipIndex > 0) this._selectClip(this._clipIndex - 1, { play: this._isFullscreen() }); }

  // ── Fullscreen ──────────────────────────────────────────────────────

  /** Fullscreen the player itself: the same <video>, so nothing downloads twice. */
  _toggleFullscreen() {
    const player = this.$('player');
    const video = this.$('video');
    if (this._isFullscreen()) {
      (document.exitFullscreen || document.webkitExitFullscreen)?.call(document);
      return;
    }
    if (player.requestFullscreen) {
      player.requestFullscreen().catch(() => {});
    } else if (video.webkitEnterFullscreen) {
      // iPhone Safari only fullscreens video elements.
      video.webkitEnterFullscreen();
    }
  }

  _onCardKey(event) {
    // Only while the card itself has focus, so arrow keys stay usable
    // everywhere else on the dashboard.
    if (event.key === 'ArrowLeft') this._prev();
    else if (event.key === 'ArrowRight') this._next();
    else if (event.key === ' ' || event.key === 'Enter') this._togglePlay();
    else return;
    event.preventDefault();
  }

  // ── Rendering ───────────────────────────────────────────────────────

  $(id) { return this.shadowRoot.getElementById(id); }

  _setLoading(active) {
    const el = this.$('loading');
    if (el) el.classList.toggle('active', active);
  }

  _fail(message) {
    const placeholder = this.$('placeholder');
    if (!placeholder) return;
    this._setLoading(false);
    placeholder.style.display = 'flex';
    placeholder.querySelector('span').textContent = message;
    const video = this.$('video');
    if (video) { video.style.display = 'none'; video.removeAttribute('src'); }
    this.$('play-btn').classList.add('hidden');
    this.$('clip-count').textContent = 'Unavailable';
  }

  _showEmpty(message) {
    const label = PLURALS[this._eventType] || `${this._eventType}s`;
    const placeholder = this.$('placeholder');
    placeholder.style.display = 'flex';
    placeholder.querySelector('span').textContent = message || `No ${label} on this day`;
    const video = this.$('video');
    video.style.display = 'none';
    video.removeAttribute('src');
    this.$('play-btn').classList.add('hidden');
    this._currentClipId = null;
    this.$('cache-badge').style.display = 'none';
    this.$('event-badge').textContent = '';
    this.$('clip-time-inner').textContent = '';
    this.$('clip-name').textContent = 'No clip loaded';
    this._renderNav();
  }

  _renderSummary() {
    const label = PLURALS[this._eventType] || `${this._eventType}s`;
    const cached = this._clips.filter((clip) => clip.cached).length;
    const suffix = this._integration && this._clips.length
      ? ` · ${cached}/${this._clips.length} cached`
      : '';
    this.$('clip-count').textContent = `${this._clips.length} ${label}${suffix}`;
  }

  _renderClipChrome(clip) {
    const info = meta(clip.event_type);
    const start = new Date(clip.start);
    const time = start.toLocaleTimeString(undefined, {
      hour: 'numeric', minute: '2-digit', second: '2-digit',
    });
    const date = start.toLocaleDateString(undefined, {
      day: '2-digit', month: '2-digit', year: 'numeric', weekday: 'short',
    });

    this.$('event-badge').textContent = info.label;
    this.$('event-badge').className = `clip-badge ${info.color}`;
    this.$('clip-time-inner').textContent = time;
    this.$('clip-time').textContent = `${date} ${time}`;
    this.$('cache-badge').style.display = clip.cached ? 'inline-flex' : 'none';
    this.$('player-cam-label').textContent = (this._camera() || {}).name || '';
    this.$('clip-name').innerHTML =
      `${info.label} <span class="sep">·</span> ${time}` +
      (clip.duration ? ` <span class="sep">·</span> ${clip.duration}s` : '');
  }

  _renderNav() {
    const hasPrev = this._clipIndex < this._clips.length - 1;
    const hasNext = this._clipIndex > 0;
    this.$('prev-btn').disabled = !hasPrev;
    this.$('next-btn').disabled = !hasNext;
    if (this.$('fs-prev')) this.$('fs-prev').disabled = !hasPrev;
    if (this.$('fs-next')) this.$('fs-next').disabled = !hasNext;
    this.$('clip-index').textContent = this._clips.length
      ? `${this._clipIndex + 1} / ${this._clips.length}`
      : '— / —';
  }

  _renderDates() {
    const menu = this.$('date-menu');
    if (!menu) return;
    const today = this._todayISO();
    const yesterday = this._offsetISO(-1);
    const label = (iso) =>
      iso === today ? 'Today' : iso === yesterday ? 'Yesterday'
        : new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });

    menu.innerHTML = this._dates.map((entry) => `
      <div class="dropdown-option ${entry.date === this._selectedDate ? 'active' : ''}"
           data-date="${entry.date}">${label(entry.date)}</div>`).join('');

    menu.querySelectorAll('.dropdown-option').forEach((option) => {
      option.addEventListener('click', () => {
        this._selectedDate = option.dataset.date;
        this.$('date-dropdown').classList.remove('open');
        this._renderDates();
        this._loadClips();
      });
    });

    this.$('date-label').textContent = this._selectedDate ? label(this._selectedDate) : 'Today';
  }

  _renderEventChips() {
    const row = this.$('event-chips');
    if (!row) return;
    const present = new Set();
    for (const clip of this._allClips) (clip.event_types || []).forEach((t) => present.add(t));
    const types = ['all', ...Array.from(present).sort()];

    if (!present.has(this._eventType) && this._eventType !== 'all') this._eventType = 'all';

    row.innerHTML = types.map((type) => {
      const info = type === 'all' ? { label: 'All', color: 'taupe' } : meta(type);
      const count = type === 'all'
        ? this._allClips.length
        : this._allClips.filter((clip) => (clip.event_types || []).includes(type)).length;
      return `<button class="evt-chip ${info.color} ${this._eventType === type ? 'active' : ''}"
                data-type="${type}"><span class="evt-dot"></span>${info.label}
                <span class="evt-count">${count}</span></button>`;
    }).join('');

    row.querySelectorAll('.evt-chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        this._eventType = chip.dataset.type;
        this._applyFilterAndRender();
      });
    });
  }

  _renderFilmstrip() {
    const strip = this.$('filmstrip');
    if (!strip) return;
    if (!this._config.thumbnails || !this._clips.length) {
      strip.style.display = 'none';
      strip.innerHTML = '';
      return;
    }
    strip.style.display = 'flex';

    strip.innerHTML = this._clips.map((clip, index) => {
      const info = meta(clip.event_type);
      const time = new Date(clip.start).toLocaleTimeString(undefined, {
        hour: 'numeric', minute: '2-digit',
      });
      const inner = clip.thumbnail
        ? `<img src="${clip.thumbnail}" alt="" loading="lazy">`
        : `<div class="thumb-fallback ${info.color}"><svg viewBox="0 0 24 24">
             <rect x="2" y="6" width="13" height="12" rx="2"/><path d="M22 8l-5 4 5 4V8z"/></svg></div>`;
      return `<button class="thumb ${index === this._clipIndex ? 'active' : ''} ${info.color}"
                data-index="${index}" title="${info.label} ${time}">
                ${inner}
                <span class="thumb-time">${time}</span>
                ${clip.cached ? '<span class="thumb-cached"></span>' : ''}
              </button>`;
    }).join('');

    strip.querySelectorAll('.thumb').forEach((thumb) => {
      thumb.addEventListener('click', () => this._selectClip(Number(thumb.dataset.index)));
    });
    const active = strip.querySelector('.thumb.active');
    if (active) active.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' });
  }

  _refreshDetections(force = false) {
    const container = this.$('last-detection');
    const camera = this._camera();
    if (!container || !camera || !this._hass) return;

    const sensors = camera.sensors || {};
    const entries = Object.entries(sensors);
    if (!entries.length) { container.style.display = 'none'; return; }

    // Re-render only when a watched sensor actually moved, rather than on
    // every state change anywhere in Home Assistant.
    const snapshot = entries
      .map(([, entityId]) => {
        const state = this._hass.states[entityId];
        return state ? `${entityId}:${state.state}:${state.last_changed}` : `${entityId}:?`;
      })
      .join('|');
    if (!force && snapshot === this._sensorSnapshot) return;
    this._sensorSnapshot = snapshot;

    const chips = entries.map(([type, entityId]) => {
      const state = this._hass.states[entityId];
      if (!state) return '';
      const info = meta(type);
      const active = state.state === 'on';
      return `<span class="det-chip ${info.color} ${active ? 'active' : ''}">
                <span class="det-dot ${active ? 'pulse' : ''}"></span>
                <span class="det-label">${info.label}</span>
                <span class="det-time">${active ? 'now' : this._ago(state.last_changed)}</span>
              </span>`;
    }).join('');

    container.style.display = 'grid';
    container.innerHTML = chips;
  }

  _ago(timestamp) {
    const diff = Date.now() - new Date(timestamp).getTime();
    const seconds = Math.floor(diff / 1000);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h`;
    const days = Math.floor(hours / 24);
    if (days < 7) return `${days}d`;
    return new Date(timestamp).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  }

  _todayISO() {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
  }

  _offsetISO(days) {
    const date = new Date();
    date.setDate(date.getDate() + days);
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  }

  _closeDropdowns() {
    const dropdown = this.$('date-dropdown');
    if (dropdown) dropdown.classList.remove('open');
  }

  // ── NVR fallback (integration absent) ───────────────────────────────

  async _fallbackCameras() {
    try {
      const root = await this._hass.callWS({
        type: 'media_source/browse_media',
        media_content_id: 'media-source://reolink',
      });
      return (root.children || []).map((child) => ({
        key: slug(child.title),
        name: child.title,
        media_content_id: child.media_content_id,
        event_types: [],
        sensors: {},
      }));
    } catch (err) {
      return [];
    }
  }

  async _fallbackStreamId(camera) {
    const result = await this._hass.callWS({
      type: 'media_source/browse_media',
      media_content_id: camera.media_content_id,
    });
    const streams = (result.children || []).filter((child) =>
      bare(child.media_content_id).startsWith('RES|'));
    const preferred = streams.find((child) => bare(child.media_content_id).endsWith('|sub'));
    return (preferred || streams[0] || {}).media_content_id || null;
  }

  async _fallbackDates(camera) {
    const streamId = await this._fallbackStreamId(camera);
    if (!streamId) return [];
    const result = await this._hass.callWS({
      type: 'media_source/browse_media', media_content_id: streamId,
    });
    return (result.children || []).map((child) => {
      const parts = bare(child.media_content_id).split('|');
      if (parts.length !== 7 || parts[0] !== 'DAY') return null;
      const iso = `${parts[4]}-${String(parts[5]).padStart(2, '0')}-${String(parts[6]).padStart(2, '0')}`;
      return { date: iso, title: child.title };
    }).filter(Boolean).sort((a, b) => b.date.localeCompare(a.date));
  }

  async _fallbackClips(camera, isoDate) {
    const streamId = await this._fallbackStreamId(camera);
    if (!streamId) return [];
    const [year, month, day] = isoDate.split('-').map(Number);
    const dayId = uri(`${bare(streamId).replace(/^RES\|/, 'DAY|')}|${year}|${month}|${day}`);

    const dayResult = await this._hass.callWS({
      type: 'media_source/browse_media', media_content_id: dayId,
    });
    const folders = (dayResult.children || []).filter((child) =>
      bare(child.media_content_id).startsWith('EVE|'));

    const sources = folders.length
      ? (await Promise.all(folders.map((folder) =>
          this._hass.callWS({ type: 'media_source/browse_media', media_content_id: folder.media_content_id })
            .then((res) => ({ trigger: bare(folder.media_content_id).split('|').pop().toLowerCase(), res }))
            .catch(() => null))))
      : [{ trigger: null, res: dayResult }];

    const byId = new Map();
    for (const source of sources) {
      if (!source) continue;
      for (const child of source.res.children || []) {
        const clip = this._fallbackDescriptor(camera, child, source.trigger);
        if (!clip) continue;
        const existing = byId.get(clip.media_content_id);
        if (existing) {
          existing.event_types = Array.from(new Set([...existing.event_types, ...clip.event_types])).sort();
        } else {
          byId.set(clip.media_content_id, clip);
        }
      }
    }
    return Array.from(byId.values()).sort((a, b) => b.start.localeCompare(a.start));
  }

  _fallbackDescriptor(camera, child, trigger) {
    const id = String(child.media_content_id || '');
    const parts = bare(id).split('|');
    if (parts.length < 7 || parts[0] !== 'FILE') return null;
    const stamp = parts[parts.length - 2];
    const match = /^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$/.exec(stamp);
    if (!match) return null;
    const start = new Date(
      Number(match[1]), Number(match[2]) - 1, Number(match[3]),
      Number(match[4]), Number(match[5]), Number(match[6]),
    );

    const types = new Set();
    if (trigger) types.add(trigger === 'pet' ? 'animal' : trigger);
    const title = String(child.title || '').toLowerCase();
    for (const key of Object.keys(EVENT_META)) if (title.includes(key)) types.add(key);
    if (title.includes('pet')) types.add('animal');
    if (!types.size) types.add('other');

    const ordered = Array.from(types).sort();
    return {
      media_content_id: id,
      camera: camera.key,
      camera_name: camera.name,
      event_type: trigger || ordered[0],
      event_types: ordered,
      start: start.toISOString(),
      duration: null,
      cached: false,
      url: null,
      thumbnail: null,
      title: child.title,
    };
  }

  // ── Markup ──────────────────────────────────────────────────────────

  _render() {
    const cameras = this._cameras;
    const cols = Math.min(Math.max(cameras.length, 1), 4);

    this.shadowRoot.innerHTML = `
      <style>${this._styles(cols)}</style>

      <div class="card" id="card" tabindex="0">
        <div class="header">
          <div class="header-top">
            <div class="hue-icon">
              <svg viewBox="0 0 24 24"><rect x="2" y="6" width="13" height="12" rx="2"/><path d="M22 8l-5 4 5 4V8z"/></svg>
            </div>
            <div class="header-left">
              <div class="title">${esc(this._config.title)}</div>
              <div class="subtitle" id="clip-count">Loading…</div>
            </div>
            <button class="refresh-btn" id="refresh-btn" title="Refresh" aria-label="Refresh">
              <svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/></svg>
              <span aria-hidden="true">Refresh</span>
            </button>
          </div>
          <div id="last-detection"></div>
        </div>

        ${cameras.length > 1 ? `
          <div class="camera-tabs">
            ${cameras.map((camera, i) => `
              <button class="cam-tab${i === this._cameraIndex ? ' active' : ''}" data-idx="${i}">${esc(camera.name)}</button>
            `).join('')}
          </div>` : ''}

        <div class="filter-row">
          <div class="dropdown" id="date-dropdown">
            <button class="dropdown-btn" id="date-btn">
              <span class="label">
                <span class="lead"><svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg></span>
                <span id="date-label">Today</span>
              </span>
              <span class="chev"><svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg></span>
            </button>
            <div class="dropdown-menu" id="date-menu"></div>
          </div>
          <div class="event-chips" id="event-chips"></div>
        </div>

        <div class="player" id="player">
          <video id="video" playsinline preload="metadata"></video>
          <div class="player-placeholder" id="placeholder">
            <svg viewBox="0 0 24 24"><rect x="2" y="6" width="13" height="12" rx="2"/><path d="M22 8l-5 4 5 4V8z"/></svg>
            <span>Loading clips…</span>
          </div>
          <div class="play-btn hidden" id="play-btn">
            <div class="play-btn-icon"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg></div>
          </div>
          <div class="player-overlay">
            <span class="clip-badge" id="event-badge"></span>
            <span class="clip-time-inner" id="clip-time-inner"></span>
            <span class="cache-badge" id="cache-badge" style="display:none;">
              <svg viewBox="0 0 24 24"><path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="10"/></svg>
              CACHED
            </span>
          </div>
          <div class="player-overlay-right"><span class="clip-time-badge" id="clip-time"></span></div>
          <div class="player-cam-label" id="player-cam-label"></div>
          <button class="fullscreen-btn" id="fullscreen-btn" title="Fullscreen" aria-label="Fullscreen">
            <svg viewBox="0 0 24 24"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>
          </button>
          <div class="fs-nav">
            <button class="fs-nav-btn" id="fs-prev" aria-label="Previous"><svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg></button>
            <button class="fs-nav-btn" id="fs-next" aria-label="Next"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></button>
          </div>
          <div class="hint" id="hint" style="display:none"></div>
          <div class="error" id="error" style="display:none">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 8v5M12 16h.01"/></svg>
            <span></span>
            <button class="retry-btn" id="retry-btn">Try again</button>
          </div>
          <div class="loading" id="loading"><div class="spinner"></div></div>
        </div>

        <div class="clips-nav">
          <button class="nav-btn" id="prev-btn" disabled aria-label="Previous">
            <svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg>
          </button>
          <div class="clip-info">
            <div class="clip-name" id="clip-name">No clip loaded</div>
            <div class="clip-index" id="clip-index">— / —</div>
          </div>
          <button class="nav-btn" id="next-btn" disabled aria-label="Next">
            <svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
          </button>
        </div>

        <div class="filmstrip" id="filmstrip"></div>
      </div>
    `;

    this._attachEvents();
    this._refreshDetections(true);
  }

  _attachEvents() {
    document.removeEventListener('click', this._docClick);
    document.addEventListener('click', this._docClick);

    this.$('card').addEventListener('keydown', (event) => this._onCardKey(event));

    this.shadowRoot.querySelectorAll('.cam-tab').forEach((tab) => {
      tab.addEventListener('click', () => {
        this._cameraIndex = Number(tab.dataset.idx);
        this.shadowRoot.querySelectorAll('.cam-tab')
          .forEach((other, i) => other.classList.toggle('active', i === this._cameraIndex));
        this._selectedDate = null;
        this._allClips = [];
        this._clips = [];
        this._prefetched.clear();
        this._refreshDetections(true);
        this._loadDates();
      });
    });

    this.$('refresh-btn').addEventListener('click', () => this._loadClips());

    const video = this.$('video');
    this.$('play-btn').addEventListener('click', () => this._play());
    video.addEventListener('click', () => this._togglePlay());
    video.addEventListener('ended', () => this.$('play-btn').classList.remove('hidden'));
    video.addEventListener('error', () => this._onVideoError());
    // Show the spinner whenever playback is waiting on data, and only then.
    video.addEventListener('waiting', () => this._setLoading(true));
    video.addEventListener('stalled', () => { if (!video.paused) this._setLoading(true); });
    for (const ev of ['playing', 'canplay', 'pause', 'emptied']) video.addEventListener(ev, () => this._setLoading(false));
    video.addEventListener('playing', () => this.$('play-btn').classList.add('hidden'));
    this.$('retry-btn').addEventListener('click', () => {
      const clip = this._clips[this._clipIndex];
      if (clip) clip.url = clip.cached ? clip.url : null;
      this._selectClip(this._clipIndex, { play: true });
    });

    this.$('prev-btn').addEventListener('click', () => this._prev());
    this.$('next-btn').addEventListener('click', () => this._next());

    this.$('fullscreen-btn').addEventListener('click', () => this._toggleFullscreen());
    this.$('player').addEventListener('dblclick', () => this._toggleFullscreen());
    this.$('fs-prev').addEventListener('click', () => this._prev());
    this.$('fs-next').addEventListener('click', () => this._next());

    this.$('date-btn').addEventListener('click', (event) => {
      event.stopPropagation();
      this.$('date-dropdown').classList.toggle('open');
    });
    this.$('date-menu').addEventListener('click', (event) => event.stopPropagation());
  }

  _styles(cols) {
    return `
      *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

      /* Cyborg themes: dark is the CAMusic OLED look, coffee the warm espresso look. */
      :host {
        display: block;
        --onyx:        #050506;
        --card-bg:     linear-gradient(180deg, #0B0C0E 0%, #000 62%);
        --coffee-800:  #141518;
        --taupe:       #D9A85C;
        --taupe-soft:  #F0D2A0;
        --on-accent:   #0B0B0C;
        --fg:          #FFFFFF;
        --fg-dim:      rgba(255,255,255,.70);
        --fg-muted:    rgba(255,255,255,.45);
        --line:        rgba(255,255,255,.09);
        --line-strong: rgba(255,255,255,.16);
        --glass:       rgba(255,255,255,.04);
        --glass2:      rgba(255,255,255,.07);
        --c-taupe: rgba(255,255,255,.62);
        --c-sand:  #D9A85C;
        --c-rust:  #7A8FD9;
        --c-moss:  #3ECF7A;
        --c-clay:  #E8A42C;
        --c-cache: #3ECF7A;
        --radius-card: 22px;
        --radius-btn:  12px;
        --radius-sm:   9px;
        font-family: Manrope, Inter, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
      }
      :host([theme="coffee"]) {
        --onyx:        #141210;
        --card-bg:     linear-gradient(180deg, #1F1C17 0%, #141210 70%);
        --coffee-800:  #26231D;
        --taupe:       #E4A667;
        --taupe-soft:  #F2C9A0;
        --on-accent:   #1A1209;
        --fg:          #F5F3EF;
        --fg-dim:      #C9C3B8;
        --fg-muted:    #8F887C;
        --line:        #302C25;
        --line-strong: #403B32;
        --glass:       rgba(245,236,220,.045);
        --glass2:      rgba(245,236,220,.075);
        --c-taupe: #C9C3B8;
        --c-sand:  #E4A667;
        --c-rust:  #8FA6E0;
        --c-moss:  #7BC68F;
        --c-clay:  #E8A42C;
      }

      .card {
        position: relative; isolation: isolate; overflow: hidden;
        background: var(--card-bg);
        border: 1px solid var(--line);
        border-radius: var(--radius-card);
        padding: 16px; color: var(--fg);
        display: flex; flex-direction: column; gap: 12px;
        outline: none;
        box-shadow: 0 30px 70px -30px rgba(0,0,0,.85);
        -webkit-font-smoothing: antialiased;
      }
      .card::before {
        content: ""; position: absolute; inset: -40% 30% auto -30%; height: 90%; z-index: -1; pointer-events: none;
        background: radial-gradient(closest-side, color-mix(in srgb, var(--taupe) 16%, transparent), transparent);
      }
      .card:focus-visible { border-color: var(--line-strong); box-shadow: 0 0 0 2px color-mix(in srgb, var(--taupe) 40%, transparent); }

      .header { display: flex; flex-direction: column; gap: 8px; }
      .header-top { display: flex; align-items: center; gap: 12px; }
      .hue-icon {
        width: 40px; height: 40px; border-radius: 12px; background: color-mix(in srgb, var(--taupe) 14%, transparent); border: 1px solid color-mix(in srgb, var(--taupe) 26%, transparent);
        display: flex; align-items: center; justify-content: center; flex-shrink: 0;
        box-shadow: 0 2px 0 rgba(0,0,0,0.25), inset 0 1px 0 rgba(255,255,255,0.08);
      }
      .hue-icon svg { width: 20px; height: 20px; stroke: var(--taupe); fill: none; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; }
      .header-left { flex: 1; min-width: 0; }
      .title { font-size: 15px; font-weight: 800; letter-spacing: -.01em; }
      .subtitle { font-size: 12px; color: var(--fg-muted); margin-top: 2px; font-weight: 500; }

      #last-detection { display: none; grid-template-columns: repeat(auto-fit, minmax(0, 1fr)); gap: 5px; max-width: 480px; }
      .det-chip {
        display: flex; align-items: center; justify-content: center; gap: 4px;
        padding: 4px 8px; border-radius: 999px; font-size: 11px; font-weight: 500;
        overflow: hidden; min-width: 0;
      }
      .det-dot { width: 6px; height: 6px; border-radius: 50%; box-shadow: 0 0 0 2px rgba(0,0,0,0.35); flex-shrink: 0; }
      .det-dot.pulse { animation: pulse 1.6s ease-in-out infinite; }
      .det-label, .det-time { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
      .det-time { opacity: 0.65; }
      @keyframes pulse { 0%,100% { opacity: 0.35; transform: scale(0.85); } 50% { opacity: 1; transform: scale(1.1); } }

      .sand, .rust, .moss, .clay, .taupe {
        background: color-mix(in srgb, var(--ec) 13%, transparent);
        border: 1px solid color-mix(in srgb, var(--ec) 36%, transparent);
        color: var(--ec);
      }
      .sand { --ec: var(--c-sand); } .rust { --ec: var(--c-rust); } .moss { --ec: var(--c-moss); }
      .clay { --ec: var(--c-clay); } .taupe { --ec: var(--c-taupe); }
      .sand  .det-dot, .sand  .evt-dot { background: var(--c-sand);  }
      .rust  .det-dot, .rust  .evt-dot { background: var(--c-rust);  }
      .moss  .det-dot, .moss  .evt-dot { background: var(--c-moss);  }
      .clay  .det-dot, .clay  .evt-dot { background: var(--c-clay);  }
      .taupe .det-dot, .taupe .evt-dot { background: var(--c-taupe); }

      .refresh-btn {
        height: 40px; padding: 0 14px; background: var(--glass);
        border: 1px solid var(--line); border-radius: var(--radius-btn);
        color: var(--fg-dim); cursor: pointer; display: flex; align-items: center;
        justify-content: center; gap: 6px; flex-shrink: 0; white-space: nowrap;
        font-size: 12px; font-weight: 600; letter-spacing: 0.3px; transition: background 0.15s, color 0.15s;
      }
      .refresh-btn:hover { background: var(--glass2); color: var(--fg); }
      .refresh-btn svg { width: 15px; height: 15px; stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }

      .camera-tabs { display: grid; grid-template-columns: repeat(${cols}, 1fr); gap: 8px; }
      .cam-tab {
        height: 40px; font-size: 12px; font-weight: 600; letter-spacing: 0.6px;
        text-transform: uppercase; background: transparent; border: 1px solid var(--line);
        border-radius: var(--radius-btn); color: var(--fg-muted); cursor: pointer; transition: all 0.15s; font-family: inherit;
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 0 8px;
      }
      .cam-tab:hover { background: var(--glass); color: var(--fg); }
      .cam-tab.active { background: var(--taupe); color: var(--on-accent); border-color: var(--taupe); box-shadow: 0 6px 16px -6px color-mix(in srgb, var(--taupe) 80%, transparent); }

      .filter-row { display: grid; grid-template-columns: minmax(120px, 180px) 1fr; gap: 10px; position: relative; z-index: 10; align-items: start; }
      @media (max-width: 460px) { .filter-row { grid-template-columns: 1fr; } }
      .dropdown { position: relative; }
      .dropdown-btn {
        width: 100%; height: 40px; padding: 0 12px; font-size: 13px; font-weight: 500;
        background: var(--glass); border: 1px solid var(--line);
        border-radius: var(--radius-btn); color: var(--fg); cursor: pointer; font-family: inherit;
        display: flex; align-items: center; gap: 8px; transition: border-color 0.15s, background 0.15s;
      }
      .dropdown-btn:hover { border-color: var(--line-strong); background: var(--glass2); }
      .dropdown-btn .label { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; overflow: hidden; }
      .dropdown-btn .label span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .dropdown-btn .lead { color: var(--taupe); display: inline-flex; }
      .dropdown-btn .lead svg, .dropdown-btn .chev svg { width: 14px; height: 14px; stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
      .dropdown-btn .chev { margin-left: auto; color: var(--fg-muted); flex-shrink: 0; }
      .dropdown.open .chev svg { transform: rotate(180deg); }
      .dropdown-menu {
        display: none; position: absolute; top: calc(100% + 4px); left: 0; right: 0;
        background: var(--coffee-800); border: 1px solid var(--line-strong);
        border-radius: var(--radius-btn); padding: 6px; max-height: 260px; overflow-y: auto;
        z-index: 100; box-shadow: 0 10px 30px rgba(0,0,0,0.55);
      }
      .dropdown.open .dropdown-menu { display: block; }
      .dropdown-option {
        padding: 9px 11px; font-size: 12px; font-weight: 500; color: var(--fg);
        cursor: pointer; border-radius: var(--radius-sm); transition: background 0.15s;
      }
      .dropdown-option:hover { background: var(--glass2); }
      .dropdown-option.active { background: var(--taupe); color: var(--on-accent); }
      .dropdown-menu::-webkit-scrollbar { width: 4px; }
      .dropdown-menu::-webkit-scrollbar-thumb { background: var(--fg-muted); border-radius: 2px; }

      .event-chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; min-height: 40px; }
      .evt-chip {
        display: inline-flex; align-items: center; gap: 6px; height: 32px; padding: 0 11px;
        border-radius: 999px; font-size: 12px; font-weight: 600; cursor: pointer;
        opacity: 0.55; transition: opacity 0.15s, transform 0.15s; font-family: inherit;
      }
      .evt-chip:hover { opacity: 0.85; }
      .evt-chip.active { opacity: 1; }
      .evt-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
      .evt-count { opacity: 0.6; font-weight: 500; font-variant-numeric: tabular-nums; }

      .player { position: relative; aspect-ratio: 16/9; background: #000; border-radius: 16px; overflow: hidden; border: 1px solid var(--line); }
      .player video { width: 100%; height: 100%; object-fit: contain; display: none; background: #000; }
      .player-placeholder { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; color: var(--fg-muted); }
      .player-placeholder svg { width: 44px; height: 44px; stroke: var(--fg-muted); fill: none; stroke-width: 1.5; opacity: 0.4; }
      .player-placeholder span { font-size: 13px; text-align: center; padding: 0 20px; }

      .play-btn { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; cursor: pointer; }
      .play-btn.hidden { display: none; }
      .play-btn-icon {
        width: 72px; height: 72px; background: var(--taupe); border: 0; box-shadow: 0 10px 26px -6px color-mix(in srgb, var(--taupe) 70%, transparent);
        border-radius: 50%; display: flex; align-items: center; justify-content: center;
        transition: transform 0.2s, background 0.2s, box-shadow 0.2s;
      }
      .play-btn:hover .play-btn-icon { transform: scale(1.06); }
      .play-btn-icon svg { width: 28px; height: 28px; fill: var(--on-accent); transform: translateX(2px); }

      .player-overlay { position: absolute; top: 12px; left: 12px; display: flex; gap: 6px; align-items: center; pointer-events: none; flex-wrap: wrap; }
      .player-overlay-right { position: absolute; top: 14px; right: 12px; pointer-events: none; }
      .player-cam-label {
        position: absolute; bottom: 12px; left: 12px; font-size: 10px; letter-spacing: 1px;
        color: rgba(255,255,255,.75); text-shadow: 0 1px 0 rgba(0,0,0,0.6);
        text-transform: uppercase; pointer-events: none; font-family: 'JetBrains Mono', monospace;
      }
      .clip-badge {
        display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 10px;
        border-radius: 8px; font-size: 11px; font-weight: 700; letter-spacing: 1px;
        text-transform: uppercase; box-shadow: 0 2px 8px rgba(0,0,0,0.4);
      }
      .clip-badge::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
      .cache-badge {
        display: inline-flex; align-items: center; gap: 4px; height: 22px; padding: 0 7px;
        border-radius: 6px; font-size: 9px; font-weight: 700; letter-spacing: 0.8px;
        background: color-mix(in srgb, var(--c-cache) 15%, transparent); border: 1px solid color-mix(in srgb, var(--c-cache) 35%, transparent); color: var(--c-cache);
      }
      .cache-badge svg { width: 10px; height: 10px; stroke: currentColor; fill: none; stroke-width: 2.4; }
      .clip-time-inner {
        display: inline-flex; align-items: center; height: 28px; padding: 0 10px;
        background: rgba(0,0,0,.65); color: #fff; border-radius: 8px;
        font-size: 12px; font-weight: 600; border: 1px solid var(--line);
        font-family: 'JetBrains Mono', monospace;
      }
      .clip-time-badge { font-size: 10px; color: rgba(255,255,255,.75); letter-spacing: 1px; text-shadow: 0 1px 0 rgba(0,0,0,0.6); font-family: 'JetBrains Mono', monospace; }

      .fullscreen-btn {
        position: absolute; bottom: 10px; right: 10px; width: 34px; height: 34px;
        background: rgba(0,0,0,.55); border: 1px solid rgba(255,255,255,.14); border-radius: 10px;
        cursor: pointer; display: flex; align-items: center; justify-content: center;
        opacity: 0; transition: opacity 0.2s;
      }
      .player:hover .fullscreen-btn { opacity: 1; }
      @media (hover: none) { .fullscreen-btn { opacity: 0.8; } }
      .fullscreen-btn svg { width: 17px; height: 17px; stroke: #fff; fill: none; stroke-width: 2; }

      .loading { position: absolute; inset: 0; display: none; align-items: center; justify-content: center; background: rgba(0,0,0,.35); pointer-events: none; }
      .loading.active { display: flex; }
      .spinner { width: 36px; height: 36px; border: 3px solid rgba(255,255,255,.14); border-top-color: var(--taupe); border-radius: 50%; animation: spin 0.9s linear infinite; }
      @keyframes spin { to { transform: rotate(360deg); } }

      .clips-nav { display: grid; grid-template-columns: 40px 1fr 40px; align-items: center; gap: 8px; }
      .nav-btn {
        width: 40px; height: 40px; background: var(--glass);
        border: 1px solid var(--line); border-radius: var(--radius-btn);
        color: var(--fg-dim); cursor: pointer; display: flex; align-items: center;
        justify-content: center; transition: all 0.15s;
      }
      .nav-btn:hover:not(:disabled) { background: var(--glass2); color: var(--taupe); border-color: var(--line-strong); }
      .nav-btn:disabled { opacity: 0.45; cursor: not-allowed; }
      .nav-btn svg { width: 16px; height: 16px; stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
      .clip-info { text-align: center; min-width: 0; }
      .clip-name { font-size: 14px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .clip-name .sep { color: var(--fg-muted); margin: 0 6px; font-weight: 400; }
      .clip-index { font-size: 12px; color: var(--fg-muted); margin-top: 4px; font-variant-numeric: tabular-nums; }

      .filmstrip {
        display: flex; gap: 8px; overflow-x: auto; padding: 2px 2px 6px;
        scroll-snap-type: x proximity; scrollbar-width: thin;
      }
      .filmstrip::-webkit-scrollbar { height: 5px; }
      .filmstrip::-webkit-scrollbar-thumb { background: var(--fg-muted); border-radius: 3px; }
      .thumb {
        position: relative; flex: 0 0 auto; width: 104px; height: 62px; padding: 0;
        border-radius: var(--radius-sm); overflow: hidden; cursor: pointer;
        background: #000; border: 1.5px solid var(--line);
        scroll-snap-align: center; transition: border-color 0.15s, transform 0.15s;
      }
      .thumb:hover { transform: translateY(-1px); }
      .thumb.active { border-color: currentColor; box-shadow: 0 0 0 1px currentColor; }
      .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
      .thumb-fallback { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; border: none; }
      .thumb-fallback svg { width: 22px; height: 22px; stroke: currentColor; fill: none; stroke-width: 1.6; opacity: 0.5; }
      .thumb-time {
        position: absolute; bottom: 0; left: 0; right: 0; padding: 2px 4px;
        font-size: 10px; font-weight: 700; color: #fff;
        background: linear-gradient(transparent, rgba(0,0,0,.85));
        font-family: 'JetBrains Mono', monospace; letter-spacing: 0.2px;
      }
      .thumb-cached { position: absolute; top: 4px; right: 4px; width: 6px; height: 6px; border-radius: 50%; background: var(--c-cache); box-shadow: 0 0 0 2px rgba(0,0,0,.6); }

      .hint { position: absolute; left: 50%; bottom: 14px; transform: translateX(-50%); max-width: 90%; padding: 5px 10px; border-radius: 999px;
        background: rgba(0,0,0,.6); color: rgba(255,255,255,.8); font-size: 11px; text-align: center; pointer-events: none; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .error { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; padding: 16px;
        background: rgba(0,0,0,.72); color: #fff; text-align: center; font-size: 13px; }
      .error svg { width: 30px; height: 30px; stroke: var(--c-clay); fill: none; stroke-width: 2; stroke-linecap: round; }
      .retry-btn { height: 36px; padding: 0 16px; border-radius: 999px; border: 0; background: var(--taupe); color: var(--on-accent); font: inherit; font-weight: 800; cursor: pointer; }
      .fs-nav { display: none; position: absolute; bottom: 24px; left: 50%; transform: translateX(-50%); gap: 12px; }
      .player:fullscreen { border-radius: 0; border: 0; aspect-ratio: auto; }
      .player:fullscreen .fs-nav { display: flex; }
      .player:fullscreen .fullscreen-btn { opacity: .8; }
      .fs-nav-btn {
        width: 52px; height: 52px; background: rgba(0,0,0,.6);
        border: 1px solid rgba(255,255,255,.18); border-radius: 16px; cursor: pointer;
        display: flex; align-items: center; justify-content: center;
      }
      .fs-nav-btn:disabled { opacity: 0.4; cursor: not-allowed; }
      .fs-nav-btn svg { width: 26px; height: 26px; stroke: #fff; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
      @media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition-duration: .01ms !important; } }
    `;
  }
}

// ── Visual editor ─────────────────────────────────────────────────────

class ReolinkClipsCardEditor extends HTMLElement {
  constructor() {
    super();
    this._config = {};
    this._cameras = [];
    this._form = null;
  }

  setConfig(config) {
    this._config = { ...config };
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) {
      this._loaded = true;
      hass.callWS({ type: 'reolink_clip_cache/cameras' })
        .then((result) => { this._cameras = result.cameras || []; this._render(); })
        .catch(() => this._render());
    }
    if (this._form) this._form.hass = hass;
  }

  _schema() {
    return [
      {
        name: 'theme',
        selector: { select: { mode: 'dropdown', options: [
          { value: 'dark', label: 'Dark (CAMusic OLED)' },
          { value: 'coffee', label: 'Coffee (warm)' },
        ] } },
      },
      { name: 'title', selector: { text: {} } },
      {
        name: 'cameras',
        selector: {
          select: {
            multiple: true,
            mode: 'list',
            options: this._cameras.map((camera) => ({ value: camera.key, label: camera.name })),
          },
        },
      },
      {
        name: 'default_event_type',
        selector: {
          select: {
            mode: 'dropdown',
            options: [
              { value: 'all', label: 'All events' },
              ...Object.entries(EVENT_META)
                .filter(([key]) => key !== 'other')
                .map(([key, info]) => ({ value: key, label: info.label })),
            ],
          },
        },
      },
      { name: 'thumbnails', selector: { boolean: {} } },
      { name: 'autoplay', selector: { boolean: {} } },
    ];
  }

  _render() {
    if (!this._form) {
      this.innerHTML = '';
      this._form = document.createElement('ha-form');
      this._form.computeLabel = (schema) => ({
        theme: 'Theme',
        title: 'Card title',
        cameras: 'Cameras (all if none selected)',
        default_event_type: 'Event type shown first',
        thumbnails: 'Show the thumbnail filmstrip',
        autoplay: 'Play the newest clip automatically',
      }[schema.name] || schema.name);
      this._form.addEventListener('value-changed', (event) => {
        this.dispatchEvent(new CustomEvent('config-changed', {
          detail: { config: { ...this._config, ...event.detail.value } },
          bubbles: true, composed: true,
        }));
      });
      this.appendChild(this._form);
    }
    this._update();
  }

  _update() {
    if (!this._form) return;
    if (this._hass) this._form.hass = this._hass;
    this._form.schema = this._schema();
    this._form.data = {
      theme: 'dark',
      title: 'Events',
      default_event_type: 'all',
      thumbnails: true,
      autoplay: false,
      ...this._config,
      cameras: (this._config.cameras || [])
        .map((camera) => (typeof camera === 'string' ? camera : slug(camera && camera.name))),
    };
  }
}

if (!customElements.get('reolink-clips-card')) customElements.define('reolink-clips-card', ReolinkClipsCard);
if (!customElements.get('reolink-clips-card-editor')) customElements.define('reolink-clips-card-editor', ReolinkClipsCardEditor);

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === 'reolink-clips-card')) window.customCards.push({
  type: 'reolink-clips-card',
  name: 'Reolink Clips Card',
  description: 'Person, vehicle and animal clips from your Reolink NVR, played instantly from the local cache.',
  preview: true,
  documentationURL: 'https://github.com/engabd11/reolink-clips',
});

console.info(
  `%c REOLINK-CLIPS %c v${CARD_VERSION} `,
  'background:#9A8873;color:#1a130d;font-weight:bold;padding:2px 6px;border-radius:4px 0 0 4px',
  'background:#4ade80;color:#171614;padding:2px 6px;border-radius:0 4px 4px 0',
);

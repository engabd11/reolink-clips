/**
 * Reolink Clips Card — Earthy Dark Edition v2.0
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

const CARD_VERSION = '2.0.0';

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
    this._docClick = () => this._closeDropdowns();
    this._docKeys = (event) => this._onFullscreenKey(event);
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
      ...config,
    };
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
    document.removeEventListener('keydown', this._docKeys);
    if (this._detectionTimer) clearInterval(this._detectionTimer);
    this._detectionTimer = null;
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
    if (this._loading) return;

    this._loading = true;
    if (!silent) this._setLoading(true);
    const previousId = keepPosition && this._clips[this._clipIndex]
      ? this._clips[this._clipIndex].media_content_id
      : null;

    try {
      this._allClips = this._integration
        ? (await this._hass.callWS({
            type: 'reolink_clip_cache/clips',
            camera: camera.key,
            date: this._selectedDate,
          })).clips
        : await this._fallbackClips(camera, this._selectedDate);
    } catch (err) {
      this._allClips = [];
      this._loading = false;
      if (!silent) this._fail(err.message || 'Could not load clips.');
      return;
    }

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

  async _selectClip(index) {
    if (index < 0 || index >= this._clips.length) return;
    this._clipIndex = index;
    this._retriedClip = null;
    const clip = this._clips[index];
    this._currentClipId = null;

    const video = this.$('video');
    const placeholder = this.$('placeholder');
    const playBtn = this.$('play-btn');

    this._renderClipChrome(clip);
    this._renderNav();
    this._renderFilmstrip();

    let url = clip.url;
    if (!url) {
      this._setLoading(true);
      url = await this._resolve(clip);
      this._setLoading(false);
    }
    if (!url) {
      this._retriedClip = clip.media_content_id;
      this._showEmpty('This clip could not be loaded from the NVR.');
      return;
    }

    video.pause();
    video.poster = clip.thumbnail || '';
    video.src = url;
    video.style.display = 'block';
    placeholder.style.display = 'none';
    playBtn.classList.remove('hidden');

    this._currentClipId = clip.media_content_id;

    if (this._config.autoplay) this._play();
    this._prefetchNeighbours();
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
    const clip = this._clips[this._clipIndex];
    if (!clip || this._retriedClip === clip.media_content_id) return;
    // Signed URLs expire; ask for a fresh one once before giving up.
    this._retriedClip = clip.media_content_id;
    clip.url = null;
    this._selectClip(this._clipIndex);
  }

  _play() {
    const video = this.$('video');
    video.play().then(() => this.$('play-btn').classList.add('hidden')).catch(() => {});
  }

  _togglePlay() {
    const video = this.$('video');
    if (video.paused) this._play();
    else {
      video.pause();
      this.$('play-btn').classList.remove('hidden');
    }
  }

  _prev() { if (this._clipIndex < this._clips.length - 1) this._selectClip(this._clipIndex + 1); }
  _next() { if (this._clipIndex > 0) this._selectClip(this._clipIndex - 1); }

  // ── Fullscreen ──────────────────────────────────────────────────────

  _openFullscreen() {
    const video = this.$('video');
    const fs = this.$('fs-video');
    fs.src = video.src;
    fs.poster = video.poster;
    fs.currentTime = video.currentTime;
    this.$('fs-title').textContent = (this._camera() || {}).name || '';
    this.$('fs-subtitle').textContent = this.$('clip-name').textContent;
    this.$('fs-overlay').classList.add('active');
    video.pause();
    fs.play().catch(() => {});
    document.addEventListener('keydown', this._docKeys);
  }

  _closeFullscreen() {
    const fs = this.$('fs-video');
    const video = this.$('video');
    video.currentTime = fs.currentTime;
    fs.pause();
    this.$('fs-overlay').classList.remove('active');
    this.$('play-btn').classList.remove('hidden');
    document.removeEventListener('keydown', this._docKeys);
  }

  _syncFullscreen() {
    const fs = this.$('fs-video');
    const video = this.$('video');
    fs.src = video.src;
    fs.poster = video.poster;
    fs.play().catch(() => {});
    this.$('fs-subtitle').textContent = this.$('clip-name').textContent;
  }

  _onFullscreenKey(event) {
    if (!this.$('fs-overlay').classList.contains('active')) return;
    if (event.key === 'Escape') this._closeFullscreen();
    else if (event.key === 'ArrowLeft') { this._prev(); this._syncFullscreen(); }
    else if (event.key === 'ArrowRight') { this._next(); this._syncFullscreen(); }
    else return;
    event.preventDefault();
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
    this.$('fs-prev').disabled = !hasPrev;
    this.$('fs-next').disabled = !hasNext;
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
      String(child.media_content_id).startsWith('RES|'));
    const preferred = streams.find((child) => child.media_content_id.endsWith('|sub'));
    return (preferred || streams[0] || {}).media_content_id || null;
  }

  async _fallbackDates(camera) {
    const streamId = await this._fallbackStreamId(camera);
    if (!streamId) return [];
    const result = await this._hass.callWS({
      type: 'media_source/browse_media', media_content_id: streamId,
    });
    return (result.children || []).map((child) => {
      const parts = String(child.media_content_id).split('|');
      if (parts.length !== 7 || parts[0] !== 'DAY') return null;
      const iso = `${parts[4]}-${String(parts[5]).padStart(2, '0')}-${String(parts[6]).padStart(2, '0')}`;
      return { date: iso, title: child.title };
    }).filter(Boolean).sort((a, b) => b.date.localeCompare(a.date));
  }

  async _fallbackClips(camera, isoDate) {
    const streamId = await this._fallbackStreamId(camera);
    if (!streamId) return [];
    const [year, month, day] = isoDate.split('-').map(Number);
    const dayId = `${streamId.replace(/^RES\|/, 'DAY|')}|${year}|${month}|${day}`;

    const dayResult = await this._hass.callWS({
      type: 'media_source/browse_media', media_content_id: dayId,
    });
    const folders = (dayResult.children || []).filter((child) =>
      String(child.media_content_id).startsWith('EVE|'));

    const sources = folders.length
      ? (await Promise.all(folders.map((folder) =>
          this._hass.callWS({ type: 'media_source/browse_media', media_content_id: folder.media_content_id })
            .then((res) => ({ trigger: String(folder.media_content_id).split('|').pop().toLowerCase(), res }))
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
    const parts = id.split('|');
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

      <div class="fs-overlay" id="fs-overlay">
        <div class="fs-video"><video id="fs-video" playsinline controls></video></div>
        <div class="fs-info">
          <div class="fs-title" id="fs-title">Camera</div>
          <div class="fs-subtitle" id="fs-subtitle">Event</div>
        </div>
        <button class="fs-close" id="fs-close" aria-label="Close">
          <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
        <div class="fs-nav">
          <button class="fs-nav-btn" id="fs-prev" aria-label="Previous"><svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg></button>
          <button class="fs-nav-btn" id="fs-next" aria-label="Next"><svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></button>
        </div>
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

    this.$('prev-btn').addEventListener('click', () => this._prev());
    this.$('next-btn').addEventListener('click', () => this._next());

    this.$('fullscreen-btn').addEventListener('click', () => this._openFullscreen());
    this.$('fs-close').addEventListener('click', () => this._closeFullscreen());
    this.$('fs-prev').addEventListener('click', () => { this._prev(); this._syncFullscreen(); });
    this.$('fs-next').addEventListener('click', () => { this._next(); this._syncFullscreen(); });

    this.$('date-btn').addEventListener('click', (event) => {
      event.stopPropagation();
      this.$('date-dropdown').classList.toggle('open');
    });
    this.$('date-menu').addEventListener('click', (event) => event.stopPropagation());
  }

  _styles(cols) {
    return `
      *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

      :host {
        --onyx:        #171614;
        --coffee-800:  #3A2618;
        --taupe:       #9A8873;
        --taupe-soft:  #d6c7b3;
        --fg:          #ece4d6;
        --fg-dim:      #bdb09c;
        --fg-muted:    #8a7e6d;
        --line:        rgba(154,136,115,0.18);
        --line-strong: rgba(154,136,115,0.30);
        --c-taupe: #9A8873;
        --c-rust:  #b86b4a;
        --c-moss:  #7a8d76;
        --c-clay:  #8a4d50;
        --c-sand:  #c9b58e;
        --c-cache: #4ade80;
        --tint-taupe: rgba(154,136,115,0.16);
        --tint-rust:  rgba(184,107, 74,0.18);
        --tint-moss:  rgba(122,141,118,0.16);
        --tint-clay:  rgba(138, 77, 80,0.20);
        --tint-sand:  rgba(201,181,142,0.16);
        --tint-cache: rgba(74,222,128,0.15);
        --radius-card: 18px;
        --radius-btn:  10px;
        --radius-sm:   8px;
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      }

      .card {
        background: var(--onyx);
        border: 1px solid var(--line);
        border-radius: var(--radius-card);
        padding: 14px; color: var(--fg);
        display: flex; flex-direction: column; gap: 12px;
        outline: none;
      }
      .card:focus-visible { border-color: var(--line-strong); box-shadow: 0 0 0 2px rgba(154,136,115,0.25); }

      .header { display: flex; flex-direction: column; gap: 8px; }
      .header-top { display: flex; align-items: center; gap: 12px; }
      .hue-icon {
        width: 44px; height: 44px; border-radius: 10px; background: var(--taupe);
        display: flex; align-items: center; justify-content: center; flex-shrink: 0;
        box-shadow: 0 2px 0 rgba(0,0,0,0.25), inset 0 1px 0 rgba(255,255,255,0.08);
      }
      .hue-icon svg { width: 22px; height: 22px; stroke: #1a130d; fill: none; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; }
      .header-left { flex: 1; min-width: 0; }
      .title { font-size: 18px; font-weight: 700; letter-spacing: 0.1px; }
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

      .sand  { background: var(--tint-sand);  border: 1px solid rgba(201,181,142,0.35); color: var(--c-sand); }
      .rust  { background: var(--tint-rust);  border: 1px solid rgba(184,107,74,0.40);  color: #d4906f; }
      .moss  { background: var(--tint-moss);  border: 1px solid rgba(122,141,118,0.38); color: #a3b89e; }
      .clay  { background: var(--tint-clay);  border: 1px solid rgba(138,77,80,0.38);   color: #e3b9bb; }
      .taupe { background: var(--tint-taupe); border: 1px solid rgba(154,136,115,0.35); color: var(--taupe-soft); }
      .sand  .det-dot, .sand  .evt-dot { background: var(--c-sand);  }
      .rust  .det-dot, .rust  .evt-dot { background: var(--c-rust);  }
      .moss  .det-dot, .moss  .evt-dot { background: var(--c-moss);  }
      .clay  .det-dot, .clay  .evt-dot { background: var(--c-clay);  }
      .taupe .det-dot, .taupe .evt-dot { background: var(--c-taupe); }

      .refresh-btn {
        height: 44px; padding: 0 14px; background: rgba(255,255,255,0.025);
        border: 1px solid var(--line); border-radius: var(--radius-btn);
        color: var(--fg-dim); cursor: pointer; display: flex; align-items: center;
        justify-content: center; gap: 6px; flex-shrink: 0; white-space: nowrap;
        font-size: 12px; font-weight: 600; letter-spacing: 0.3px; transition: background 0.15s, color 0.15s;
      }
      .refresh-btn:hover { background: rgba(255,255,255,0.04); color: var(--fg); }
      .refresh-btn svg { width: 15px; height: 15px; stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }

      .camera-tabs { display: grid; grid-template-columns: repeat(${cols}, 1fr); gap: 8px; }
      .cam-tab {
        height: 40px; font-size: 12px; font-weight: 600; letter-spacing: 0.6px;
        text-transform: uppercase; background: transparent; border: 1px solid var(--line);
        border-radius: var(--radius-btn); color: var(--fg-muted); cursor: pointer; transition: all 0.15s;
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 0 8px;
      }
      .cam-tab:hover { background: rgba(255,255,255,0.02); }
      .cam-tab[data-idx="0"] { color: var(--c-taupe); }
      .cam-tab[data-idx="1"] { color: var(--c-moss);  }
      .cam-tab[data-idx="2"] { color: var(--c-clay);  }
      .cam-tab[data-idx="3"] { color: var(--c-rust);  }
      .cam-tab.active[data-idx="0"] { background: var(--tint-taupe); color: var(--taupe-soft); border: 1.5px solid var(--c-taupe); }
      .cam-tab.active[data-idx="1"] { background: var(--tint-moss);  color: #c2d4bd; border: 1.5px solid var(--c-moss); }
      .cam-tab.active[data-idx="2"] { background: var(--tint-clay);  color: #e3b9bb; border: 1.5px solid var(--c-clay); }
      .cam-tab.active[data-idx="3"] { background: var(--tint-rust);  color: #e8b89a; border: 1.5px solid var(--c-rust); }

      .filter-row { display: grid; grid-template-columns: minmax(120px, 180px) 1fr; gap: 10px; position: relative; z-index: 10; align-items: start; }
      @media (max-width: 460px) { .filter-row { grid-template-columns: 1fr; } }
      .dropdown { position: relative; }
      .dropdown-btn {
        width: 100%; height: 40px; padding: 0 12px; font-size: 13px; font-weight: 500;
        background: rgba(255,255,255,0.025); border: 1px solid var(--line);
        border-radius: var(--radius-btn); color: var(--fg); cursor: pointer;
        display: flex; align-items: center; gap: 8px; transition: border-color 0.15s, background 0.15s;
      }
      .dropdown-btn:hover { border-color: var(--line-strong); background: rgba(255,255,255,0.045); }
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
      .dropdown-option:hover { background: rgba(154,136,115,0.12); }
      .dropdown-option.active { background: rgba(117,64,67,0.2); color: #e3b9bb; }
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

      .player { position: relative; aspect-ratio: 16/9; background: #0c0a09; border-radius: 14px; overflow: hidden; border: 1px solid var(--line); }
      .player video { width: 100%; height: 100%; object-fit: contain; display: none; background: #0c0a09; }
      .player-placeholder { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; color: var(--fg-muted); }
      .player-placeholder svg { width: 44px; height: 44px; stroke: var(--fg-muted); fill: none; stroke-width: 1.5; opacity: 0.4; }
      .player-placeholder span { font-size: 13px; text-align: center; padding: 0 20px; }

      .play-btn { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; cursor: pointer; }
      .play-btn.hidden { display: none; }
      .play-btn-icon {
        width: 78px; height: 78px; background: rgba(23,22,20,0.5); border: 2px solid var(--taupe);
        border-radius: 50%; display: flex; align-items: center; justify-content: center;
        transition: transform 0.2s, background 0.2s, box-shadow 0.2s;
      }
      .play-btn:hover .play-btn-icon { background: rgba(23,22,20,0.75); transform: scale(1.05); box-shadow: 0 0 0 10px rgba(154,136,115,0.12); }
      .play-btn-icon svg { width: 28px; height: 28px; fill: var(--taupe); transform: translateX(2px); }

      .player-overlay { position: absolute; top: 12px; left: 12px; display: flex; gap: 6px; align-items: center; pointer-events: none; flex-wrap: wrap; }
      .player-overlay-right { position: absolute; top: 14px; right: 12px; pointer-events: none; }
      .player-cam-label {
        position: absolute; bottom: 12px; left: 12px; font-size: 10px; letter-spacing: 1px;
        color: rgba(214,199,179,0.75); text-shadow: 0 1px 0 rgba(0,0,0,0.6);
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
        background: var(--tint-cache); border: 1px solid rgba(74,222,128,0.30); color: var(--c-cache);
      }
      .cache-badge svg { width: 10px; height: 10px; stroke: currentColor; fill: none; stroke-width: 2.4; }
      .clip-time-inner {
        display: inline-flex; align-items: center; height: 28px; padding: 0 10px;
        background: rgba(15,11,9,0.78); color: var(--fg); border-radius: 8px;
        font-size: 12px; font-weight: 600; border: 1px solid var(--line);
        font-family: 'JetBrains Mono', monospace;
      }
      .clip-time-badge { font-size: 10px; color: rgba(214,199,179,0.75); letter-spacing: 1px; text-shadow: 0 1px 0 rgba(0,0,0,0.6); font-family: 'JetBrains Mono', monospace; }

      .fullscreen-btn {
        position: absolute; bottom: 10px; right: 10px; width: 34px; height: 34px;
        background: rgba(15,11,9,0.55); border: 1px solid var(--line); border-radius: 8px;
        cursor: pointer; display: flex; align-items: center; justify-content: center;
        opacity: 0; transition: opacity 0.2s;
      }
      .player:hover .fullscreen-btn { opacity: 1; }
      @media (hover: none) { .fullscreen-btn { opacity: 0.8; } }
      .fullscreen-btn svg { width: 17px; height: 17px; stroke: var(--fg); fill: none; stroke-width: 2; }

      .loading { position: absolute; inset: 0; display: none; align-items: center; justify-content: center; background: rgba(15,11,9,0.5); }
      .loading.active { display: flex; }
      .spinner { width: 36px; height: 36px; border: 2px solid rgba(154,136,115,0.15); border-top-color: var(--taupe); border-radius: 50%; animation: spin 0.9s linear infinite; }
      @keyframes spin { to { transform: rotate(360deg); } }

      .clips-nav { display: grid; grid-template-columns: 40px 1fr 40px; align-items: center; gap: 8px; }
      .nav-btn {
        width: 40px; height: 40px; background: rgba(255,255,255,0.025);
        border: 1px solid var(--line); border-radius: var(--radius-btn);
        color: var(--fg-dim); cursor: pointer; display: flex; align-items: center;
        justify-content: center; transition: all 0.15s;
      }
      .nav-btn:hover:not(:disabled) { background: rgba(255,255,255,0.04); color: var(--taupe); border-color: var(--line-strong); }
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
        background: #0c0a09; border: 1.5px solid var(--line);
        scroll-snap-align: center; transition: border-color 0.15s, transform 0.15s;
      }
      .thumb:hover { transform: translateY(-1px); }
      .thumb.active { border-color: currentColor; box-shadow: 0 0 0 1px currentColor; }
      .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
      .thumb-fallback { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; border: none; }
      .thumb-fallback svg { width: 22px; height: 22px; stroke: currentColor; fill: none; stroke-width: 1.6; opacity: 0.5; }
      .thumb-time {
        position: absolute; bottom: 0; left: 0; right: 0; padding: 2px 4px;
        font-size: 10px; font-weight: 600; color: #ece4d6;
        background: linear-gradient(transparent, rgba(12,10,9,0.9));
        font-family: 'JetBrains Mono', monospace; letter-spacing: 0.2px;
      }
      .thumb-cached { position: absolute; top: 4px; right: 4px; width: 6px; height: 6px; border-radius: 50%; background: var(--c-cache); box-shadow: 0 0 0 2px rgba(12,10,9,0.6); }

      .fs-overlay { display: none; position: fixed; inset: 0; background: #0c0a09; z-index: 999999; flex-direction: column; }
      .fs-overlay.active { display: flex; }
      .fs-video { flex: 1; display: flex; align-items: center; justify-content: center; min-height: 0; }
      .fs-video video { width: 100%; height: 100%; object-fit: contain; }
      .fs-close {
        position: fixed; top: 16px; right: 16px; width: 44px; height: 44px;
        background: rgba(15,11,9,0.75); border: 1px solid var(--line-strong);
        border-radius: 12px; cursor: pointer; display: flex; align-items: center;
        justify-content: center; z-index: 1000000;
      }
      .fs-close svg { width: 24px; height: 24px; stroke: var(--fg); fill: none; stroke-width: 2; stroke-linecap: round; }
      .fs-info { position: fixed; top: 20px; left: 20px; z-index: 1000000; }
      .fs-title { font-size: 18px; font-weight: 700; color: var(--fg); margin-bottom: 4px; }
      .fs-subtitle { font-size: 13px; color: var(--fg-muted); }
      .fs-nav { position: fixed; bottom: 30px; left: 50%; transform: translateX(-50%); display: flex; gap: 12px; z-index: 1000000; }
      .fs-nav-btn {
        width: 52px; height: 52px; background: rgba(15,11,9,0.75);
        border: 1px solid var(--line-strong); border-radius: 14px; cursor: pointer;
        display: flex; align-items: center; justify-content: center;
      }
      .fs-nav-btn:disabled { opacity: 0.45; cursor: not-allowed; }
      .fs-nav-btn svg { width: 26px; height: 26px; stroke: var(--fg); fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
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

customElements.define('reolink-clips-card', ReolinkClipsCard);
customElements.define('reolink-clips-card-editor', ReolinkClipsCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
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

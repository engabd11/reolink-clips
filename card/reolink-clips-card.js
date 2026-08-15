/**
 * Reolink NVR Clips Card — Earthy Dark Edition v5.0
 * With Reolink Clip Cache support — instant playback from local cache
 *
 * type: custom:reolink-clips-card
 * cameras:
 *   - name: BACK DOOR
 *     sensors:
 *       person: binary_sensor.back_door_person
 *       vehicle: binary_sensor.back_door_vehicle
 *       animal: binary_sensor.back_door_animal
 *   - name: Carport
 *     sensors:
 *       person: binary_sensor.carport_person
 *       vehicle: binary_sensor.carport_vehicle
 *       animal: binary_sensor.carport_animal
 *   - name: Doorbell
 *     sensors:
 *       person: binary_sensor.doorbell_person
 *       visitor: binary_sensor.doorbell_visitor
 *       package: binary_sensor.doorbell_package
 *       vehicle: binary_sensor.doorbell_vehicle
 * resolution: low
 * cache_enabled: true   ← NEW: enables cache-first loading
 */

class ReolinkClipsCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._hass = null;
    this._config = null;
    this._selectedCamera = 0;
    this._selectedDate = null;
    this._selectedEventType = 'all';
    this._availableDays = [];
    this._availableEventTypes = [];
    this._allClips = [];
    this._clipsList = [];
    this._currentClipIndex = 0;
    // ── Cache support ──
    this._cacheEnabled = false;        // set by cache_enabled config
    this._cachedClips = new Map();      // media_content_id → { url, filename, ... }
    this._cacheLoading = false;         // tracks if cache browse is in progress
    this._docClickHandler = () => this._closeAllDropdowns();
    this._docKeyHandler = (e) => this._handleKey(e);
    this._listenersAttached = false;
  }

  static getStubConfig() {
    return { cameras: [{ name: 'BACK DOOR' }, { name: 'Carport' }], resolution: 'low', cache_enabled: true };
  }

  setConfig(config) {
    if (!config.cameras || config.cameras.length === 0) throw new Error('Define at least one camera');
    this._config = { resolution: 'low', cache_enabled: false, ...config };
    this._cacheEnabled = !!this._config.cache_enabled;
    this.render();
  }

  set hass(hass) {
    const wasNull = !this._hass;
    this._hass = hass;
    if (wasNull) { this.initCamera(); this.updateLastDetection(); }
    else { this.updateLastDetection(); }
  }

  disconnectedCallback() {
    if (this._listenersAttached) {
      document.removeEventListener('click', this._docClickHandler);
      document.removeEventListener('keydown', this._docKeyHandler);
      this._listenersAttached = false;
    }
  }

  _closeAllDropdowns() {
    this.shadowRoot.getElementById('date-dropdown')?.classList.remove('open');
    this.shadowRoot.getElementById('event-dropdown')?.classList.remove('open');
  }

  _handleKey(e) {
    if (e.key === 'Escape') this.closeFullscreen();
    if (e.key === 'ArrowLeft') this.prevClip();
    if (e.key === 'ArrowRight') this.nextClip();
  }

  // ── Cache WebSocket API ───────────────────────────────────────────────

  async _fetchCachedClips(camera, date, eventType) {
    /** Query the reolink_clip_cache integration for locally cached clips. */
    if (!this._hass || !this._cacheEnabled) return;
    try {
      const result = await this._hass.callWS({
        type: 'reolink_clip_cache/browse',
        camera: camera,
        date: date,
        event_type: eventType === 'all' ? undefined : eventType,
      });
      if (result && result.clips) {
        for (const clip of result.clips) {
          if (clip.media_content_id) {
            this._cachedClips.set(clip.media_content_id, clip);
          }
          // Also index by filename for quick lookup
          if (clip.filename) {
            this._cachedClips.set(clip.filename, clip);
          }
        }
      }
    } catch (e) {
      // Integration not installed or not running — silent fallback to NVR
      console.debug('[reolink-clips] Cache not available, using NVR directly');
      this._cacheEnabled = false; // Stop trying
    }
  }

  async _resolveFromCache(mediaContentId) {
    /** Try to resolve a clip from local cache. Returns {url, cached: true} or null. */
    if (!this._hass || !this._cacheEnabled) return null;
    try {
      const result = await this._hass.callWS({
        type: 'reolink_clip_cache/resolve',
        filename: mediaContentId,
      });
      if (result && result.cached && result.url) {
        return result;
      }
    } catch (e) {
      // Silent fail — fallback to NVR
    }
    return null;
  }

  // ── Detection times ──────────────────────────────────────────────────

  getLastDetectionTimes() {
    if (!this._hass) return [];
    const cam = this._config.cameras[this._selectedCamera];
    if (!cam.sensors) return [];
    const sensorTypes = [
      { key: 'person',  label: 'Person',  color: 'blue'   },
      { key: 'visitor', label: 'Visitor', color: 'blue'   },
      { key: 'vehicle', label: 'Vehicle', color: 'amber'  },
      { key: 'animal',  label: 'Animal',  color: 'green'  },
      { key: 'package', label: 'Package', color: 'purple' }
    ];
    return sensorTypes.filter(t => cam.sensors[t.key]).map(type => {
      const entityId = cam.sensors[type.key];
      const state = this._hass.states[entityId];
      if (!state) return null;
      const isActive = state.state === 'on';
      const lastUpdated = new Date(state.last_updated);
      const diffMs = Date.now() - lastUpdated;
      const diffSecs = Math.floor(diffMs / 1000);
      const diffMins = Math.floor(diffMs / 60000);
      const diffHours = Math.floor(diffMs / 3600000);
      const diffDays = Math.floor(diffMs / 86400000);
      let timeAgo;
      if (isActive)           timeAgo = 'now';
      else if (diffSecs < 60) timeAgo = `${diffSecs}s`;
      else if (diffMins < 60) timeAgo = `${diffMins}m`;
      else if (diffHours < 24)timeAgo = `${diffHours}h`;
      else if (diffDays < 7)  timeAgo = `${diffDays}d`;
      else timeAgo = lastUpdated.toLocaleDateString('en-AU', { day: 'numeric', month: 'short' });
      return { type: type.label, color: type.color, timeAgo, isActive };
    }).filter(Boolean);
  }

  updateLastDetection() {
    const container = this.shadowRoot.getElementById('last-detection');
    if (!container) return;
    const detections = this.getLastDetectionTimes();
    if (detections.length === 0) { container.style.display = 'none'; return; }
    container.style.display = 'grid';
    container.innerHTML = detections.map(d => `
      <span class="det-chip ${d.color} ${d.isActive ? 'active' : ''}">
        <span class="det-dot ${d.isActive ? 'pulse' : ''}"></span>
        <span class="det-label">${d.type}</span>
        <span class="det-time">${d.timeAgo}</span>
      </span>`).join('');
  }

  // ── Render ────────────────────────────────────────────────────────────

  render() {
    const cameras = this._config.cameras;
    const cols = Math.min(cameras.length, 4);

    this.shadowRoot.innerHTML = `
      <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        :host {
          --onyx:           #171614;
          --coffee-800:     #3A2618;
          --coffee-700:     #4a3225;
          --bitter-choc:    #754043;
          --taupe:          #9A8873;
          --taupe-soft:     #d6c7b3;
          --fg:             #ece4d6;
          --fg-dim:         #bdb09c;
          --fg-muted:       #8a7e6d;
          --line:           rgba(154,136,115,0.18);
          --line-strong:    rgba(154,136,115,0.30);
          --alert-fg:       #f3e2d8;
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
          padding: 14px;
          color: var(--fg);
          display: flex;
          flex-direction: column;
          gap: 12px;
        }

        .header { display: flex; flex-direction: column; gap: 8px; }
        .header-top { display: flex; align-items: center; gap: 12px; }

        .hue-icon {
          width: 44px; height: 44px;
          border-radius: 10px;
          background: var(--taupe);
          display: flex; align-items: center; justify-content: center;
          flex-shrink: 0;
          box-shadow: 0 2px 0 rgba(0,0,0,0.25), inset 0 1px 0 rgba(255,255,255,0.08);
        }
        .hue-icon svg { width: 22px; height: 22px; stroke: #1a130d; fill: none; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; }

        .header-left { flex: 1; min-width: 0; }
        .title    { font-size: 18px; font-weight: 700; letter-spacing: 0.1px; color: var(--fg); }
        .subtitle { font-size: 12px; color: var(--fg-muted); margin-top: 2px; font-weight: 500; }

        #last-detection {
          display: none;
          grid-template-columns: repeat(auto-fit, minmax(0, 1fr));
          gap: 5px;
          max-width: 480px;
        }

        .det-chip {
          display: flex; align-items: center; justify-content: center; gap: 4px;
          padding: 4px 8px;
          border-radius: 999px;
          font-size: 11px; font-weight: 500;
          overflow: hidden;
          cursor: default;
          transition: filter 0.15s;
          min-width: 0;
        }
        .det-chip:hover { filter: brightness(1.08); }
        .det-chip.blue   { background: var(--tint-sand); border: 1px solid rgba(201,181,142,0.35); color: var(--c-sand); }
        .det-chip.blue   .det-dot { background: var(--c-sand); }
        .det-chip.amber  { background: var(--tint-rust); border: 1px solid rgba(184,107,74,0.40);  color: #d4906f; }
        .det-chip.amber  .det-dot { background: var(--c-rust); }
        .det-chip.green  { background: var(--tint-moss); border: 1px solid rgba(122,141,118,0.38); color: #a3b89e; }
        .det-chip.green  .det-dot { background: var(--c-moss); }
        .det-chip.purple { background: var(--tint-clay); border: 1px solid rgba(138,77,80,0.38);   color: #e3b9bb; }
        .det-chip.purple .det-dot { background: var(--c-clay); }

        .det-dot { width: 6px; height: 6px; border-radius: 50%; box-shadow: 0 0 0 2px rgba(0,0,0,0.35); flex-shrink: 0; }
        .det-dot.pulse { animation: chip-pulse 1.6s ease-in-out infinite; }
        @keyframes chip-pulse {
          0%,100% { opacity: 0.35; transform: scale(0.85); }
          50%      { opacity: 1;   transform: scale(1.1); }
        }
        .det-label { overflow: hidden; text-overflow: ellipsis; flex-shrink: 1; min-width: 0; white-space: nowrap; }
        .det-time { opacity: 0.65; font-weight: 500; flex-shrink: 1; white-space: nowrap; min-width: 0; overflow: hidden; text-overflow: ellipsis; }

        .refresh-btn {
          height: 44px;
          padding: 0 14px;
          background: rgba(255,255,255,0.025);
          border: 1px solid var(--line);
          border-radius: var(--radius-btn);
          color: var(--fg-dim);
          cursor: pointer;
          display: flex; align-items: center; justify-content: center; gap: 6px;
          transition: background 0.15s, color 0.15s;
          flex-shrink: 0;
          white-space: nowrap;
          font-size: 12px; font-weight: 600; letter-spacing: 0.3px;
        }
        .refresh-btn:hover { background: rgba(255,255,255,0.04); color: var(--fg); }
        .refresh-btn svg { width: 15px; height: 15px; stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; flex-shrink: 0; }

        .camera-tabs {
          display: grid;
          grid-template-columns: repeat(${cols}, 1fr);
          gap: 8px;
        }

        .cam-tab {
          height: 40px;
          font-size: 12px; font-weight: 600;
          letter-spacing: 0.6px;
          text-transform: uppercase;
          background: transparent;
          border: 1px solid var(--line);
          border-radius: var(--radius-btn);
          color: var(--fg-muted);
          cursor: pointer;
          transition: all 0.15s;
        }
        .cam-tab:hover { filter: brightness(1.15); background: rgba(255,255,255,0.02); }
        .cam-tab[data-idx="0"] { color: var(--c-taupe); border-color: rgba(154,136,115,0.30); }
        .cam-tab[data-idx="1"] { color: var(--c-moss);  border-color: rgba(122,141,118,0.30); }
        .cam-tab[data-idx="2"] { color: var(--c-clay);  border-color: rgba(138,77,80,0.40); }
        .cam-tab[data-idx="3"] { color: var(--c-rust);  border-color: rgba(184,107,74,0.35); }
        .cam-tab.active[data-idx="0"] { background: var(--tint-taupe); color: var(--taupe-soft); border: 1.5px solid var(--c-taupe); }
        .cam-tab.active[data-idx="1"] { background: var(--tint-moss);  color: #c2d4bd;           border: 1.5px solid var(--c-moss);  }
        .cam-tab.active[data-idx="2"] { background: var(--tint-clay);  color: #e3b9bb;           border: 1.5px solid var(--c-clay);  }
        .cam-tab.active[data-idx="3"] { background: var(--tint-rust);  color: #e8b89a;           border: 1.5px solid var(--c-rust);  }

        .filter-row {
          display: grid; grid-template-columns: 1fr 1fr;
          gap: 10px; position: relative; z-index: 10;
        }
        .dropdown { position: relative; }

        .dropdown-btn {
          width: 100%; height: 40px; padding: 0 12px;
          font-size: 13px; font-weight: 500;
          background: rgba(255,255,255,0.025);
          border: 1px solid var(--line);
          border-radius: var(--radius-btn);
          color: var(--fg); cursor: pointer;
          display: flex; align-items: center; gap: 8px;
          transition: border-color 0.15s, background 0.15s;
        }
        .dropdown-btn:hover { border-color: var(--line-strong); background: rgba(255,255,255,0.045); }
        .dropdown-btn .label { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; overflow: hidden; }
        .dropdown-btn .label span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .dropdown-btn .lead { color: var(--taupe); display: inline-flex; }
        .dropdown-btn .lead svg { width: 14px; height: 14px; stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
        .dropdown-btn .chev { margin-left: auto; color: var(--fg-muted); flex-shrink: 0; }
        .dropdown-btn .chev svg { width: 14px; height: 14px; stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; transition: transform 0.2s; }
        .dropdown.open .chev svg { transform: rotate(180deg); }

        .pill-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--taupe); box-shadow: 0 0 0 2px rgba(154,136,115,0.18); flex-shrink: 0; }

        .dropdown-menu {
          display: none;
          position: absolute; top: calc(100% + 4px); left: 0; right: 0;
          background: var(--coffee-800);
          border: 1px solid var(--line-strong);
          border-radius: var(--radius-btn); padding: 6px;
          max-height: 240px; overflow-y: auto;
          z-index: 100;
          box-shadow: 0 10px 30px rgba(0,0,0,0.55);
        }
        .dropdown.open .dropdown-menu { display: block; }

        .dropdown-option {
          padding: 9px 11px;
          font-size: 12px; font-weight: 500;
          color: var(--fg); cursor: pointer;
          border-radius: var(--radius-sm);
          transition: background 0.15s;
          display: flex; align-items: center; gap: 8px;
        }
        .dropdown-option:hover { background: rgba(154,136,115,0.12); }
        .dropdown-option.active { background: rgba(117,64,67,0.2); color: #e3b9bb; }

        .evt-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; box-shadow: 0 0 0 2px rgba(154,136,115,0.18); }
        .evt-dot.all      { background: var(--taupe);  }
        .evt-dot.motion   { background: var(--c-clay); }
        .evt-dot.person   { background: var(--c-sand); }
        .evt-dot.vehicle  { background: var(--c-rust); }
        .evt-dot.animal   { background: var(--c-moss); }
        .evt-dot.doorbell { background: var(--c-sand); }
        .evt-dot.visitor  { background: var(--c-sand); }
        .evt-dot.package  { background: var(--c-clay); }

        .player {
          position: relative; aspect-ratio: 16/9;
          background: #0c0a09;
          border-radius: 14px; overflow: hidden;
          border: 1px solid var(--line);
        }
        .player video { width: 100%; height: 100%; object-fit: contain; display: none; }

        .player-placeholder {
          position: absolute; inset: 0;
          display: flex; flex-direction: column;
          align-items: center; justify-content: center;
          gap: 12px; color: var(--fg-muted);
        }
        .player-placeholder svg { width: 44px; height: 44px; stroke: var(--fg-muted); fill: none; stroke-width: 1.5; opacity: 0.4; }
        .player-placeholder span { font-size: 13px; text-align: center; padding: 0 20px; }

        .play-btn {
          position: absolute; inset: 0;
          display: flex; align-items: center; justify-content: center;
          background: transparent; cursor: pointer;
        }
        .play-btn.hidden { display: none; }

        .play-btn-icon {
          width: 78px; height: 78px;
          background: rgba(23,22,20,0.5);
          border: 2px solid var(--taupe);
          border-radius: 50%;
          display: flex; align-items: center; justify-content: center;
          transition: transform 0.2s, background 0.2s, box-shadow 0.2s;
          box-shadow: 0 0 0 0 rgba(154,136,115,0.35);
        }
        .play-btn:hover .play-btn-icon {
          background: rgba(23,22,20,0.75);
          transform: scale(1.05);
          box-shadow: 0 0 0 10px rgba(154,136,115,0.12);
        }
        .play-btn-icon svg { width: 28px; height: 28px; fill: var(--taupe); transform: translateX(2px); }

        .player-overlay { position: absolute; top: 12px; left: 12px; display: flex; gap: 6px; align-items: center; pointer-events: none; }
        .player-overlay-right { position: absolute; top: 10px; right: 12px; pointer-events: none; }
        .player-cam-label {
          position: absolute; bottom: 10px; right: 12px;
          font-size: 10px; letter-spacing: 1px;
          color: rgba(214,199,179,0.75);
          text-shadow: 0 1px 0 rgba(0,0,0,0.6);
          text-transform: uppercase;
          pointer-events: none;
          font-family: 'JetBrains Mono', 'Courier New', monospace;
        }

        .clip-badge {
          display: inline-flex; align-items: center; gap: 6px;
          height: 28px; padding: 0 10px;
          border-radius: 8px;
          font-size: 11px; font-weight: 700;
          letter-spacing: 1px; text-transform: uppercase;
          background: rgba(15,11,9,0.78); color: var(--fg-dim);
          box-shadow: 0 2px 8px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.08);
        }
        .clip-badge::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--fg-dim); opacity: 0.6; flex-shrink: 0; }

        .clip-badge.motion   { background: var(--bitter-choc); color: var(--alert-fg); }
        .clip-badge.motion::before   { background: var(--alert-fg); opacity: 0.9; animation: badge-pulse 1.6s ease-in-out infinite; }
        .clip-badge.person   { background: rgba(201,181,142,0.25); border: 1px solid rgba(201,181,142,0.35); color: var(--c-sand); }
        .clip-badge.person::before   { background: var(--c-sand); opacity: 1; }
        .clip-badge.vehicle  { background: rgba(184,107,74,0.25);  border: 1px solid rgba(184,107,74,0.40);  color: #d4906f; }
        .clip-badge.vehicle::before  { background: var(--c-rust); opacity: 1; }
        .clip-badge.animal   { background: rgba(122,141,118,0.25); border: 1px solid rgba(122,141,118,0.38); color: #a3b89e; }
        .clip-badge.animal::before   { background: var(--c-moss); opacity: 1; }
        .clip-badge.doorbell { background: rgba(201,181,142,0.25); border: 1px solid rgba(201,181,142,0.35); color: var(--c-sand); }
        .clip-badge.doorbell::before { background: var(--c-sand); opacity: 1; }
        .clip-badge.visitor  { background: rgba(201,181,142,0.25); border: 1px solid rgba(201,181,142,0.35); color: var(--c-sand); }
        .clip-badge.visitor::before  { background: var(--c-sand); opacity: 1; }
        .clip-badge.package  { background: rgba(138,77,80,0.25);   border: 1px solid rgba(138,77,80,0.38);   color: #e3b9bb; }
        .clip-badge.package::before  { background: var(--c-clay); opacity: 1; }

        /* ── Cache badge ── */
        .cache-badge {
          display: inline-flex; align-items: center; gap: 4px;
          height: 20px; padding: 0 7px;
          border-radius: 6px;
          font-size: 9px; font-weight: 700;
          letter-spacing: 0.8px; text-transform: uppercase;
          background: var(--tint-cache);
          border: 1px solid rgba(74,222,128,0.30);
          color: var(--c-cache);
          font-family: 'Inter', -apple-system, sans-serif;
        }
        .cache-badge svg { width: 10px; height: 10px; fill: currentColor; }

        @keyframes badge-pulse { 0%,100%{opacity:0.35;transform:scale(0.85)} 50%{opacity:1;transform:scale(1.1)} }

        .clip-time-inner {
          display: inline-flex; align-items: center;
          height: 28px; padding: 0 10px;
          background: rgba(15,11,9,0.78);
          color: var(--fg);
          border-radius: 8px;
          font-size: 12px; font-weight: 600;
          border: 1px solid var(--line);
          font-family: 'JetBrains Mono', 'Courier New', monospace;
        }

        .clip-time-badge {
          font-size: 10px;
          color: rgba(214,199,179,0.75);
          letter-spacing: 1px;
          text-shadow: 0 1px 0 rgba(0,0,0,0.6);
          font-family: 'JetBrains Mono', 'Courier New', monospace;
        }

        .fullscreen-btn {
          position: absolute; bottom: 10px; right: 10px;
          width: 34px; height: 34px;
          background: rgba(15,11,9,0.55);
          border: 1px solid var(--line); border-radius: 8px;
          cursor: pointer;
          display: flex; align-items: center; justify-content: center;
          opacity: 0; transition: opacity 0.2s;
        }
        .player:hover .fullscreen-btn { opacity: 1; }
        @media (hover: none) { .fullscreen-btn { opacity: 0.8; } }
        .fullscreen-btn:hover { border-color: var(--line-strong); }
        .fullscreen-btn svg { width: 17px; height: 17px; stroke: var(--fg); fill: none; stroke-width: 2; }

        .loading { position: absolute; inset: 0; display: none; align-items: center; justify-content: center; background: rgba(15,11,9,0.5); }
        .loading.active { display: flex; }
        .spinner { width: 36px; height: 36px; border: 2px solid rgba(154,136,115,0.15); border-top-color: var(--taupe); border-radius: 50%; animation: spin 0.9s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }

        .clips-nav {
          display: grid;
          grid-template-columns: 40px 1fr 40px;
          align-items: center; gap: 8px;
          padding: 4px 2px 2px;
        }

        .nav-btn {
          width: 40px; height: 40px;
          background: rgba(255,255,255,0.025);
          border: 1px solid var(--line);
          border-radius: var(--radius-btn);
          color: var(--fg-dim); cursor: pointer;
          display: flex; align-items: center; justify-content: center;
          transition: all 0.15s;
        }
        .nav-btn:hover:not(:disabled) { background: rgba(255,255,255,0.04); color: var(--taupe); border-color: var(--line-strong); }
        .nav-btn:disabled { opacity: 0.45; cursor: not-allowed; }
        .nav-btn svg { width: 16px; height: 16px; stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }

        .clip-info { text-align: center; }
        .clip-name { font-size: 14px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--fg); }
        .clip-name .sep { color: var(--fg-muted); margin: 0 6px; font-weight: 400; }
        .clip-index { font-size: 12px; color: var(--fg-muted); margin-top: 4px; }

        .fs-overlay { display: none; position: fixed; inset: 0; background: #0c0a09; z-index: 999999; flex-direction: column; }
        .fs-overlay.active { display: flex; }
        .fs-video { flex: 1; display: flex; align-items: center; justify-content: center; }
        .fs-video video { width: 100%; height: 100%; object-fit: contain; }
        .fs-close {
          position: fixed; top: 16px; right: 16px;
          width: 44px; height: 44px;
          background: rgba(15,11,9,0.75); border: 1px solid var(--line-strong);
          border-radius: 12px; cursor: pointer;
          display: flex; align-items: center; justify-content: center;
          z-index: 1000000; transition: border-color 0.15s;
        }
        .fs-close:hover { border-color: var(--taupe); }
        .fs-close svg { width: 24px; height: 24px; stroke: var(--fg); fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
        .fs-info { position: fixed; top: 20px; left: 20px; z-index: 1000000; }
        .fs-title { font-size: 18px; font-weight: 700; color: var(--fg); margin-bottom: 4px; }
        .fs-subtitle { font-size: 13px; color: var(--fg-muted); }
        .fs-nav { position: fixed; bottom: 30px; left: 50%; transform: translateX(-50%); display: flex; gap: 12px; z-index: 1000000; }
        .fs-nav-btn {
          width: 52px; height: 52px;
          background: rgba(15,11,9,0.75); border: 1px solid var(--line-strong);
          border-radius: 14px; cursor: pointer;
          display: flex; align-items: center; justify-content: center;
          transition: border-color 0.15s;
        }
        .fs-nav-btn:hover:not(:disabled) { border-color: var(--taupe); }
        .fs-nav-btn:disabled { opacity: 0.45; cursor: not-allowed; }
        .fs-nav-btn svg { width: 26px; height: 26px; stroke: var(--fg); fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }

        .dropdown-menu::-webkit-scrollbar { width: 4px; }
        .dropdown-menu::-webkit-scrollbar-track { background: transparent; }
        .dropdown-menu::-webkit-scrollbar-thumb { background: var(--fg-muted); border-radius: 2px; }
      </style>

      <div class="card">
        <div class="header">
          <div class="header-top">
            <div class="hue-icon">
              <svg viewBox="0 0 24 24">
                <rect x="2" y="6" width="13" height="12" rx="2"/>
                <path d="M22 8l-5 4 5 4V8z"/>
              </svg>
            </div>
            <div class="header-left">
              <div class="title">Events</div>
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
            ${cameras.map((cam, i) => `
              <button class="cam-tab${i === 0 ? ' active' : ''}" data-idx="${i}">${cam.name}</button>
            `).join('')}
          </div>
        ` : ''}

        <div class="filter-row">
          <div class="dropdown" id="date-dropdown">
            <button class="dropdown-btn" id="date-btn">
              <span class="label">
                <span class="lead">
                  <svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>
                </span>
                <span id="date-label">Today</span>
              </span>
              <span class="chev"><svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg></span>
            </button>
            <div class="dropdown-menu" id="date-menu"></div>
          </div>

          <div class="dropdown" id="event-dropdown">
            <button class="dropdown-btn" id="event-btn">
              <span class="label">
                <span class="pill-dot" id="event-dot"></span>
                <span id="event-label">All Events</span>
              </span>
              <span class="chev"><svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg></span>
            </button>
            <div class="dropdown-menu" id="event-menu"></div>
          </div>
        </div>

        <div class="player" id="player">
          <video id="video" playsinline></video>
          <div class="player-placeholder" id="placeholder">
            <svg viewBox="0 0 24 24"><rect x="2" y="6" width="13" height="12" rx="2"/><path d="M22 8l-5 4 5 4V8z"/></svg>
            <span>Loading clips…</span>
          </div>
          <div class="play-btn hidden" id="play-btn">
            <div class="play-btn-icon">
              <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
            </div>
          </div>
          <div class="player-overlay">
            <span class="clip-badge" id="event-badge"></span>
            <span class="clip-time-inner" id="clip-time-inner"></span>
            <span class="cache-badge" id="cache-badge" style="display:none;">
              <svg viewBox="0 0 24 24"><path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="10"/></svg>
              CACHED
            </span>
          </div>
          <div class="player-overlay-right">
            <span class="clip-time-badge" id="clip-time"></span>
          </div>
          <div class="player-cam-label" id="player-cam-label"></div>
          <button class="fullscreen-btn" id="fullscreen-btn" title="Fullscreen">
            <svg viewBox="0 0 24 24" stroke="currentColor" fill="none" stroke-width="2"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>
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
  }

  _attachEvents() {
    if (this._listenersAttached) {
      document.removeEventListener('click', this._docClickHandler);
      document.removeEventListener('keydown', this._docKeyHandler);
    }
    document.addEventListener('click', this._docClickHandler);
    document.addEventListener('keydown', this._docKeyHandler);
    this._listenersAttached = true;

    this.shadowRoot.querySelectorAll('.cam-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        this._selectedCamera = parseInt(tab.dataset.idx);
        this.shadowRoot.querySelectorAll('.cam-tab').forEach((t, i) => t.classList.toggle('active', i === this._selectedCamera));
        this._selectedDate = null; this._selectedEventType = 'all';
        this._allClips = []; this._clipsList = [];
        this.updateLastDetection(); this.initCamera();
      });
    });

    this.shadowRoot.getElementById('refresh-btn').addEventListener('click', () => this.loadClips());

    const video = this.shadowRoot.getElementById('video');
    this.shadowRoot.getElementById('play-btn').addEventListener('click', () => this._doPlay());
    video.addEventListener('ended', () => this._onVideoEnded());
    video.addEventListener('click',  () => this._togglePlay());

    this.shadowRoot.getElementById('prev-btn').addEventListener('click', () => this.prevClip());
    this.shadowRoot.getElementById('next-btn').addEventListener('click', () => this.nextClip());

    this.shadowRoot.getElementById('fullscreen-btn').addEventListener('click', () => this.openFullscreen());
    this.shadowRoot.getElementById('fs-close').addEventListener('click', () => this.closeFullscreen());
    this.shadowRoot.getElementById('fs-prev').addEventListener('click', () => { this.prevClip(); this._syncFsVideo(); });
    this.shadowRoot.getElementById('fs-next').addEventListener('click', () => { this.nextClip(); this._syncFsVideo(); });

    this.shadowRoot.getElementById('date-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      this.shadowRoot.getElementById('event-dropdown').classList.remove('open');
      this.shadowRoot.getElementById('date-dropdown').classList.toggle('open');
    });
    this.shadowRoot.getElementById('event-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      this.shadowRoot.getElementById('date-dropdown').classList.remove('open');
      this.shadowRoot.getElementById('event-dropdown').classList.toggle('open');
    });
    this.shadowRoot.getElementById('date-menu').addEventListener('click', e => e.stopPropagation());
    this.shadowRoot.getElementById('event-menu').addEventListener('click', e => e.stopPropagation());
  }

  // ── Event dropdown ────────────────────────────────────────────────────

  updateEventDropdown() {
    const eventMenu  = this.shadowRoot.getElementById('event-menu');
    const eventLabel = this.shadowRoot.getElementById('event-label');
    const eventDot   = this.shadowRoot.getElementById('event-dot');
    const eventTypes = [
      { id: 'all', label: 'All Events' },
      ...this._availableEventTypes.map(et => ({ id: et.toLowerCase(), label: et }))
    ];
    eventMenu.innerHTML = eventTypes.map(et => `
      <div class="dropdown-option ${this._selectedEventType === et.id ? 'active' : ''}" data-type="${et.id}">
        <span class="evt-dot ${et.id}"></span>${et.label}
      </div>`).join('');
    eventMenu.querySelectorAll('.dropdown-option').forEach(opt => {
      opt.addEventListener('click', () => {
        this._selectedEventType = opt.dataset.type;
        this.shadowRoot.getElementById('event-dropdown').classList.remove('open');
        this.updateEventDropdown(); this.loadClips();
      });
    });
    const sel = eventTypes.find(et => et.id === this._selectedEventType) || eventTypes[0];
    eventLabel.textContent = sel.label;
    eventDot.className = `pill-dot evt-dot ${sel.id}`;
  }

  // ── Camera init ───────────────────────────────────────────────────────

  async initCamera() {
    if (!this._hass) return;
    const cam = this._config.cameras[this._selectedCamera];
    const loading = this.shadowRoot.getElementById('loading');
    const ph = this.shadowRoot.getElementById('placeholder');
    const count = this.shadowRoot.getElementById('clip-count');
    loading.classList.add('active'); ph.style.display = 'none';
    try {
      const root = await this._hass.callWS({ type: 'media_source/browse_media', media_content_id: 'media-source://reolink' });
      if (!root.children?.length) throw new Error('No Reolink devices found');
      const camName = cam.name.toLowerCase();
      const camFolder = root.children.find(c => c.title.toLowerCase() === camName || c.title.toLowerCase().includes(camName));
      if (!camFolder) throw new Error(`Camera "${cam.name}" not found`);
      const camResult = await this._hass.callWS({ type: 'media_source/browse_media', media_content_id: camFolder.media_content_id });
      const resFolder = camResult.children?.find(c => c.title.toLowerCase().includes(this._config.resolution));
      if (!resFolder) throw new Error('Resolution folder not found');
      const resResult = await this._hass.callWS({ type: 'media_source/browse_media', media_content_id: resFolder.media_content_id });
      this._availableDays = resResult.children || [];
      this.updateDatePicker();
      if (!this._selectedDate && this._availableDays.length > 0) {
        const today = new Date();
        const todayStr = `${today.getFullYear()}/${today.getMonth() + 1}/${today.getDate()}`;
        this._selectedDate = this._availableDays.find(d => d.title.includes(todayStr)) || this._availableDays[this._availableDays.length - 1];
      }
      await this.loadClips();
    } catch (err) {
      console.error('[reolink-clips] Init error:', err);
      loading.classList.remove('active'); ph.style.display = 'flex';
      ph.querySelector('span').textContent = err.message; count.textContent = 'Error';
    }
  }

  updateDatePicker() {
    const dropdown = this.shadowRoot.getElementById('date-menu');
    const dateLabel = this.shadowRoot.getElementById('date-label');
    const today = new Date();
    const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1);
    const fmt = d => `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
    const todayStr = fmt(today); const yesterdayStr = fmt(yesterday);
    const days = [...this._availableDays].reverse();
    dropdown.innerHTML = days.map(day => {
      let label = day.title;
      if (day.title.includes(todayStr)) label = 'Today';
      else if (day.title.includes(yesterdayStr)) label = 'Yesterday';
      const isActive = this._selectedDate?.media_content_id === day.media_content_id;
      return `<div class="dropdown-option ${isActive ? 'active' : ''}" data-id="${day.media_content_id}">${label}</div>`;
    }).join('');
    if (this._selectedDate) {
      if (this._selectedDate.title.includes(todayStr)) dateLabel.textContent = 'Today';
      else if (this._selectedDate.title.includes(yesterdayStr)) dateLabel.textContent = 'Yesterday';
      else {
        const m = this._selectedDate.title.match(/(\d{4})\/(\d{1,2})\/(\d{1,2})/);
        dateLabel.textContent = m ? `${m[2]}/${m[3]}` : this._selectedDate.title;
      }
    }
    dropdown.querySelectorAll('.dropdown-option').forEach(opt => {
      opt.addEventListener('click', () => {
        const id = opt.dataset.id;
        this._selectedDate = this._availableDays.find(d => d.media_content_id === id);
        this.shadowRoot.getElementById('date-dropdown').classList.remove('open');
        this.updateDatePicker(); this.loadClips();
      });
    });
  }

  // ── Load clips (with cache integration) ──────────────────────────────

  async loadClips() {
    if (!this._hass || !this._selectedDate) return;
    const loading = this.shadowRoot.getElementById('loading');
    const ph = this.shadowRoot.getElementById('placeholder');
    const count = this.shadowRoot.getElementById('clip-count');
    loading.classList.add('active'); ph.style.display = 'none';
    this._allClips = []; this._clipsList = []; this._availableEventTypes = []; this._currentClipIndex = 0;

    try {
      const dayResult = await this._hass.callWS({ type: 'media_source/browse_media', media_content_id: this._selectedDate.media_content_id });
      const knownFolders = ['Motion','Person','Vehicle','Animal','Doorbell','Visitor','Package'];
      const eventFolders = (dayResult.children || []).filter(c => !c.can_play && knownFolders.some(n => n.toLowerCase() === c.title.toLowerCase()));
      this._availableEventTypes = eventFolders.map(f => f.title);
      const folderResults = await Promise.all(
        eventFolders.map(folder =>
          this._hass.callWS({ type: 'media_source/browse_media', media_content_id: folder.media_content_id })
            .then(res => ({ folder, res })).catch(err => { console.warn(`[reolink-clips] Failed to load ${folder.title}:`, err); return null; })
        )
      );
      for (const result of folderResults) {
        if (!result) continue;
        const { folder, res } = result;
        const clips = (res.children || []).filter(c => c.can_play || c.media_content_type?.includes('video')).map(clip => ({ ...clip, eventType: folder.title }));
        this._allClips = this._allClips.concat(clips);
      }
      this._allClips.sort((a, b) => b.title.localeCompare(a.title));

      // ── Fetch cache for this camera/date ──
      if (this._cacheEnabled) {
        const cam = this._config.cameras[this._selectedCamera];
        await this._fetchCachedClips(cam.name, this._selectedDate.title);
      }

      this.updateEventDropdown(); this.filterAndDisplayClips(); loading.classList.remove('active');
    } catch (err) {
      console.error('[reolink-clips] Load error:', err);
      loading.classList.remove('active'); ph.style.display = 'flex';
      ph.querySelector('span').textContent = err.message; count.textContent = '0 events'; this.updateNavigation();
    }
  }

  filterAndDisplayClips() {
    const ph = this.shadowRoot.getElementById('placeholder');
    const count = this.shadowRoot.getElementById('clip-count');
    const video = this.shadowRoot.getElementById('video');
    this._clipsList = this._selectedEventType === 'all' ? [...this._allClips] : this._allClips.filter(c => c.eventType.toLowerCase() === this._selectedEventType);
    this._currentClipIndex = 0;
    const plurals = { 'all': 'events', 'person': 'people', 'vehicle': 'vehicles', 'animal': 'animals', 'doorbell': 'doorbell events', 'visitor': 'visitors', 'package': 'packages', 'motion': 'motion events' };
    const typeLabel = plurals[this._selectedEventType] ?? (this._selectedEventType + 's');
    count.textContent = `${this._clipsList.length} ${typeLabel}`;
    if (this._clipsList.length === 0) {
      ph.style.display = 'flex'; ph.querySelector('span').textContent = `No ${typeLabel} found`;
      video.style.display = 'none'; video.removeAttribute('src');
      this.shadowRoot.getElementById('play-btn').classList.add('hidden'); this.updateNavigation();
    } else { this.loadClipByIndex(0); }
  }

  // ── Load clip (cache-first) ───────────────────────────────────────────

  async loadClipByIndex(index) {
    if (index < 0 || index >= this._clipsList.length) return;
    this._currentClipIndex = index;
    const clip = this._clipsList[index];
    const video    = this.shadowRoot.getElementById('video');
    const playBtn  = this.shadowRoot.getElementById('play-btn');
    const ph       = this.shadowRoot.getElementById('placeholder');
    const clipName = this.shadowRoot.getElementById('clip-name');
    const clipTime = this.shadowRoot.getElementById('clip-time');
    const clipTimeInner = this.shadowRoot.getElementById('clip-time-inner');
    const badge    = this.shadowRoot.getElementById('event-badge');
    const cacheBadge = this.shadowRoot.getElementById('cache-badge');

    // Reset cache badge
    cacheBadge.style.display = 'none';

    try {
      let clipUrl = null;
      let isCached = false;

      // ── Cache-first resolution ──
      if (this._cacheEnabled) {
        const cached = await this._resolveFromCache(clip.media_content_id);
        if (cached && cached.url) {
          clipUrl = cached.url;
          isCached = true;
          console.debug('[reolink-clips] Playing from CACHE:', clipUrl);
        }
      }

      // ── Fall back to NVR ──
      if (!clipUrl) {
        const resolved = await this._hass.callWS({ type: 'media_source/resolve_media', media_content_id: clip.media_content_id });
        clipUrl = resolved.url;
        console.debug('[reolink-clips] Playing from NVR:', clipUrl);
      }

      video.pause(); video.removeAttribute('src'); video.load();
      video.src = clipUrl; video.style.display = 'block';
      ph.style.display = 'none'; playBtn.classList.remove('hidden');

      const parsed = this._parseClipTitle(clip.title, clip.eventType);
      clipTimeInner.textContent = parsed.time;
      clipTime.textContent      = parsed.datetime;
      badge.textContent         = parsed.eventType;
      badge.className           = `clip-badge ${parsed.eventType.toLowerCase()}`;

      // Show cache badge if locally cached
      if (isCached) {
        cacheBadge.style.display = 'inline-flex';
      }

      clipName.innerHTML = `${parsed.eventType} <span class="sep">·</span> ${parsed.time}`;
      const camLabel = this.shadowRoot.getElementById('player-cam-label');
      if (camLabel) camLabel.textContent = this._config.cameras[this._selectedCamera].name;
      this.updateNavigation();
    } catch (err) { console.error('[reolink-clips] Failed to load clip:', err); }
  }

  _parseClipTitle(title, eventType) {
    const base = title.replace('.mp4', '').split(' ')[0];
    let time = '--:--'; let datetime = '';
    if (base.includes(':')) {
      const parts = base.split(':');
      if (parts.length >= 2) {
        const h = parseInt(parts[0]); const m = parts[1]; const s = parts[2] || '00';
        const ampm = h >= 12 ? 'pm' : 'am'; const h12 = h % 12 || 12;
        time = `${h12}:${m}:${s} ${ampm.toUpperCase()}`;
        const now = new Date();
        const days = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
        const dd = String(now.getDate()).padStart(2,'0'); const mm = String(now.getMonth()+1).padStart(2,'0');
        const yyyy = now.getFullYear(); const hh = String(h).padStart(2,'0'); const day = days[now.getDay()];
        datetime = `${dd}/${mm}/${yyyy} ${hh}:${m}:${s} ${ampm} ${day}`;
      }
    }
    return { time, datetime, eventType: eventType || 'Event' };
  }

  updateNavigation() {
    const hasPrev = this._currentClipIndex < this._clipsList.length - 1;
    const hasNext = this._currentClipIndex > 0;
    this.shadowRoot.getElementById('prev-btn').disabled = !hasPrev;
    this.shadowRoot.getElementById('next-btn').disabled = !hasNext;
    this.shadowRoot.getElementById('fs-prev').disabled  = !hasPrev;
    this.shadowRoot.getElementById('fs-next').disabled  = !hasNext;
    this.shadowRoot.getElementById('clip-index').textContent = this._clipsList.length > 0
      ? `${this._currentClipIndex + 1} / ${this._clipsList.length}` : '— / —';
  }

  prevClip() { if (this._currentClipIndex < this._clipsList.length - 1) this.loadClipByIndex(this._currentClipIndex + 1); }
  nextClip() { if (this._currentClipIndex > 0) this.loadClipByIndex(this._currentClipIndex - 1); }

  _doPlay() { this.shadowRoot.getElementById('video').play(); this.shadowRoot.getElementById('play-btn').classList.add('hidden'); }
  _togglePlay() {
    const video = this.shadowRoot.getElementById('video');
    const playBtn = this.shadowRoot.getElementById('play-btn');
    if (video.paused) { video.play(); playBtn.classList.add('hidden'); }
    else { video.pause(); playBtn.classList.remove('hidden'); }
  }
  _onVideoEnded() { this.shadowRoot.getElementById('play-btn').classList.remove('hidden'); }

  openFullscreen() {
    const video = this.shadowRoot.getElementById('video');
    const fsVideo = this.shadowRoot.getElementById('fs-video');
    const cam = this._config.cameras[this._selectedCamera];
    this.shadowRoot.getElementById('fs-title').textContent = cam.name;
    this.shadowRoot.getElementById('fs-subtitle').textContent = this.shadowRoot.getElementById('clip-name').textContent;
    fsVideo.src = video.src; fsVideo.currentTime = video.currentTime;
    this.shadowRoot.getElementById('fs-overlay').classList.add('active');
    video.pause(); fsVideo.play().catch(() => {});
  }

  _syncFsVideo() {
    const video = this.shadowRoot.getElementById('video');
    const fsVideo = this.shadowRoot.getElementById('fs-video');
    setTimeout(() => {
      fsVideo.src = video.src; fsVideo.play().catch(() => {});
      this.shadowRoot.getElementById('fs-subtitle').textContent = this.shadowRoot.getElementById('clip-name').textContent;
    }, 150);
  }

  closeFullscreen() {
    const fsVideo = this.shadowRoot.getElementById('fs-video');
    const video = this.shadowRoot.getElementById('video');
    video.currentTime = fsVideo.currentTime; fsVideo.pause();
    this.shadowRoot.getElementById('fs-overlay').classList.remove('active');
    this.shadowRoot.getElementById('play-btn').classList.remove('hidden');
  }

  getCardSize() { return 5; }
}

customElements.define('reolink-clips-card', ReolinkClipsCard);

window.customCards = window.customCards || [];
window.customCards.push({ type: 'reolink-clips-card', name: 'Reolink Clips Card', description: 'View event clips from Reolink NVR with local cache support — Earthy Dark Edition v5.0' });

console.info(
  '%c REOLINK-CLIPS %c v5.0 Cache ',
  'background:#9A8873;color:#1a130d;font-weight:bold;padding:2px 6px;border-radius:4px 0 0 4px',
  'background:#4ade80;color:#171614;padding:2px 6px;border-radius:0 4px 4px 0'
);
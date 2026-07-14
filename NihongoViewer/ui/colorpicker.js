// ---- Custom color picker ----------------------------------------------------
// Replaces the native <input type="color"> (WebView2's popup only commits its
// value when dismissed by clicking elsewhere). This one is an in-app popup with
// an explicit Save button: nothing is committed until the user clicks Save.
//
// Usage:
//   const picker = new ColorPicker(swatchEl, { onSave(hex) { ... } });
//   picker.value = "#ffffff";   // set (updates the swatch)
//   picker.value;               // get the committed hex
//
// The swatch element's background reflects the committed color. Opening the
// popup edits a working copy; Save commits it (updates swatch + fires onSave),
// Cancel / Escape / click-outside revert to the value the popup opened with.

(function () {
  // ---- color math (all hex are "#rrggbb") -----------------------------------
  function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }

  function normalizeHex(hex) {
    if (typeof hex !== "string") return "#000000";
    let h = hex.trim().replace(/^#/, "");
    if (/^[0-9a-fA-F]{3}$/.test(h)) h = h.split("").map((c) => c + c).join("");
    if (!/^[0-9a-fA-F]{6}$/.test(h)) return null;
    return "#" + h.toLowerCase();
  }

  function hexToRgb(hex) {
    const h = normalizeHex(hex) || "#000000";
    return {
      r: parseInt(h.slice(1, 3), 16),
      g: parseInt(h.slice(3, 5), 16),
      b: parseInt(h.slice(5, 7), 16),
    };
  }

  function rgbToHex(r, g, b) {
    const to = (n) => clamp(Math.round(n), 0, 255).toString(16).padStart(2, "0");
    return "#" + to(r) + to(g) + to(b);
  }

  // h in [0,360), s/v in [0,1]
  function rgbToHsv(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
    let h = 0;
    if (d) {
      if (max === r) h = ((g - b) / d) % 6;
      else if (max === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h *= 60;
      if (h < 0) h += 360;
    }
    const s = max === 0 ? 0 : d / max;
    return { h, s, v: max };
  }

  function hsvToRgb(h, s, v) {
    const c = v * s;
    const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
    const m = v - c;
    let r = 0, g = 0, b = 0;
    if (h < 60) { r = c; g = x; }
    else if (h < 120) { r = x; g = c; }
    else if (h < 180) { g = c; b = x; }
    else if (h < 240) { g = x; b = c; }
    else if (h < 300) { r = x; b = c; }
    else { r = c; b = x; }
    return { r: (r + m) * 255, g: (g + m) * 255, b: (b + m) * 255 };
  }

  // ---- shared popup (one DOM instance, reused by every picker) ---------------
  let popup = null;      // the root element
  let els = null;        // cached child elements
  let active = null;     // the ColorPicker currently editing, or null

  function buildPopup() {
    popup = document.createElement("div");
    popup.className = "cp-popup";
    popup.setAttribute("role", "dialog");
    popup.innerHTML = `
      <div class="cp-sv" tabindex="-1">
        <div class="cp-sv-sat"></div>
        <div class="cp-sv-val"></div>
        <div class="cp-sv-thumb"></div>
      </div>
      <div class="cp-hue">
        <div class="cp-hue-thumb"></div>
      </div>
      <div class="cp-row">
        <span class="cp-preview"></span>
        <label class="cp-hex-wrap">#<input class="cp-hex" maxlength="7" spellcheck="false" /></label>
        <label class="cp-num"><span>R</span><input class="cp-r" type="number" min="0" max="255" /></label>
        <label class="cp-num"><span>G</span><input class="cp-g" type="number" min="0" max="255" /></label>
        <label class="cp-num"><span>B</span><input class="cp-b" type="number" min="0" max="255" /></label>
      </div>
      <div class="cp-actions">
        <button class="cp-btn cp-eyedropper" type="button" title="Pick a color from the screen">⿴ Pick</button>
        <span class="cp-spacer"></span>
        <button class="cp-btn cp-cancel" type="button">Cancel</button>
        <button class="cp-btn cp-save" type="button">Save</button>
      </div>`;
    document.body.appendChild(popup);

    els = {
      sv: popup.querySelector(".cp-sv"),
      svThumb: popup.querySelector(".cp-sv-thumb"),
      hue: popup.querySelector(".cp-hue"),
      hueThumb: popup.querySelector(".cp-hue-thumb"),
      preview: popup.querySelector(".cp-preview"),
      hex: popup.querySelector(".cp-hex"),
      r: popup.querySelector(".cp-r"),
      g: popup.querySelector(".cp-g"),
      b: popup.querySelector(".cp-b"),
      eyedropper: popup.querySelector(".cp-eyedropper"),
      cancel: popup.querySelector(".cp-cancel"),
      save: popup.querySelector(".cp-save"),
    };

    if (!window.EyeDropper) els.eyedropper.style.display = "none";

    wireDrag(els.sv, (px, py) => {
      active.s = clamp(px, 0, 1);
      active.v = clamp(1 - py, 0, 1);
      syncFromHsv();
    });
    wireDrag(els.hue, (px) => {
      active.h = clamp(px, 0, 1) * 360;
      syncFromHsv();
    });

    els.hex.addEventListener("input", () => {
      const norm = normalizeHex(els.hex.value);
      if (norm) setWorkingHex(norm);
    });
    [["r", "r"], ["g", "g"], ["b", "b"]].forEach(([key]) => {
      els[key].addEventListener("input", () => {
        const rgb = {
          r: clamp(parseInt(els.r.value, 10) || 0, 0, 255),
          g: clamp(parseInt(els.g.value, 10) || 0, 0, 255),
          b: clamp(parseInt(els.b.value, 10) || 0, 0, 255),
        };
        setWorkingHex(rgbToHex(rgb.r, rgb.g, rgb.b), true);
      });
    });

    els.eyedropper.addEventListener("click", async () => {
      if (!window.EyeDropper) return;
      try {
        const res = await new window.EyeDropper().open();
        const norm = normalizeHex(res.sRGBHex);
        if (norm) setWorkingHex(norm);
      } catch (e) { /* user cancelled */ }
    });

    els.cancel.addEventListener("click", () => closePopup(false));
    els.save.addEventListener("click", () => closePopup(true));

    // Click-outside closes and reverts. Registered lazily while a popup is open.
    document.addEventListener("mousedown", onDocMouseDown, true);
    document.addEventListener("keydown", (e) => {
      if (!active) return;
      if (e.key === "Escape") closePopup(false);
      else if (e.key === "Enter" && e.target !== els.hex) closePopup(true);
    });
    window.addEventListener("resize", () => { if (active) positionPopup(); });
  }

  function onDocMouseDown(e) {
    if (!active) return;
    if (popup.contains(e.target) || active.swatch.contains(e.target)) return;
    closePopup(false);
  }

  // Drag helper: reports the pointer position within `el` as fractions [0,1].
  function wireDrag(el, onMove) {
    let dragging = false;
    const report = (e) => {
      const rect = el.getBoundingClientRect();
      const px = (e.clientX - rect.left) / rect.width;
      const py = (e.clientY - rect.top) / rect.height;
      onMove(px, py);
    };
    el.addEventListener("mousedown", (e) => {
      dragging = true;
      report(e);
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => { if (dragging) report(e); });
    document.addEventListener("mouseup", () => { dragging = false; });
  }

  // ---- working-state <-> UI sync --------------------------------------------
  // The popup edits active.{h,s,v}. These push that state into the widgets.

  function syncFromHsv() {
    const rgb = hsvToRgb(active.h, active.s, active.v);
    const hex = rgbToHex(rgb.r, rgb.g, rgb.b);
    renderWidgets(hex, rgb);
  }

  // Set working color from an RGB/hex source (hex or R/G/B inputs).
  // fromNumbers keeps the hex field untouched so typing isn't disrupted.
  function setWorkingHex(hex, fromNumbers) {
    const rgb = hexToRgb(hex);
    const hsv = rgbToHsv(rgb.r, rgb.g, rgb.b);
    active.h = hsv.h; active.s = hsv.s; active.v = hsv.v;
    renderWidgets(hex, rgb, { skipHex: false, skipNumbers: fromNumbers });
  }

  function renderWidgets(hex, rgb, opts = {}) {
    // SV area background = the pure hue; thumb at (s, 1-v).
    const hueRgb = hsvToRgb(active.h, 1, 1);
    els.sv.style.setProperty("--cp-hue", rgbToHex(hueRgb.r, hueRgb.g, hueRgb.b));
    els.svThumb.style.left = active.s * 100 + "%";
    els.svThumb.style.top = (1 - active.v) * 100 + "%";
    els.hueThumb.style.left = (active.h / 360) * 100 + "%";
    els.svThumb.style.background = hex;
    els.preview.style.background = hex;
    if (!opts.skipHex && document.activeElement !== els.hex) els.hex.value = hex;
    if (!opts.skipNumbers) {
      els.r.value = Math.round(rgb.r);
      els.g.value = Math.round(rgb.g);
      els.b.value = Math.round(rgb.b);
    }
    active._working = hex;
  }

  function positionPopup() {
    const rect = active.swatch.getBoundingClientRect();
    const pw = popup.offsetWidth, ph = popup.offsetHeight;
    const margin = 8;
    let left = rect.left;
    let top = rect.bottom + 6;
    if (left + pw > window.innerWidth - margin) left = window.innerWidth - pw - margin;
    if (left < margin) left = margin;
    if (top + ph > window.innerHeight - margin) top = rect.top - ph - 6; // flip above
    if (top < margin) top = margin;
    popup.style.left = left + window.scrollX + "px";
    popup.style.top = top + window.scrollY + "px";
  }

  function openPopup(picker) {
    if (!popup) buildPopup();
    active = picker;
    const hsv = rgbToHsv(...Object.values(hexToRgb(picker.value)));
    active.h = hsv.h; active.s = hsv.s; active.v = hsv.v;
    syncFromHsv();
    popup.classList.add("open");
    positionPopup();
    els.hex.value = normalizeHex(picker.value);
  }

  function closePopup(commit) {
    if (!active) return;
    const picker = active;
    const working = picker._working;
    active = null;
    popup.classList.remove("open");
    if (commit && working) picker._commit(working);
  }

  // ---- public class ---------------------------------------------------------
  class ColorPicker {
    constructor(swatch, opts = {}) {
      this.swatch = swatch;
      this.onSave = opts.onSave || null;
      this._value = normalizeHex(swatch.dataset.value || "#000000") || "#000000";
      this._working = this._value;
      this._renderSwatch();
      swatch.addEventListener("click", (e) => {
        e.preventDefault();
        if (active === this) closePopup(false);
        else openPopup(this);
      });
    }

    get value() { return this._value; }
    set value(hex) {
      const norm = normalizeHex(hex);
      if (norm) { this._value = norm; this._renderSwatch(); }
    }

    _renderSwatch() {
      this.swatch.style.setProperty("--swatch", this._value);
    }

    // Called by Save: update the committed value + notify.
    _commit(hex) {
      this.value = hex;
      if (this.onSave) this.onSave(hex);
    }
  }

  window.ColorPicker = ColorPicker;
})();

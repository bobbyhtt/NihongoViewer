// ---- Settings panel: UI-only interactions -----------------------------------

// Generic visual-only toggle groups. The OCR engine group (loads a model) and
// the Text mode group (persists + drives the overlay) are handled separately.
document.querySelectorAll(".toggle-group:not(#ocr-engine):not(#text-mode)").forEach((group) => {
  group.querySelectorAll(".toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      group.querySelectorAll(".toggle").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
    });
  });
});

const sizeSlider = document.getElementById("size-slider");
const sizeVal = document.getElementById("size-val");
sizeSlider.addEventListener("input", () => (sizeVal.textContent = sizeSlider.value));

const opacitySlider = document.getElementById("opacity-slider");
const opacityVal = document.getElementById("opacity-val");
opacitySlider.addEventListener("input", () => (opacityVal.textContent = opacitySlider.value));

// ---- Capture feature --------------------------------------------------------

const windowSelect = document.getElementById("window-select");
const refreshBtn = document.getElementById("refresh-btn");
const startBtn = document.getElementById("start-btn");
const previewImg = document.getElementById("preview-img");
const noPreview = document.getElementById("no-preview");

let capturing = false;

function hasApi() {
  return window.pywebview && window.pywebview.api;
}

// Latest capture-tick result (frame image + detected/translated text), shared
// with the Create Card "Capture" button so it can grab the current frame
// without triggering a second window capture. Updated on every OCR tick.
const lastCapture = { frame: null, ja: "", en: "" };
window.NihongoViewer = {
  lastCapture,
  isCapturing: () => capturing,
  currentEngine: () => currentEngine,
};

// Let other views (Create card's capture-status badge) react to capture/engine
// changes without polling.
function notifyCaptureChanged() {
  document.dispatchEvent(new CustomEvent("nv:capture-changed"));
}

async function loadWindows() {
  if (!hasApi()) return;
  windowSelect.innerHTML = '<option value="">Loading windows…</option>';
  const windows = await window.pywebview.api.list_windows();
  windowSelect.innerHTML = "";
  if (!windows.length) {
    windowSelect.innerHTML = '<option value="">No windows found</option>';
    return;
  }
  for (const w of windows) {
    const opt = document.createElement("option");
    opt.value = String(w.id);
    opt.textContent = w.title;
    windowSelect.appendChild(opt);
  }
}

function resetPreview() {
  previewImg.removeAttribute("src");
  previewImg.hidden = true;
  noPreview.textContent = "No preview";
  noPreview.hidden = false;
}

function setPreview(dataUrl) {
  if (dataUrl) {
    previewImg.src = dataUrl;
    previewImg.hidden = false;
    noPreview.hidden = true;
  }
}

async function startCapture() {
  const hwnd = Number(windowSelect.value);
  if (!hwnd) {
    alert("Please select a window first.");
    return;
  }
  await window.pywebview.api.start_capture(hwnd);
  saveSettings(); // ensure the overlay uses the latest settings
  capturing = true;
  notifyCaptureChanged();
  startBtn.textContent = "Stop";
  startBtn.classList.remove("start");
  startBtn.classList.add("stop");
  noPreview.textContent = "Capturing…";
  // A single loop drives preview + OCR + translation from ONE capture per tick.
  // (Capturing twice per tick made us call the game's PrintWindow ~4.7x/sec,
  // which can flash a DirectX game black for a frame — see process_frame.)
  startOcrLoop();
}

function stopCapture() {
  capturing = false;
  notifyCaptureChanged();
  stopOcrLoop();
  if (hasApi()) window.pywebview.api.stop_capture(); // hide the overlay
  startBtn.textContent = "Start";
  startBtn.classList.remove("stop");
  startBtn.classList.add("start");
  resetPreview();
}

startBtn.addEventListener("click", () => {
  if (!hasApi()) {
    alert("Run this through NihongoViewer (python main.py) to use capture.");
    return;
  }
  capturing ? stopCapture() : startCapture();
});

refreshBtn.addEventListener("click", () => {
  if (capturing) stopCapture();
  resetPreview();
  loadWindows();
});

// ---- OCR + translation + overlay --------------------------------------------

const ocrGroup = document.getElementById("ocr-engine");
const ocrStatus = document.getElementById("ocr-status");
const trStatus = document.getElementById("tr-status");
const detectedJa = document.getElementById("detected-ja");
const translatedEn = document.getElementById("translated-en");

// One capture per tick feeds both the preview and OCR/translation. Kept modest
// (~1 fps) on purpose: each tick calls the game's PrintWindow once, and a lower
// rate means far fewer chances of the DirectX black-frame flash.
const OCR_INTERVAL_MS = 1000;
let ocrTimer = null;
let ocrBusy = false; // guards against overlapping process_frame() calls
let engineReady = false;
let translatorReady = false;
let currentEngine = "MeikiOCR";

function setDetected(text, dim = false) {
  detectedJa.textContent = text;
  detectedJa.style.opacity = dim ? "0.5" : "";
}

function setTranslated(text, dim = false) {
  translatedEn.textContent = text;
  translatedEn.style.opacity = dim ? "0.5" : "";
}

function setOcrStatus(label) {
  ocrStatus.textContent = `● OCR: ${label}`;
}

// ---- Settings: mirror the panel to the overlay + persist to disk ------------

const hotkeyInput = document.getElementById("hotkey-input");
const hotkeySaveBtn = document.getElementById("hotkey-save");
const hotkeyStatus = document.getElementById("hotkey-status");
const textModeGroup = document.getElementById("text-mode");

function setHotkeyStatus(text, kind = "") {
  hotkeyStatus.textContent = text;
  hotkeyStatus.className = "field-hint" + (kind ? " " + kind : "");
}

// The committed hotkey (what's actually persisted + registered). The input can
// show a freshly-captured combo that isn't live until the user clicks Save.
let savedHotkey = "Ctrl + Shift + H";
const HOTKEY_MODS = new Set(["Ctrl", "Shift", "Alt", "Win"]);

// Tokenize on the spaced " + " our capture emits, so the "+" / "-" keys don't
// collide with the separator; fall back to bare "+" for compact specs.
function splitHotkey(spec) {
  const parts = spec.includes(" + ") || spec.trim() === "+"
    ? spec.split(" + ")
    : spec.split("+");
  return parts.map((s) => s.trim()).filter(Boolean);
}
function formatHotkey(spec) {
  return splitHotkey(spec).join(" + ");
}
function canonHotkey(spec) {
  return splitHotkey(spec).map((s) => s.toLowerCase()).join("+");
}
// Valid = optional modifiers followed by exactly one non-modifier key.
// A bare key (no modifier) is allowed; it's captured globally.
function isValidHotkey(spec) {
  const parts = splitHotkey(spec);
  if (parts.length < 1) return false;
  const key = parts[parts.length - 1];
  const mods = parts.slice(0, -1);
  return !HOTKEY_MODS.has(key) && mods.every((m) => HOTKEY_MODS.has(m));
}
// Save is enabled only for a valid combo that differs from the saved one.
function refreshHotkeySave() {
  const cur = hotkeyInput.value;
  hotkeySaveBtn.disabled = !(isValidHotkey(cur) && canonHotkey(cur) !== canonHotkey(savedHotkey));
}

function activeTextMode() {
  const active = textModeGroup.querySelector(".toggle.active");
  return (active && active.dataset.mode) || "single";
}

function setTextModeButtons(mode) {
  textModeGroup.querySelectorAll(".toggle").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === mode));
}

// MangaOCR reads whole bubbles as one block, so it only supports Single mode.
// Disable the Duo button while MangaOCR is active, and force Single if Duo was on.
function applyTextModeConstraint(engine) {
  const duoBtn = textModeGroup.querySelector('.toggle[data-mode="duo"]');
  const singleOnly = engine === "MangaOCR";
  duoBtn.disabled = singleOnly;
  if (singleOnly && activeTextMode() === "duo") setTextModeButtons("single");
}

// Custom color pickers (replace the native <input type="color">). Committing a
// color via the popup's Save button applies + persists it live via saveSettings.
const textColorPicker = new ColorPicker(document.getElementById("text-color"), {
  onSave: saveSettings,
});
const bgColorPicker = new ColorPicker(document.getElementById("bg-color"), {
  onSave: saveSettings,
});

// The full settings object the backend persists (a superset of the overlay style).
function currentSettings() {
  return {
    font: document.getElementById("font-select").value,
    size: Number(sizeSlider.value),
    text_color: textColorPicker.value,
    bg_color: bgColorPicker.value,
    opacity: Number(opacitySlider.value),
    offset_x: Number(document.getElementById("offset-x").value) || 0,
    offset_y: Number(document.getElementById("offset-y").value) || 0,
    text_mode: activeTextMode(),
    hotkey: savedHotkey, // only the committed hotkey; capture stays in the field
  };
}

// Persist + live-apply. Called on every settings change (the backend also saves
// the OCR engine choice itself, so we don't send it here).
function saveSettings() {
  if (hasApi()) window.pywebview.api.update_settings(currentSettings());
}

// Any setting that affects the overlay saves the new value live. (Color swatches
// persist through their own Save button — see the ColorPicker onSave above.)
document.getElementById("font-select").addEventListener("change", saveSettings);
sizeSlider.addEventListener("change", saveSettings);
opacitySlider.addEventListener("change", saveSettings);

// Position offset: apply on every keystroke / spinner nudge so the overlay
// moves as you tune it (the backend redraws the current frame immediately).
["offset-x", "offset-y"].forEach((id) =>
  document.getElementById(id).addEventListener("input", saveSettings));

// Text mode (Single vs Duo): update the buttons, then persist the choice.
textModeGroup.querySelectorAll(".toggle").forEach((btn) => {
  btn.addEventListener("click", () => {
    textModeGroup.querySelectorAll(".toggle").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    saveSettings();
  });
});

// Hotkey capture: focus the field and press a combo. Capturing only fills the
// field — the user commits it with the Save button (so a mistyped combo is easy
// to redo). We build the spec from the live modifier state + the pressed key.
const MODIFIER_KEYS = new Set(["Control", "Shift", "Alt", "Meta"]);
function heldModifiers(e) {
  const parts = [];
  if (e.ctrlKey) parts.push("Ctrl");
  if (e.shiftKey) parts.push("Shift");
  if (e.altKey) parts.push("Alt");
  if (e.metaKey) parts.push("Win");
  return parts;
}
hotkeyInput.addEventListener("keydown", (e) => {
  e.preventDefault(); // don't type the key into the field
  if (MODIFIER_KEYS.has(e.key)) {
    // Live feedback while only modifiers are held (e.g. "Ctrl + Shift + …").
    const held = heldModifiers(e);
    if (held.length) hotkeyInput.value = held.join(" + ") + " + …";
    return; // wait for the real (non-modifier) key
  }
  const parts = heldModifiers(e);
  let key = e.key;
  if (key === " ") key = "Space";
  else if (key.length === 1) key = key.toUpperCase();
  parts.push(key);
  hotkeyInput.value = parts.join(" + ");
  refreshHotkeySave();
});

// If the user clicks away with only modifiers held (a dangling "Ctrl + …"),
// restore the committed combo so the field never shows a half-typed spec.
hotkeyInput.addEventListener("blur", () => {
  if (hotkeyInput.value.endsWith("…")) {
    hotkeyInput.value = formatHotkey(savedHotkey);
    refreshHotkeySave();
  }
});

// Commit the captured hotkey: persist + register it, then report the outcome.
// Only mark it saved if the OS actually accepted the combo.
hotkeySaveBtn.addEventListener("click", async () => {
  if (hotkeySaveBtn.disabled) return;
  const spec = formatHotkey(hotkeyInput.value);
  hotkeySaveBtn.disabled = true;
  let res;
  try {
    res = await window.pywebview.api.update_settings({ hotkey: spec });
  } catch (e) {
    res = { ok: false };
  }
  const hk = res && res.hotkey;
  if (hk && hk.ok === false) {
    // The OS refused it (usually another app owns that combo). Keep it editable.
    setHotkeyStatus(`✗ "${spec}" is unavailable — try another combo`, "err");
    refreshHotkeySave();
    return;
  }
  savedHotkey = spec;
  hotkeyInput.value = spec;
  refreshHotkeySave(); // now disabled again (nothing pending)
  setHotkeyStatus(`✓ Active: ${spec}`, "ok");
});

// Reflect saved settings into every control on launch.
function applySettings(s) {
  const fontSelect = document.getElementById("font-select");
  fontSelect.value = s.font;
  if (!fontSelect.value) fontSelect.selectedIndex = 0; // saved font no longer offered
  sizeSlider.value = s.size;
  sizeVal.textContent = s.size;
  textColorPicker.value = s.text_color;
  bgColorPicker.value = s.bg_color;
  opacitySlider.value = s.opacity;
  opacityVal.textContent = s.opacity;
  document.getElementById("offset-x").value = s.offset_x;
  document.getElementById("offset-y").value = s.offset_y;
  savedHotkey = formatHotkey(s.hotkey);
  hotkeyInput.value = savedHotkey;
  refreshHotkeySave(); // Save starts disabled (nothing changed yet)
  setHotkeyStatus(`Active: ${savedHotkey}`);
  setTextModeButtons(s.text_mode);
  currentEngine = s.ocr_engine || currentEngine;
  applyTextModeConstraint(currentEngine); // disable Duo if the saved engine is MangaOCR
  currentSpeed = s.ocr_speed || currentSpeed;
  setSpeedButtons(currentSpeed); // reflect the saved OCR speed in the dialog
}

// Load the translation backend (MADLAD-400). Slow on first run (model download
// ~1.65 GB).
async function loadTranslator() {
  if (!hasApi()) return;
  trStatus.textContent = "● MADLAD-400 (loading…)";
  let res;
  try {
    res = await window.pywebview.api.load_translator();
  } catch (e) {
    res = { ok: false, error: String(e) };
  }
  translatorReady = !!(res && res.ok);
  const backend = (res && res.backend) || "MADLAD-400";
  trStatus.textContent = translatorReady ? `● ${backend}` : "● MADLAD-400 (unavailable)";
}

// Select an OCR engine and load its weights. Switches the pipeline *stage*
// (per CLAUDE.md) — no app restart. Returns once the model is ready or failed.
async function selectEngine(name) {
  if (!hasApi()) return;
  currentEngine = name;
  engineReady = false;

  // Reflect the choice immediately in the toggle buttons.
  ocrGroup.querySelectorAll(".toggle").forEach((b) => {
    b.classList.toggle("active", b.dataset.engine === name);
    b.disabled = true;
  });
  setOcrStatus(`${name} (loading…)`);

  let res;
  try {
    res = await window.pywebview.api.set_ocr_engine(name);
  } catch (e) {
    res = { ok: false, error: String(e) };
  }

  ocrGroup.querySelectorAll(".toggle").forEach((b) => (b.disabled = false));

  if (res && res.ok) {
    engineReady = true;
    setOcrStatus(name);
    notifyCaptureChanged(); // refresh the Create-card status badge with the engine
    // Backend may have forced Single (MangaOCR); mirror it and lock the Duo
    // button so it can't be re-selected while MangaOCR is active.
    if (res.text_mode) setTextModeButtons(res.text_mode);
    applyTextModeConstraint(name);
  } else {
    engineReady = false;
    setOcrStatus(`${name} (unavailable)`);
    const msg = (res && res.error) || "failed to load";
    setDetected(`${name} unavailable — ${msg}`, true);
  }
}

ocrGroup.querySelectorAll(".toggle").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.engine !== currentEngine) selectEngine(btn.dataset.engine);
  });
});

// ---- OCR speed dialog ("Configure OCR") -------------------------------------
// A speed/quality tradeoff applied live to the active engine (no reload). The
// chosen preset persists and is reflected here on launch via applySettings.

const ocrModal = document.getElementById("ocr-modal");
const speedOptions = document.getElementById("speed-options");
let currentSpeed = "balanced";

function setSpeedButtons(speed) {
  speedOptions.querySelectorAll(".speed-opt").forEach((b) =>
    b.classList.toggle("active", b.dataset.speed === speed));
}

function openOcrModal() {
  setSpeedButtons(currentSpeed);
  ocrModal.hidden = false;
}
function closeOcrModal() {
  ocrModal.hidden = true;
}

document.getElementById("configure-ocr").addEventListener("click", (e) => {
  e.preventDefault();
  openOcrModal();
});
document.getElementById("ocr-modal-close").addEventListener("click", closeOcrModal);
// Click the dim backdrop (outside the dialog) or press Escape to dismiss.
ocrModal.addEventListener("click", (e) => {
  if (e.target === ocrModal) closeOcrModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !ocrModal.hidden) closeOcrModal();
});

speedOptions.querySelectorAll(".speed-opt").forEach((btn) => {
  btn.addEventListener("click", () => {
    const speed = btn.dataset.speed;
    currentSpeed = speed;
    setSpeedButtons(speed);
    if (hasApi()) window.pywebview.api.set_ocr_speed(speed);
  });
});

async function ocrTick() {
  if (ocrBusy || !engineReady || !capturing) return;
  const hwnd = Number(windowSelect.value);
  if (!hwnd) return;
  ocrBusy = true;
  try {
    const res = await window.pywebview.api.process_frame(hwnd);
    if (!capturing) return; // stopped while we were awaiting
    if (res && res.closed) {
      // Target window was closed — stop capturing and drop the overlay.
      stopCapture();
      noPreview.textContent = "Window closed — capture stopped";
      loadWindows(); // refresh the list (the window is gone)
      return;
    }
    if (!res || !res.ok) {
      if (res && res.error) setDetected(res.error, true);
      noPreview.textContent = "Could not capture this window";
      noPreview.hidden = false;
      previewImg.hidden = true;
      return;
    }
    if (res.frame) setPreview(res.frame); // preview shares this capture
    // Cache this frame + text so the Create Card "Capture" button can reuse it.
    lastCapture.frame = res.frame || null;
    lastCapture.ja = (res.ja || "").trim();
    lastCapture.en = (res.en || "").trim();
    const ja = (res.ja || "").trim();
    if (ja) setDetected(ja);
    else {
      setDetected("(no Japanese text detected)", true);
      setTranslated("—", true);
      return;
    }
    const en = (res.en || "").trim();
    if (en) setTranslated(en);
    else setTranslated(translatorReady ? "…" : "(translator loading…)", true);
  } catch (e) {
    setDetected(String(e), true);
  } finally {
    ocrBusy = false;
  }
}

function startOcrLoop() {
  if (ocrTimer) return;
  if (!engineReady) setDetected("Loading OCR engine…", true);
  ocrTick();
  ocrTimer = setInterval(ocrTick, OCR_INTERVAL_MS);
}

function stopOcrLoop() {
  clearInterval(ocrTimer);
  ocrTimer = null;
  ocrBusy = false;
}

// The pywebview API becomes available after this event fires.
window.addEventListener("pywebviewready", async () => {
  loadWindows();
  // Restore saved settings into the controls before anything reads them.
  try {
    const saved = await window.pywebview.api.get_settings();
    if (saved) applySettings(saved);
  } catch (e) {
    /* fall back to the HTML defaults */
  }
  saveSettings(); // seed the overlay with the restored settings
  selectEngine(currentEngine); // load the saved/default OCR engine up front
  loadTranslator(); // and the translation backend (both download on first run)
});

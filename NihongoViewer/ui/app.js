// ---- Settings panel: UI-only interactions -----------------------------------

// Generic visual-only toggle groups. The Text mode and Capture mode groups
// (which persist + drive behavior) are handled separately.
document.querySelectorAll(".toggle-group:not(#text-mode):not(#capture-mode)").forEach((group) => {
  group.querySelectorAll(".toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      group.querySelectorAll(".toggle").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
    });
  });
});

// ---- Capture mode (Screen / Area) -------------------------------------------
const captureModeGroup = document.getElementById("capture-mode");
const configureAreaBtn = document.getElementById("configure-area-btn");

function activeCaptureMode() {
  const b = captureModeGroup.querySelector(".toggle.active");
  return b ? b.dataset.capmode : "screen";
}
function setCaptureModeButtons(mode) {
  captureModeGroup.querySelectorAll(".toggle").forEach((b) =>
    b.classList.toggle("active", b.dataset.capmode === mode));
  // Area mode grabs the screen directly (no window), so swap the window picker +
  // refresh for the Configure Area button. Screen mode keeps them.
  const area = mode === "area";
  configureAreaBtn.hidden = !area;
  document.getElementById("window-select").hidden = area;
  document.getElementById("refresh-btn").hidden = area;
}
captureModeGroup.querySelectorAll(".toggle").forEach((btn) => {
  btn.addEventListener("click", () => {
    setCaptureModeButtons(btn.dataset.capmode);
    saveSettings();
  });
});
configureAreaBtn.addEventListener("click", async () => {
  if (!hasApi()) return;
  // Opens the fullscreen editor; drag the green (detect) and blue (translate)
  // boxes anywhere on screen, then Save.
  const res = await window.pywebview.api.configure_area();
  if (res && res.error) alert(res.error);
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
  // Called from Python (main.Api._capture_card_hotkey) when the global
  // Create-card capture hotkey fires. Fan out to whoever owns the stack (cards.js)
  // via a DOM event — same decoupling as nv:capture-changed.
  onHotkeyCapture: () => document.dispatchEvent(new CustomEvent("nv:hotkey-capture")),
};

// Let other views (Create card's capture-status badge) react to capture/engine
// changes without polling.
function notifyCaptureChanged() {
  document.dispatchEvent(new CustomEvent("nv:capture-changed"));
}

// ---- Overlay show/hide toggle (SETTING panel status) ------------------------
const overlayToggle = document.getElementById("overlay-toggle");
const overlayToggleLabel = document.getElementById("overlay-toggle-label");
let overlayVisible = false; // reflects the running overlay, not a saved setting

// Render the toggle. Disabled (and "Overlay off") whenever we're not capturing,
// since there's no overlay to control; otherwise "Shown"/"Hidden" with a colored
// dot. Kept purely presentational so both the click and the hotkey use it.
function renderOverlayToggle() {
  overlayToggle.disabled = !capturing;
  overlayToggle.classList.toggle("on", capturing && overlayVisible);
  overlayToggle.classList.toggle("off", capturing && !overlayVisible);
  overlayToggleLabel.textContent = !capturing
    ? "Overlay off"
    : overlayVisible ? "Overlay shown" : "Overlay hidden";
}

// Called from Python (Api._notify_overlay_state) when the global hide/show hotkey
// flips the overlay, so the toggle stays in sync with the hotkey.
function setOverlayVisibleState(visible) {
  overlayVisible = !!visible;
  renderOverlayToggle();
}

overlayToggle.addEventListener("click", async () => {
  if (!capturing || !hasApi()) return;
  const res = await window.pywebview.api.set_overlay_visible(!overlayVisible);
  if (res && typeof res.visible === "boolean") overlayVisible = res.visible;
  renderOverlayToggle();
});

window.NihongoViewer.onOverlayToggled = setOverlayVisibleState;

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
  const areaMode = activeCaptureMode() === "area";
  if (!hwnd && !areaMode) {
    alert("Please select a window first.");
    return;
  }
  const res = await window.pywebview.api.start_capture(hwnd || 0);
  saveSettings(); // ensure the overlay uses the latest settings
  capturing = true;
  // Start always re-shows the overlay (see Api.start_capture); reflect that.
  overlayVisible = !res || res.overlay_visible !== false;
  renderOverlayToggle();
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
  overlayVisible = false;
  renderOverlayToggle();
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
    alert("Run this through Yakutori (python main.py) to use capture.");
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
let currentEngine = "RapidOCR";

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

const textModeGroup = document.getElementById("text-mode");

// The committed combos (what's actually persisted + registered). Each hotkey
// input can show a freshly-captured combo that isn't live until the user Saves.
let savedHotkey = "Alt + V";                // hide/show overlay
let savedCardHotkey = "Alt + C";            // capture into the Create-card stack
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

function activeTextMode() {
  const active = textModeGroup.querySelector(".toggle.active");
  return (active && active.dataset.mode) || "single";
}

function setTextModeButtons(mode) {
  textModeGroup.querySelectorAll(".toggle").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === mode));
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
    capture_mode: activeCaptureMode(),
    // Only the committed combos; a freshly-captured one stays in its field until
    // Saved. Resent unchanged on style saves — the backend only rebinds on change.
    hotkey: savedHotkey,
    card_hotkey: savedCardHotkey,
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

// Hotkey capture: focus a field and press a combo. Capturing only fills the
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

// Wire one hotkey field (input + Save button + status hint). Both global
// hotkeys — hide/show and Create-card capture — share identical capture/commit
// behavior, differing only in which setting key + rebind result they carry.
// `getSaved`/`setSaved` bridge to the module-level committed combo so the rest
// of the settings code (currentSettings / applySettings) can read it.
function createHotkeyField({ inputId, saveId, statusId, settingKey, resultKey, getSaved, setSaved }) {
  const input = document.getElementById(inputId);
  const saveBtn = document.getElementById(saveId);
  const status = document.getElementById(statusId);

  function setStatus(text, kind = "") {
    status.textContent = text;
    status.className = "field-hint" + (kind ? " " + kind : "");
  }
  // Save is enabled only for a valid combo that differs from the saved one.
  function refreshSave() {
    const cur = input.value;
    saveBtn.disabled = !(isValidHotkey(cur) && canonHotkey(cur) !== canonHotkey(getSaved()));
  }

  input.addEventListener("keydown", (e) => {
    e.preventDefault(); // don't type the key into the field
    if (MODIFIER_KEYS.has(e.key)) {
      // Live feedback while only modifiers are held (e.g. "Ctrl + Shift + …").
      const held = heldModifiers(e);
      if (held.length) input.value = held.join(" + ") + " + …";
      return; // wait for the real (non-modifier) key
    }
    const parts = heldModifiers(e);
    let key = e.key;
    if (key === " ") key = "Space";
    else if (key.length === 1) key = key.toUpperCase();
    parts.push(key);
    input.value = parts.join(" + ");
    refreshSave();
  });

  // If the user clicks away with only modifiers held (a dangling "Ctrl + …"),
  // restore the committed combo so the field never shows a half-typed spec.
  input.addEventListener("blur", () => {
    if (input.value.endsWith("…")) {
      input.value = formatHotkey(getSaved());
      refreshSave();
    }
  });

  // Commit the captured hotkey: persist + register it, then report the outcome.
  // Only mark it saved if the OS actually accepted the combo.
  saveBtn.addEventListener("click", async () => {
    if (saveBtn.disabled) return;
    const spec = formatHotkey(input.value);
    saveBtn.disabled = true;
    let res;
    try {
      res = await window.pywebview.api.update_settings({ [settingKey]: spec });
    } catch (e) {
      res = { ok: false };
    }
    const hk = res && res[resultKey];
    if (hk && hk.ok === false) {
      // The OS refused it (usually another app owns that combo). Keep it editable.
      setStatus(`✗ "${spec}" is unavailable — try another combo`, "err");
      refreshSave();
      return;
    }
    setSaved(spec);
    input.value = spec;
    refreshSave(); // now disabled again (nothing pending)
    setStatus(`✓ Active: ${spec}`, "ok");
  });

  // Reflect a saved combo into the field on launch (Save starts disabled).
  function applySaved(spec) {
    setSaved(formatHotkey(spec));
    input.value = getSaved();
    refreshSave();
    setStatus(`Active: ${getSaved()}`);
  }

  return { applySaved };
}

const hideShowHotkeyField = createHotkeyField({
  inputId: "hotkey-input", saveId: "hotkey-save", statusId: "hotkey-status",
  settingKey: "hotkey", resultKey: "hotkey",
  getSaved: () => savedHotkey, setSaved: (v) => (savedHotkey = v),
});
const cardHotkeyField = createHotkeyField({
  inputId: "card-hotkey-input", saveId: "card-hotkey-save", statusId: "card-hotkey-status",
  settingKey: "card_hotkey", resultKey: "card_hotkey",
  getSaved: () => savedCardHotkey, setSaved: (v) => (savedCardHotkey = v),
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
  hideShowHotkeyField.applySaved(s.hotkey);
  if (s.card_hotkey) cardHotkeyField.applySaved(s.card_hotkey);
  setTextModeButtons(s.text_mode);
  setCaptureModeButtons(s.capture_mode || "screen");
  currentEngine = s.ocr_engine || currentEngine;
  currentSpeed = s.ocr_speed || currentSpeed;
  setSpeedButtons(currentSpeed); // reflect the saved OCR speed in the dialog
}

// Load the translation backend (Qwen3-4B). Slow on first run (~4 GB of int8
// weights read off disk).
async function loadTranslator() {
  if (!hasApi()) return;
  trStatus.textContent = "● Qwen3-4B (loading…)";
  trStatus.title = "";
  let res;
  try {
    res = await window.pywebview.api.load_translator();
  } catch (e) {
    res = { ok: false, error: String(e) };
  }
  translatorReady = !!(res && res.ok);
  const backend = (res && res.backend) || "Qwen3-4B";
  trStatus.textContent = translatorReady ? `● ${backend}` : "● Qwen3-4B (unavailable)";
  if (translatorReady) {
    trStatus.title = "";
    return;
  }
  // Say WHY, like the OCR path does. The backend's message is actionable
  // ("Qwen model not found. Expected … at models/qwen …"), and showing only
  // "unavailable" threw it away — leaving a missing model looking like a bug.
  const msg = (res && res.error) || "failed to load";
  trStatus.title = msg;          // full text on hover, survives later OCR output
  setDetected(`${backend} unavailable — ${msg}`, true);
}

// Load the OCR engine's weights. RapidOCR is the only engine (no picker), so this
// runs once at startup; the pipeline stage still loads via the backend Api.
async function selectEngine(name) {
  if (!hasApi()) return;
  currentEngine = name;
  engineReady = false;
  setOcrStatus(`${name} (loading…)`);

  let res;
  try {
    res = await window.pywebview.api.set_ocr_engine(name);
  } catch (e) {
    res = { ok: false, error: String(e) };
  }

  if (res && res.ok) {
    engineReady = true;
    // The backend may have fallen back to a different engine (e.g. a saved config
    // named a removed engine); reflect the engine that actually loaded.
    currentEngine = res.engine || name;
    setOcrStatus(currentEngine);
    notifyCaptureChanged(); // refresh the Create-card status badge with the engine
  } else {
    engineReady = false;
    setOcrStatus(`${name} (unavailable)`);
    const msg = (res && res.error) || "failed to load";
    setDetected(`${name} unavailable — ${msg}`, true);
  }
}

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
  const areaMode = activeCaptureMode() === "area";
  if (!hwnd && !areaMode) return; // screen mode needs a window; area mode doesn't
  ocrBusy = true;
  try {
    const res = await window.pywebview.api.process_frame(hwnd || 0);
    if (!capturing) return; // stopped while we were awaiting
    if (res && res.unchanged) {
      // Screen (or its text) didn't change — the backend skipped OCR/translation.
      // Keep the current detected/translated text and overlay; just refresh the
      // preview if a fresh frame came back (a cutscene behind steady text).
      if (res.frame) setPreview(res.frame);
      return;
    }
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

// About page: open the bundled third-party licenses file in the OS default app.
const licensesLink = document.getElementById("open-licenses-link");
if (licensesLink) {
  licensesLink.addEventListener("click", async (e) => {
    e.preventDefault();
    if (!hasApi()) return;
    const res = await window.pywebview.api.open_licenses();
    if (res && res.ok === false) licensesLink.title = res.error || "Couldn't open the file.";
  });
}

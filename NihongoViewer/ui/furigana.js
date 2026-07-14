// ---- Furigana: kana readings over kanji for the card views ------------------
// Renders read-only ruby (振り仮名) previews under the Japanese Word / Sentence
// fields in both Create card and My card, using the offline analyzer exposed as
// window.pywebview.api.furigana. One show/hide toggle — persisted as the
// `furigana_show` setting — governs every preview at once.
//
// Loads AFTER app.js so it augments the shared window.NihongoViewer object
// (app.js assigns it wholesale) rather than being clobbered by it.

(() => {
  const api = () => (window.pywebview && window.pywebview.api) || null;

  let show = true;           // mirrors the persisted `furigana_show` setting
  const cache = new Map();   // source text -> segments (skip repeat backend calls)
  const previews = [];       // [{srcEl, previewEl}] registered field/preview pairs
  const toggles = [];        // toggle buttons kept in sync with `show`

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // Build ruby HTML and report whether any reading was actually placed — a
  // preview with no ruby would just echo the source line, so we hide it.
  function segmentsToHtml(segments) {
    let hasRuby = false;
    const html = segments
      .map((seg) => {
        if (seg.ruby) {
          hasRuby = true;
          return `<ruby>${esc(seg.base)}<rt>${esc(seg.ruby)}</rt></ruby>`;
        }
        return esc(seg.base);
      })
      .join("");
    return { html, hasRuby };
  }

  async function segmentsFor(text) {
    if (cache.has(text)) return cache.get(text);
    let segments = null;
    if (api()) {
      try {
        const res = await api().furigana(text);
        if (res && res.ok) segments = res.segments || [];
      } catch (e) {
        /* no readings available — leave segments null */
      }
    }
    if (segments) cache.set(text, segments);
    return segments;
  }

  function hide(previewEl) {
    previewEl.hidden = true;
    previewEl.innerHTML = "";
  }

  // Render the reading for `text` into `previewEl`, hiding it when furigana is
  // off, the field is empty, the backend is unavailable, or there are no kanji.
  async function renderInto(previewEl, text) {
    text = (text || "").trim();
    if (!show || !text) {
      hide(previewEl);
      return;
    }
    const segments = await segmentsFor(text);
    if (!segments) {
      hide(previewEl);
      return;
    }
    const { html, hasRuby } = segmentsToHtml(segments);
    if (!hasRuby) {
      hide(previewEl);
      return;
    }
    previewEl.innerHTML = html;
    previewEl.hidden = false;
  }

  // Register a source field (input/textarea) + its preview element. The preview
  // re-renders (debounced) as the user types; programmatic value changes call
  // refresh() explicitly.
  function attach(srcEl, previewEl) {
    if (!srcEl || !previewEl) return;
    const entry = { srcEl, previewEl };
    previews.push(entry);
    let timer = null;
    srcEl.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => renderInto(previewEl, srcEl.value), 250);
    });
  }

  // Re-render one field now (after capture/translate/edit set its value).
  function refresh(srcEl) {
    const entry = previews.find((p) => p.srcEl === srcEl);
    if (entry) renderInto(entry.previewEl, entry.srcEl.value);
  }

  function refreshAll() {
    previews.forEach((p) => renderInto(p.previewEl, p.srcEl.value));
  }

  function syncToggles() {
    toggles.forEach((btn) => {
      btn.classList.toggle("active", show);
      btn.setAttribute("aria-pressed", show ? "true" : "false");
    });
  }

  function setShow(next, persist = true) {
    show = !!next;
    syncToggles();
    refreshAll();
    if (persist && api()) {
      try {
        api().update_settings({ furigana_show: show });
      } catch (e) {
        /* setting is cosmetic — ignore a failed persist */
      }
    }
  }

  function registerToggle(btn) {
    if (!btn) return;
    toggles.push(btn);
    btn.addEventListener("click", () => setShow(!show));
  }

  // Wire the known field/preview pairs and the toggles (elements exist because
  // this script is the last one in <body>).
  const byId = (id) => document.getElementById(id);
  attach(byId("card-word"), byId("card-word-furi"));
  attach(byId("card-sentence"), byId("card-sentence-furi"));
  attach(byId("detail-word"), byId("detail-word-furi"));
  attach(byId("detail-sentence"), byId("detail-sentence-furi"));
  registerToggle(byId("furigana-toggle-create"));
  registerToggle(byId("furigana-toggle-detail"));
  syncToggles();

  window.NihongoViewer = window.NihongoViewer || {};
  window.NihongoViewer.furigana = { attach, refresh, refreshAll, setShow, isShown: () => show };

  // Pull the saved show/hide state once the backend is ready, then repaint.
  window.addEventListener("pywebviewready", async () => {
    if (!api()) return;
    try {
      const s = await api().get_settings();
      if (s && typeof s.furigana_show === "boolean") show = s.furigana_show;
    } catch (e) {
      /* keep the default (on) */
    }
    syncToggles();
    refreshAll();
  });
})();

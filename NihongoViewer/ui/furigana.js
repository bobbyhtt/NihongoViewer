// ---- Read Mode: furigana + hover-to-lookup dictionary -----------------------
// The green "Read Mode" toggle governs two things at once (persisted as the
// `furigana_show` setting):
//
//   * a read-only ruby (振り仮名) reading shown under the Word / Sentence fields
//     in both Create card and My card, via window.pywebview.api.furigana; and
//   * on the *sentence* previews, an interactive layer: every word is a hover
//     target that pops up its dictionary entry (reading, part of speech, English
//     senses) from the offline JMdict index (api.read_tokens + api.lookup). On
//     the Create-card sentence, clicking a word fills the Word / Translated-word
//     fields from the dictionary — the "deep dive" half of capture -> card.
//
// Loads AFTER app.js so it augments the shared window.NihongoViewer object
// (app.js assigns it wholesale) rather than being clobbered by it.

(() => {
  const api = () => (window.pywebview && window.pywebview.api) || null;

  let show = true;              // mirrors the persisted `furigana_show` setting
  const rubyCache = new Map();  // text -> annotate segments (non-interactive)
  const tokenCache = new Map(); // text -> read_tokens tokens (interactive)
  const lookupCache = new Map();// "q\x1fr\x1fpos" -> lookup entries
  const previews = [];          // [{srcEl, previewEl, interactive, fill}]
  const toggles = [];           // toggle buttons kept in sync with `show`

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // Ruby HTML for a token's/segment's pieces; reports whether any ruby was placed.
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

  // ---- non-interactive furigana (Word previews) -----------------------------
  async function segmentsFor(text) {
    if (rubyCache.has(text)) return rubyCache.get(text);
    let segments = null;
    if (api()) {
      try {
        const res = await api().furigana(text);
        if (res && res.ok) segments = res.segments || [];
      } catch (e) {
        /* no readings available — leave segments null */
      }
    }
    if (segments) rubyCache.set(text, segments);
    return segments;
  }

  // ---- interactive tokens (Sentence previews) -------------------------------
  async function tokensFor(text) {
    if (tokenCache.has(text)) return tokenCache.get(text);
    let tokens = null;
    if (api() && api().read_tokens) {
      try {
        const res = await api().read_tokens(text);
        if (res && res.ok) tokens = res.tokens || [];
      } catch (e) {
        /* analyzer unavailable — leave tokens null */
      }
    }
    if (tokens) tokenCache.set(text, tokens);
    return tokens;
  }

  // Build the interactive sentence HTML: each lookupable word is a .rt-word span
  // carrying its dictionary query/reading/pos; punctuation stays plain. Reports
  // whether anything worth showing (a reading or a hoverable word) is present.
  function tokensToHtml(tokens) {
    let hasRuby = false;
    let hasWord = false;
    const html = tokens
      .map((tok) => {
        const { html: inner, hasRuby: r } = segmentsToHtml(tok.segments || []);
        if (r) hasRuby = true;
        if (!tok.lookup) return inner;
        hasWord = true;
        return `<span class="rt-word" tabindex="0"`
          + ` data-q="${esc(tok.query || tok.surface || "")}"`
          + ` data-r="${esc(tok.reading || "")}"`
          + ` data-pos="${esc(tok.pos || "")}"`
          + ` data-surface="${esc(tok.surface || "")}">${inner}</span>`;
      })
      .join("");
    return { html, show: hasRuby || hasWord };
  }

  function hide(previewEl) {
    previewEl.hidden = true;
    previewEl.innerHTML = "";
  }

  // Render `text` into `previewEl`. Interactive previews get hoverable word
  // spans; plain previews get ruby only. Hidden when Read Mode is off, the field
  // is empty, the backend is unavailable, or there's nothing to show.
  async function renderInto(entry, text) {
    const { previewEl, interactive } = entry;
    text = (text || "").trim();
    if (!show || !text) {
      hide(previewEl);
      return;
    }
    if (interactive) {
      const tokens = await tokensFor(text);
      if (!tokens) {
        hide(previewEl);
        return;
      }
      const { html, show: visible } = tokensToHtml(tokens);
      if (!visible) {
        hide(previewEl);
        return;
      }
      previewEl.innerHTML = html;
      previewEl.hidden = false;
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

  // ---- the hover popup ------------------------------------------------------
  const popup = document.getElementById("rt-popup");
  let hoverEl = null;      // the .rt-word currently driving the popup
  let overPopup = false;   // pointer is inside the popup (don't hide yet)
  let showTimer = null;
  let hideTimer = null;
  let lookupSeq = 0;       // guards against a stale async lookup rendering

  function popupInner(entries, attribution, fillable) {
    if (!entries.length) {
      return '<div class="rt-empty">No dictionary entry</div>'
        + `<div class="rt-attr">${esc(attribution || "JMdict · EDRDG · CC BY-SA")}</div>`;
    }
    const blocks = entries.map((e, ei) => {
      const head = `<div class="rt-head">`
        + (e.kanji ? `<span class="rt-kanji">${esc(e.kanji)}</span>` : "")
        + (e.reading ? `<span class="rt-reading">${esc(e.reading)}</span>` : "")
        + (e.common ? `<span class="rt-common">common</span>` : "")
        + `</div>`;
      const senses = (e.senses || []).slice(0, 6).map((s) => {
        const pos = (s.pos || []).length
          ? `<span class="rt-pos">${esc(s.pos.join(", "))}</span>` : "";
        return `<li>${pos}${esc((s.glosses || []).join("; "))}</li>`;
      }).join("");
      const fill = fillable
        ? `<button class="rt-fill" type="button" data-ei="${ei}">＋ Use as Word</button>`
        : "";
      return `<div class="rt-entry">${head}<ol class="rt-senses">${senses}</ol>${fill}</div>`;
    }).join("");
    return blocks + `<div class="rt-attr">${esc(attribution || "JMdict · EDRDG · CC BY-SA")}</div>`;
  }

  function positionPopup(wordEl) {
    // Fixed-position card just below the word, clamped to the viewport.
    popup.style.visibility = "hidden";
    popup.hidden = false;
    const r = wordEl.getBoundingClientRect();
    const pw = popup.offsetWidth;
    const ph = popup.offsetHeight;
    const margin = 8;
    let left = r.left;
    if (left + pw > window.innerWidth - margin) left = window.innerWidth - pw - margin;
    if (left < margin) left = margin;
    let top = r.bottom + 6;
    if (top + ph > window.innerHeight - margin) top = r.top - ph - 6; // flip above
    if (top < margin) top = margin;
    popup.style.left = `${Math.round(left)}px`;
    popup.style.top = `${Math.round(top)}px`;
    popup.style.visibility = "visible";
  }

  async function openPopup(wordEl) {
    const q = wordEl.dataset.q || wordEl.dataset.surface || "";
    if (!q) return;
    const r = wordEl.dataset.r || "";
    const pos = wordEl.dataset.pos || "";
    const fillTarget = activeFillTarget(wordEl);
    const fillable = !!fillTarget;
    const seq = ++lookupSeq;
    const key = `${q}\x1f${r}\x1f${pos}`;

    let entries = lookupCache.get(key);
    let attribution = "JMdict · EDRDG · CC BY-SA 4.0";
    if (!entries) {
      if (!api() || !api().lookup) return;
      let res;
      try {
        res = await api().lookup(q, r, pos);
      } catch (e) {
        return;
      }
      if (seq !== lookupSeq) return; // a newer hover superseded this one
      if (res && res.loading) {
        renderPopup(wordEl, '<div class="rt-empty">Building dictionary… try again in a moment</div>');
        return;
      }
      if (!res || !res.ok) {
        const msg = (res && res.error) || "Lookup unavailable";
        renderPopup(wordEl, `<div class="rt-empty">${esc(msg)}</div>`);
        return;
      }
      entries = res.entries || [];
      attribution = res.attribution || attribution;
      lookupCache.set(key, entries);
      lookupCache.set(key + "\x1fattr", attribution);
    } else {
      attribution = lookupCache.get(key + "\x1fattr") || attribution;
    }
    if (seq !== lookupSeq) return;
    // Stash the entries on the popup so a Fill click can read them back.
    popup._entries = entries;
    popup._fillTarget = fillTarget;
    renderPopup(wordEl, popupInner(entries, attribution, fillable));
  }

  function renderPopup(wordEl, innerHtml) {
    popup.innerHTML = innerHtml;
    positionPopup(wordEl);
  }

  function closePopup() {
    popup.hidden = true;
    popup.innerHTML = "";
    popup._entries = null;
    popup._fillTarget = null;
    hoverEl = null;
  }

  function scheduleOpen(wordEl) {
    clearTimeout(hideTimer);
    clearTimeout(showTimer);
    hoverEl = wordEl;
    showTimer = setTimeout(() => openPopup(wordEl), 110);
  }

  function scheduleClose() {
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      if (!overPopup) closePopup();
    }, 160);
  }

  // The fill-target for a hovered word: the preview's [data-rt-fill] wrapper, but
  // only when its Word field is currently editable. That's always true on Create
  // card, and only true in Edit mode on My card (where the field is `readonly`
  // while reading) — so the "Use as Word" button appears exactly when it can act.
  function activeFillTarget(el) {
    const target = el.closest("[data-rt-fill]");
    if (!target) return null;
    const wordEl = document.getElementById(target.dataset.rtWord);
    if (!wordEl || wordEl.hasAttribute("readonly") || wordEl.disabled) return null;
    return target;
  }

  // Fill a Word / Translated-word field pair from a dictionary entry.
  function fillWord(fillTarget, entry) {
    if (!fillTarget || !entry) return;
    const wordEl = document.getElementById(fillTarget.dataset.rtWord);
    const trEl = document.getElementById(fillTarget.dataset.rtWordTr);
    if (wordEl) {
      wordEl.value = entry.kanji || entry.reading || "";
      // Fire input so readiness + the word's own furigana preview refresh.
      wordEl.dispatchEvent(new Event("input", { bubbles: true }));
    }
    if (trEl) {
      const first = (entry.senses && entry.senses[0] && entry.senses[0].glosses) || [];
      trEl.value = first[0] || "";
      trEl.dispatchEvent(new Event("input", { bubbles: true }));
    }
    closePopup();
  }

  // Wire the popup's own hover (so moving into it doesn't dismiss) and Fill click.
  if (popup) {
    popup.addEventListener("mouseenter", () => { overPopup = true; });
    popup.addEventListener("mouseleave", () => { overPopup = false; scheduleClose(); });
    popup.addEventListener("click", (e) => {
      const btn = e.target.closest(".rt-fill");
      if (!btn) return;
      const ei = Number(btn.dataset.ei);
      const entries = popup._entries || [];
      fillWord(popup._fillTarget, entries[ei]);
    });
  }

  // Delegated hover/click on an interactive preview's word spans.
  function wireInteractive(previewEl) {
    previewEl.addEventListener("mouseover", (e) => {
      const w = e.target.closest(".rt-word");
      if (w && w !== hoverEl) scheduleOpen(w);
    });
    previewEl.addEventListener("mouseout", (e) => {
      const w = e.target.closest(".rt-word");
      if (w) scheduleClose();
    });
    // Keyboard focus (tab) opens the popup too, for accessibility.
    previewEl.addEventListener("focusin", (e) => {
      const w = e.target.closest(".rt-word");
      if (w) scheduleOpen(w);
    });
    previewEl.addEventListener("focusout", scheduleClose);
    // Click a word to (re)open its popup for reading — useful for touch/keyboard
    // or after the hover popup was dismissed. Filling the form happens ONLY via
    // the popup's "Use as Word" buttons, so a word click never populates fields.
    previewEl.addEventListener("click", (e) => {
      const w = e.target.closest(".rt-word");
      if (w) openPopup(w);
    });
  }

  // ---- registration ---------------------------------------------------------
  function attach(srcEl, previewEl, opts = {}) {
    if (!srcEl || !previewEl) return;
    const entry = { srcEl, previewEl, interactive: !!opts.interactive };
    previews.push(entry);
    if (entry.interactive) wireInteractive(previewEl);
    let timer = null;
    srcEl.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => renderInto(entry, srcEl.value), 250);
    });
  }

  function entryFor(srcEl) {
    return previews.find((p) => p.srcEl === srcEl);
  }

  // Re-render one field now (after capture/translate/edit set its value).
  function refresh(srcEl) {
    const entry = entryFor(srcEl);
    if (entry) renderInto(entry, entry.srcEl.value);
  }

  function refreshAll() {
    previews.forEach((entry) => renderInto(entry, entry.srcEl.value));
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
    if (!show) closePopup();
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

  // Wire the field/preview pairs and the toggles. The two *sentence* previews are
  // interactive (hover lookup); the Create-card one also fills the Word fields on
  // click — flagged by the data-rt-fill attributes on its wrapper below.
  const byId = (id) => document.getElementById(id);
  attach(byId("card-word"), byId("card-word-furi"));
  attach(byId("card-sentence"), byId("card-sentence-furi"), { interactive: true });
  attach(byId("detail-word"), byId("detail-word-furi"));
  attach(byId("detail-sentence"), byId("detail-sentence-furi"), { interactive: true });
  registerToggle(byId("furigana-toggle-create"));
  registerToggle(byId("furigana-toggle-detail"));
  syncToggles();

  // Mark both sentence previews as able to fill their Word / Translated-word
  // fields from the dictionary. The "Use as Word" button only actually appears
  // when that Word field is editable — always on Create card, and only in Edit
  // mode on My card (its fields are `readonly` while reading). See activeFillTarget.
  function markFillable(previewId, wordId, wordTrId) {
    const el = byId(previewId);
    if (!el) return;
    el.setAttribute("data-rt-fill", "");
    el.setAttribute("data-rt-word", wordId);
    el.setAttribute("data-rt-word-tr", wordTrId);
  }
  markFillable("card-sentence-furi", "card-word", "card-word-tr");
  markFillable("detail-sentence-furi", "detail-word", "detail-word-tr");

  window.NihongoViewer = window.NihongoViewer || {};
  window.NihongoViewer.furigana = { attach, refresh, refreshAll, setShow, isShown: () => show };

  // Hide the popup when the *page* scrolls (it's fixed-position, so it would
  // float orphaned) — but NOT when the user scrolls inside the popup itself to
  // read a long entry (that scroll event's target is the popup). Same guard
  // covers dragging the popup's own scrollbar.
  window.addEventListener("scroll", (e) => {
    if (e.target === popup || (popup && popup.contains(e.target))) return;
    closePopup();
  }, true);
  window.addEventListener("resize", closePopup);

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

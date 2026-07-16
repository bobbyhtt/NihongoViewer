// ---- Create Card + My Card: decks and cards ---------------------------------
// Everything for the flashcard feature: deck management (shared by the
// Create-card dropdown and the My-card list), capture-to-card, per-field
// translation, saving a card, and browsing/reading saved cards in a deck.

(() => {
  const api = () => (window.pywebview && window.pywebview.api) || null;

  // Re-render a field's furigana preview after a programmatic value change
  // (capture, translate, open card). Typing is handled by furigana.js itself.
  function refreshFuri(el) {
    const f = window.NihongoViewer && window.NihongoViewer.furigana;
    if (f && el) f.refresh(el);
  }

  const deckSelect = document.getElementById("create-deck-select");
  const deckListEl = document.getElementById("deck-list");

  // Last decks we rendered ([{name, count}]), so the no-backend path (plain
  // browser preview) can update the list locally without a round-trip.
  let deckState = [];

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // The always-present default deck (mirrors decks.DEFAULT_DECK) — can't be deleted.
  const DEFAULT_DECK = "MyDeck";

  // --- Confirm dialog (deletion) ---
  const confirmModal = document.getElementById("confirm-modal");
  const confirmTitle = document.getElementById("confirm-title");
  const confirmMsg = document.getElementById("confirm-message");
  const confirmOk = document.getElementById("confirm-ok");
  const confirmCancel = document.getElementById("confirm-cancel");

  // Show the confirm modal; resolves true (confirmed) or false (cancelled).
  function askConfirm(title, message, okLabel = "Delete") {
    return new Promise((resolve) => {
      confirmTitle.textContent = title;
      confirmMsg.textContent = message;
      confirmOk.textContent = okLabel;
      confirmModal.hidden = false;
      const done = (val) => {
        confirmModal.hidden = true;
        confirmOk.onclick = confirmCancel.onclick = confirmModal.onclick = null;
        resolve(val);
      };
      confirmOk.onclick = () => done(true);
      confirmCancel.onclick = () => done(false);
      confirmModal.onclick = (e) => {
        if (e.target === confirmModal) done(false); // click backdrop = cancel
      };
    });
  }

  // --- Deck grid rendering ---
  let deckFilter = "";

  // Deterministic cover for a deck with no card images (colour keyed on name).
  function deckGradient(name) {
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
    return `linear-gradient(135deg, hsl(${h} 30% 26%), hsl(${(h + 40) % 360} 30% 18%))`;
  }

  function coverHtml(d) {
    if (d.covers && d.covers.length) {
      const slots = [0, 1, 2].map((i) =>
        d.covers[i]
          ? `<i style="background-image:url('${d.covers[i]}')"></i>`
          : '<i style="background:#161a20"></i>');
      return `<div class="deck-cover">${slots.join("")}</div>`;
    }
    return `<div class="deck-cover solo"><i style="background:${deckGradient(d.name)}"></i></div>`;
  }

  function deckCardHtml(d) {
    const plural = d.count === 1 ? "" : "s";
    const exportDisabled = d.count === 0 ? " disabled" : "";
    const isDefault = d.name.toLowerCase() === DEFAULT_DECK.toLowerCase();
    const menu = isDefault
      ? "" // the default deck can't be deleted, so it gets no ⋯ menu
      : '<button class="btn sm deck-menu-btn" type="button" aria-label="More actions">⋯</button>';
    return `
      <div class="deck-card" data-deck="${esc(d.name)}">
        ${coverHtml(d)}
        <div class="deck-foot">
          <div class="deck-meta">
            <span class="deck-name">${esc(d.name)}</span>
            <span class="deck-count">${d.count} card${plural}</span>
          </div>
          <div class="deck-actions">
            <button class="btn sm export-btn"${exportDisabled}>
              <svg viewBox="0 0 24 24" class="btn-ico" aria-hidden="true">
                <path d="M12 4v10M8 11l4 4 4-4M5 20h14"/>
              </svg>
              Export
            </button>
            ${menu}
          </div>
        </div>
      </div>`;
  }

  function renderDeckGrid() {
    const q = deckFilter.trim().toLowerCase();
    const shown = q ? deckState.filter((d) => d.name.toLowerCase().includes(q)) : deckState;
    if (!shown.length) {
      deckListEl.innerHTML = q
        ? '<div class="empty-state">No decks match your search.</div>'
        : '<div class="empty-state">No decks yet.</div>';
      return;
    }
    let html = shown.map(deckCardHtml).join("");
    if (!q) {
      html += '<button class="deck-new" id="deck-new-tile" type="button">' +
              '<span class="plus">＋</span> New deck</button>';
    }
    deckListEl.innerHTML = html;
  }

  function renderDecks(decks) {
    deckState = decks;

    // Dropdown (Create card) — keep the current selection if it still exists.
    const prev = deckSelect.value;
    deckSelect.innerHTML = decks
      .map((d) => {
        const n = `${d.count} card${d.count === 1 ? "" : "s"}`;
        return `<option value="${esc(d.name)}">${esc(d.name)} · ${n}</option>`;
      })
      .join("");
    if (decks.some((d) => d.name === prev)) deckSelect.value = prev;

    // Deck grid (My card).
    renderDeckGrid();
  }

  document.getElementById("deck-search").addEventListener("input", (e) => {
    deckFilter = e.target.value;
    renderDeckGrid();
  });

  async function loadDecks() {
    let decks = [{ name: "MyDeck", count: 0 }]; // fallback for a plain browser
    if (api()) {
      try {
        const res = await api().list_decks();
        if (Array.isArray(res) && res.length) decks = res;
      } catch (e) {
        /* keep the fallback */
      }
    }
    renderDecks(decks);
  }

  // --- New deck dialog ---
  const deckModal = document.getElementById("deck-modal");
  const deckNameInput = document.getElementById("deck-name-input");
  const deckNameError = document.getElementById("deck-name-error");

  function openDeckModal() {
    deckNameInput.value = "";
    deckNameError.textContent = "";
    deckModal.hidden = false;
    deckNameInput.focus();
  }
  function closeDeckModal() {
    deckModal.hidden = true;
  }

  async function createDeck() {
    const name = deckNameInput.value.trim();
    if (!name) {
      deckNameError.textContent = "Deck name can't be empty.";
      return;
    }

    if (!api()) {
      // No backend (plain browser): update the list locally.
      if (deckState.some((d) => d.name.toLowerCase() === name.toLowerCase())) {
        deckNameError.textContent = `A deck named "${name}" already exists.`;
        return;
      }
      renderDecks([...deckState, { name, count: 0 }]);
      deckSelect.value = name;
      closeDeckModal();
      return;
    }

    let res;
    try {
      res = await api().create_deck(name);
    } catch (e) {
      res = { ok: false, error: String(e) };
    }
    if (!res || !res.ok) {
      deckNameError.textContent = (res && res.error) || "Could not create the deck.";
      return;
    }
    renderDecks(res.decks);
    deckSelect.value = name; // select the new deck for the next card
    closeDeckModal();
  }

  document.getElementById("new-deck-btn").addEventListener("click", openDeckModal);
  document.getElementById("deck-cancel-btn").addEventListener("click", closeDeckModal);
  document.getElementById("deck-create-btn").addEventListener("click", createDeck);
  // Click the dim backdrop to dismiss; Enter submits, Escape cancels.
  deckModal.addEventListener("click", (e) => {
    if (e.target === deckModal) closeDeckModal();
  });
  deckNameInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      createDeck();
    } else if (e.key === "Escape") {
      closeDeckModal();
    }
  });
  deckNameInput.addEventListener("input", () => (deckNameError.textContent = ""));

  // --- Capture into the card form ---
  const captureBtn = document.getElementById("create-capture-btn");
  const captureHint = document.getElementById("capture-hint");
  const mediaBox = document.getElementById("create-media-box");
  const wordEl = document.getElementById("card-word");
  const wordTrEl = document.getElementById("card-word-tr");
  const sentenceEl = document.getElementById("card-sentence");
  const sentenceTrEl = document.getElementById("card-sentence-tr");
  const noteEl = document.getElementById("card-note");
  const saveBtn = document.getElementById("card-save-btn");

  // The empty media-box placeholder, restored on Clear.
  const MEDIA_PLACEHOLDER = mediaBox.innerHTML;

  const saveHint = document.getElementById("save-hint");

  // Transient status text in a .field-hint element (auto-clears after 4s).
  function showHint(el, text, kind = "") {
    el.textContent = text;
    el.className = "field-hint" + (kind ? " " + kind : "");
    clearTimeout(el._hintTimer);
    if (text) el._hintTimer = setTimeout(() => (el.textContent = ""), 4000);
  }
  const flashHint = (text, kind) => showHint(captureHint, text, kind); // capture/translate

  // The save-row hint does double duty: a persistent "readiness" note when the
  // card can't be saved yet, and a transient result message after a save.
  let saveMsgTimer = null;
  function refreshReady() {
    const ready = !!(wordEl.value.trim() || sentenceEl.value.trim());
    saveBtn.disabled = !ready;
    return ready;
  }
  function setReadinessHint() {
    const ready = refreshReady();
    if (saveMsgTimer) return; // a transient message is showing — don't stomp it
    saveHint.className = "field-hint";
    saveHint.textContent = ready ? "" : "Needs a word or sentence";
  }
  function flashSave(text, kind) {
    clearTimeout(saveMsgTimer);
    refreshReady();
    saveHint.textContent = text;
    saveHint.className = "field-hint" + (kind ? " " + kind : "");
    saveMsgTimer = setTimeout(() => {
      saveMsgTimer = null;
      setReadinessHint();
    }, 4000);
  }

  function setMediaImage(dataUrl) {
    mediaBox.innerHTML = '<img class="media-img" alt="Captured frame" title="Click to expand" />';
    mediaBox.querySelector("img").src = dataUrl;
  }

  // --- Expand / retract the captured image ---
  const lightbox = document.getElementById("img-lightbox");
  const lightboxImg = document.getElementById("lightbox-img");

  function openLightbox(src) {
    lightboxImg.src = src;
    lightbox.hidden = false;
  }
  function closeLightbox() {
    lightbox.hidden = true;
    lightboxImg.removeAttribute("src");
  }

  // Click any captured/card image (create form or card detail) to expand it.
  document.addEventListener("click", (e) => {
    const img = e.target.closest(".media-img");
    if (img) openLightbox(img.src);
  });
  // Click anywhere on the expanded view — or press Escape — to retract.
  lightbox.addEventListener("click", closeLightbox);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !lightbox.hidden) closeLightbox();
  });

  captureBtn.addEventListener("click", async () => {
    const nv = window.NihongoViewer;
    // Capture only works while the screen-capture loop is running.
    if (!nv || !nv.isCapturing()) {
      flashHint("Start screen capture first (Capture tab).", "err");
      return;
    }
    const last = nv.lastCapture;
    if (!last || !last.frame) {
      flashHint("No frame yet — give the capture a moment.", "err");
      return;
    }
    // Text comes from the last OCR tick; grab a higher-res still (~960px) for
    // the card image so the expanded view is crisp, falling back to the ~640px
    // preview frame if the backend call isn't available.
    let frame = last.frame;
    if (api()) {
      try {
        const res = await api().capture_card_image();
        if (res && res.ok && res.frame) frame = res.frame;
      } catch (e) {
        /* keep the preview-resolution frame */
      }
    }
    setMediaImage(frame);
    if (last.ja) sentenceEl.value = last.ja;
    if (last.en) sentenceTrEl.value = last.en;
    refreshFuri(sentenceEl); // update the reading preview for the captured line
    setReadinessHint();
    flashHint("Captured the current frame.", "ok");
  });

  // --- Capture stack (fed by the global Create-card capture hotkey) ---
  // Each hotkey press snapshots the current frame + its OCR/translation into a
  // small ring (cap 20). The user flips through them here and loads one into the
  // form to save as a card. Transient — not persisted across launches.
  const CAPTURE_STACK_MAX = 20;
  const captureStackEl = document.getElementById("capture-stack");
  const stackPosEl = document.getElementById("stack-pos");
  const stackPrevBtn = document.getElementById("stack-prev-btn");
  const stackNextBtn = document.getElementById("stack-next-btn");
  const stackDeleteBtn = document.getElementById("stack-delete-btn");
  const stackClearBtn = document.getElementById("stack-clear-btn");

  let captureStack = []; // [{frame, ja, en}] oldest → newest
  let stackIndex = -1; // currently-shown capture (index into captureStack)
  let stackLoadedIndex = -1; // which capture is reflected in the form right now

  function createPageActive() {
    return document.getElementById("page-create").classList.contains("active");
  }

  function renderStack() {
    const n = captureStack.length;
    captureStackEl.hidden = n === 0;
    if (!n) return;
    stackPosEl.textContent = `${stackIndex + 1} / ${n}`;
    stackPrevBtn.disabled = stackIndex <= 0;
    stackNextBtn.disabled = stackIndex >= n - 1;
  }

  // Load the capture at stackIndex into the form (image + sentence + translation).
  function loadStackIntoForm() {
    if (stackIndex < 0 || stackIndex >= captureStack.length) return;
    const cap = captureStack[stackIndex];
    if (cap.frame) setMediaImage(cap.frame);
    else mediaBox.innerHTML = MEDIA_PLACEHOLDER;
    sentenceEl.value = cap.ja || "";
    sentenceTrEl.value = cap.en || "";
    refreshFuri(sentenceEl);
    setReadinessHint();
    stackLoadedIndex = stackIndex;
  }

  // Snapshot the current frame + last OCR text into the stack. Mirrors the
  // manual "Capture frame" button, but appends to the stack instead of the form.
  async function captureToStack() {
    const nv = window.NihongoViewer;
    if (!nv || !nv.isCapturing()) {
      if (createPageActive()) flashHint("Start screen capture first (Capture tab).", "err");
      return;
    }
    const last = nv.lastCapture;
    if (!last || !last.frame) return; // no frame yet — silently skip
    // Prefer a higher-res still (~960px) for the card image, like the button.
    let frame = last.frame;
    if (api()) {
      try {
        const res = await api().capture_card_image();
        if (res && res.ok && res.frame) frame = res.frame;
      } catch (e) {
        /* keep the preview-resolution frame */
      }
    }
    captureStack.push({ frame, ja: last.ja || "", en: last.en || "" });
    if (captureStack.length > CAPTURE_STACK_MAX) captureStack.shift(); // drop oldest
    stackIndex = captureStack.length - 1; // jump to the newest
    renderStack();
    // If the user is on the Create-card page, show it right away; otherwise it
    // loads when they navigate there (see the nv:page-changed handler below).
    if (createPageActive()) loadStackIntoForm();
    flashHint(`Captured (${captureStack.length}/${CAPTURE_STACK_MAX}).`, "ok");
  }

  function showStackItem(index) {
    if (index < 0 || index >= captureStack.length) return;
    stackIndex = index;
    renderStack();
    loadStackIntoForm();
  }

  function deleteStackItem() {
    if (stackIndex < 0) return;
    captureStack.splice(stackIndex, 1);
    if (!captureStack.length) {
      stackIndex = stackLoadedIndex = -1;
      renderStack();
      return;
    }
    if (stackIndex >= captureStack.length) stackIndex = captureStack.length - 1;
    stackLoadedIndex = -1; // force a reload of the now-current item
    showStackItem(stackIndex);
  }

  function clearStack() {
    captureStack = [];
    stackIndex = stackLoadedIndex = -1;
    renderStack();
  }

  stackPrevBtn.addEventListener("click", () => showStackItem(stackIndex - 1));
  stackNextBtn.addEventListener("click", () => showStackItem(stackIndex + 1));
  stackDeleteBtn.addEventListener("click", deleteStackItem);
  stackClearBtn.addEventListener("click", clearStack);

  // Global capture hotkey → snapshot into the stack.
  document.addEventListener("nv:hotkey-capture", captureToStack);

  // When the Create-card page becomes visible, ensure the current capture is
  // loaded (a hotkey press while on another tab won't have filled the form yet).
  document.addEventListener("nv:page-changed", (e) => {
    if (!e.detail || e.detail.page !== "create") return;
    renderStack();
    if (stackIndex >= 0 && stackLoadedIndex !== stackIndex) loadStackIntoForm();
  });

  // --- Translate a field with MADLAD-400 (offline) ---
  // Re-translates the JA source (Word / Sentence) into its EN field, so a user
  // who edits the captured text can refresh the translation.
  // `word: true` uses the word-gloss path (translate_word), which avoids the
  // padded sentence MADLAD returns for a bare word ("学生" -> "Students are
  // students."). The Sentence field uses the plain sentence translator.
  async function translateField(srcEl, dstEl, btn, opts = {}) {
    const text = srcEl.value.trim();
    if (!text) {
      flashHint("Nothing to translate — fill in the Japanese first.", "err");
      return;
    }
    if (!api()) {
      flashHint("Translation needs the app running (MADLAD-400).", "err");
      return;
    }
    const labelEl = btn.querySelector(".mini-label");
    const label = labelEl.textContent;
    btn.disabled = true;
    labelEl.textContent = "Translating…";
    let res;
    try {
      res = opts.word
        ? await api().translate_word(text)
        : await api().translate_text(text);
    } catch (e) {
      res = { ok: false, error: String(e) };
    }
    btn.disabled = false;
    labelEl.textContent = label;
    if (!res || !res.ok) {
      flashHint((res && res.error) || "Translation failed.", "err");
      return;
    }
    dstEl.value = res.text || "";
  }

  document.getElementById("translate-word-btn").addEventListener("click", (e) =>
    translateField(
      document.getElementById("card-word"),
      document.getElementById("card-word-tr"),
      e.currentTarget,
      { word: true }
    ));
  document.getElementById("translate-sentence-btn").addEventListener("click", (e) =>
    translateField(
      sentenceEl,
      sentenceTrEl,
      e.currentTarget
    ));

  // --- Clear / collect the form ---
  const CARD_FIELD_IDS = ["card-word", "card-word-tr", "card-sentence", "card-sentence-tr", "card-note"];

  function clearForm() {
    CARD_FIELD_IDS.forEach((id) => (document.getElementById(id).value = ""));
    mediaBox.innerHTML = MEDIA_PLACEHOLDER;
    refreshFuri(wordEl); // cleared fields -> hide their reading previews
    refreshFuri(sentenceEl);
    flashHint("");
    setReadinessHint();
  }

  function collectCard() {
    const img = mediaBox.querySelector("img.media-img");
    return {
      word: document.getElementById("card-word").value.trim(),
      word_tr: document.getElementById("card-word-tr").value.trim(),
      sentence: sentenceEl.value.trim(),
      sentence_tr: sentenceTrEl.value.trim(),
      note: document.getElementById("card-note").value.trim(),
      image: img ? img.src : "",
    };
  }

  document.getElementById("card-clear-btn").addEventListener("click", clearForm);

  // --- Save the card to the selected deck ---
  saveBtn.addEventListener("click", async () => {
    const deck = deckSelect.value;
    const card = collectCard();
    if (!card.word && !card.sentence) {
      flashSave("Add a word or sentence before saving.", "err");
      return;
    }
    if (!api()) {
      flashSave("Saving needs the app running.", "err");
      return;
    }
    saveBtn.disabled = true;
    let res;
    try {
      res = await api().save_card(deck, card);
    } catch (e) {
      res = { ok: false, error: String(e) };
    }
    saveBtn.disabled = false;
    if (!res || !res.ok) {
      flashSave((res && res.error) || "Could not save the card.", "err");
      return;
    }
    renderDecks(res.decks); // refresh the deck card counts
    deckSelect.value = deck; // keep the same deck selected for the next card
    // If the saved-to deck is the one currently open in My card, refresh it.
    if (currentDeck && currentDeck.toLowerCase() === deck.toLowerCase()) {
      loadCards(currentDeck);
    }
    clearForm();
    flashSave(`Saved to “${deck}”.`, "ok");
  });

  // --- My card: browse a deck's cards, open one to read ---
  const cardListEl = document.getElementById("card-list");
  const detailMediaBox = document.getElementById("detail-media-box");
  const DETAIL_MEDIA_PLACEHOLDER = detailMediaBox.innerHTML;
  const prevBtn = document.getElementById("card-prev-btn");
  const nextBtn = document.getElementById("card-next-btn");
  const backBtn = document.getElementById("card-back-btn");
  const editBtn = document.getElementById("card-edit-btn");
  const deleteCardBtn = document.getElementById("card-delete-btn");
  const cancelBtn = document.getElementById("card-cancel-btn");
  const saveEditBtn = document.getElementById("card-save-edit-btn");
  const uploadBtn = document.getElementById("detail-upload-btn");
  const uploadNote = document.getElementById("upload-note");
  const uploadInput = document.getElementById("detail-upload-input");
  const detailHintEl = document.getElementById("detail-hint");
  const cardSearch = document.getElementById("card-search");
  const cardSortSel = document.getElementById("card-sort");
  const deckCountInline = document.getElementById("deck-count-inline");
  const cardPos = document.getElementById("card-pos");
  const cardMeta = document.getElementById("card-meta");
  const metaDeck = document.getElementById("meta-deck");
  const metaDate = document.getElementById("meta-date");

  const DETAIL_FIELD_IDS = ["detail-word", "detail-word-tr", "detail-sentence", "detail-sentence-tr", "detail-note"];
  const detailHint = (text, kind) => showHint(detailHintEl, text, kind);

  let currentDeck = null;
  let currentCards = []; // [{id, word, word_tr, sentence, thumb}] as stored (oldest→newest)
  let displayCards = []; // currentCards after search + sort (what's rendered)
  let currentIndex = -1; // index into displayCards
  let currentFullCard = null; // the full card currently shown/edited
  let cardFilter = "";
  let cardSort = "recent";
  let editing = false;
  let editImage = ""; // the (possibly newly uploaded) image while editing

  const CARD_PLACEHOLDER_ICO =
    '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/>' +
    '<circle cx="8.5" cy="9.5" r="1.6"/><path d="M4 18l5-5 4 4 3-3 4 4"/></svg>';

  function cardRowHtml(c, i) {
    const thumb = c.thumb
      ? `<div class="card-thumb" style="background-image:url('${c.thumb}')"></div>`
      : `<div class="card-thumb">${CARD_PLACEHOLDER_ICO}</div>`;
    const sent = c.sentence ? `<div class="card-sent">${esc(c.sentence)}</div>` : "";
    return `
      <div class="card-row" data-index="${i}">
        ${thumb}
        <div class="card-main">
          <div class="card-word-line">
            <span class="card-word">${esc(c.word || "—")}</span>
            <span class="card-word-tr">${esc(c.word_tr || "")}</span>
          </div>
          ${sent}
        </div>
        <span class="card-chevron">›</span>
      </div>`;
  }

  // Filter + sort currentCards into displayCards, then render the rows.
  function applyCardView() {
    const q = cardFilter.trim().toLowerCase();
    let list = currentCards.slice();
    if (q) {
      list = list.filter((c) =>
        (c.word || "").toLowerCase().includes(q) ||
        (c.word_tr || "").toLowerCase().includes(q) ||
        (c.reading || "").toLowerCase().includes(q) ||
        (c.sentence || "").toLowerCase().includes(q));
    }
    if (cardSort === "recent") list.reverse(); // stored oldest→newest
    else if (cardSort === "az") list.sort((a, b) => (a.word || "").localeCompare(b.word || "", "ja"));
    // "oldest" keeps stored order
    displayCards = list;

    if (!displayCards.length) {
      cardListEl.innerHTML = currentCards.length
        ? '<div class="empty-state">No cards match your search.</div>'
        : '<div class="empty-state">No cards yet.</div>';
      return;
    }
    cardListEl.innerHTML = displayCards.map(cardRowHtml).join("");
  }

  function updateDeckCount() {
    const n = currentCards.length;
    deckCountInline.textContent = `${n} card${n === 1 ? "" : "s"}`;
  }

  async function loadCards(name) {
    currentCards = [];
    if (api()) {
      try {
        currentCards = (await api().list_cards(name)) || [];
      } catch (e) {
        /* leave empty */
      }
    }
    updateDeckCount();
    applyCardView();
  }

  async function openDeck(name) {
    currentDeck = name;
    document.getElementById("deck-crumb").textContent = name;
    document.getElementById("card-crumb-deck").textContent = name;
    cardFilter = "";
    cardSearch.value = "";
    await loadCards(name);
    window.showMyCardView("deck");
  }

  cardSearch.addEventListener("input", (e) => {
    cardFilter = e.target.value;
    applyCardView();
  });
  cardSortSel.addEventListener("change", (e) => {
    cardSort = e.target.value;
    applyCardView();
  });
  document.getElementById("deck-export-btn").addEventListener("click", (e) => {
    if (currentDeck) exportDeck(currentDeck, e.currentTarget);
  });

  function setDetailImage(url) {
    if (url) {
      detailMediaBox.innerHTML =
        '<img class="media-img" alt="Card image" title="Click to expand" />';
      detailMediaBox.querySelector("img").src = url;
    } else {
      detailMediaBox.innerHTML = DETAIL_MEDIA_PLACEHOLDER;
    }
  }

  function formatDate(ts) {
    if (!ts) return "";
    try {
      return new Date(ts * 1000).toLocaleDateString(undefined, {
        year: "numeric", month: "short", day: "numeric",
      });
    } catch (e) {
      return "";
    }
  }

  function fillDetail(card) {
    currentFullCard = card;
    setEditing(false); // always open a card in read mode
    document.getElementById("card-crumb").textContent = card.word || "(card)";
    document.getElementById("detail-word").value = card.word || "";
    document.getElementById("detail-word-tr").value = card.word_tr || "";
    document.getElementById("detail-sentence").value = card.sentence || "";
    document.getElementById("detail-sentence-tr").value = card.sentence_tr || "";
    document.getElementById("detail-note").value = card.note || "";
    refreshFuri(document.getElementById("detail-word"));
    refreshFuri(document.getElementById("detail-sentence"));
    setDetailImage(card.image);
    // Position + metadata
    cardPos.textContent = `${currentIndex + 1} / ${displayCards.length}`;
    metaDeck.textContent = currentDeck || "";
    const added = formatDate(card.created);
    metaDate.textContent = added ? `Added ${added}` : "";
    prevBtn.disabled = currentIndex <= 0;
    nextBtn.disabled = currentIndex >= displayCards.length - 1;
  }

  // --- Edit mode ---
  function setEditing(on) {
    editing = on;
    DETAIL_FIELD_IDS.forEach((id) => {
      const el = document.getElementById(id);
      if (on) el.removeAttribute("readonly");
      else el.setAttribute("readonly", "");
    });
    // Read mode shows Prev/Next/Edit; edit mode shows Cancel/Save + upload.
    backBtn.hidden = on;
    prevBtn.hidden = on;
    nextBtn.hidden = on;
    cardPos.hidden = on;
    editBtn.hidden = on;
    deleteCardBtn.hidden = on;
    cancelBtn.hidden = !on;
    saveEditBtn.hidden = !on;
    uploadBtn.hidden = !on;
    uploadNote.hidden = !on;
    cardMeta.hidden = on; // read-only info; hide while editing
    detailMediaBox.classList.toggle("editing", on);
    if (!on) detailMediaBox.classList.remove("drop-active");
  }

  function collectDetail() {
    return {
      word: document.getElementById("detail-word").value.trim(),
      word_tr: document.getElementById("detail-word-tr").value.trim(),
      sentence: document.getElementById("detail-sentence").value.trim(),
      sentence_tr: document.getElementById("detail-sentence-tr").value.trim(),
      note: document.getElementById("detail-note").value.trim(),
      image: editImage || "",
    };
  }

  function enterEdit() {
    editImage = (currentFullCard && currentFullCard.image) || "";
    setEditing(true);
    detailHint("");
    document.getElementById("detail-word").focus();
  }

  function cancelEdit() {
    fillDetail(currentFullCard); // restores fields + image, back to read mode
    detailHint("");
  }

  async function saveEdit() {
    const card = collectDetail();
    if (!card.word && !card.sentence) {
      detailHint("Add a word or sentence before saving.", "err");
      return;
    }
    if (!api()) {
      detailHint("Saving needs the app running.", "err");
      return;
    }
    saveEditBtn.disabled = true;
    let res;
    try {
      res = await api().update_card(currentDeck, currentFullCard.id, card);
    } catch (e) {
      res = { ok: false, error: String(e) };
    }
    saveEditBtn.disabled = false;
    if (!res || !res.ok) {
      detailHint((res && res.error) || "Could not save the card.", "err");
      return;
    }
    currentFullCard = { ...currentFullCard, ...card };
    // Update the summary in currentCards (by id), then re-render the list and
    // re-point currentIndex at this card in the (possibly re-sorted) display.
    const summary = res.card || {
      id: currentFullCard.id,
      word: card.word,
      word_tr: card.word_tr,
      sentence: card.sentence,
    };
    const ci = currentCards.findIndex((c) => c.id === currentFullCard.id);
    if (ci >= 0) currentCards[ci] = summary;
    applyCardView();
    currentIndex = displayCards.findIndex((c) => c.id === currentFullCard.id);
    fillDetail(currentFullCard); // back to read mode with the saved values
    detailHint("Card updated.", "ok");
  }

  // Turn an uploaded/dropped/pasted image file into a ≤960px PNG data URL,
  // matching the resolution of captured card images.
  function fileToCardImage(file) {
    return new Promise((resolve, reject) => {
      if (!file || !file.type || !file.type.startsWith("image/")) {
        reject(new Error("not an image"));
        return;
      }
      const reader = new FileReader();
      reader.onerror = () => reject(new Error("read failed"));
      reader.onload = () => {
        const img = new Image();
        img.onerror = () => reject(new Error("bad image"));
        img.onload = () => {
          const maxW = 960;
          let { width, height } = img;
          if (width > maxW) {
            height = Math.round((height * maxW) / width);
            width = maxW;
          }
          const canvas = document.createElement("canvas");
          canvas.width = width;
          canvas.height = height;
          canvas.getContext("2d").drawImage(img, 0, 0, width, height);
          resolve(canvas.toDataURL("image/png"));
        };
        img.src = reader.result;
      };
      reader.readAsDataURL(file);
    });
  }

  async function useImageFile(file) {
    try {
      const url = await fileToCardImage(file);
      editImage = url;
      setDetailImage(url);
      detailHint("Image updated.", "ok");
    } catch (e) {
      detailHint("That file isn't a valid image.", "err");
    }
  }

  async function openCard(index) {
    if (index < 0 || index >= displayCards.length) return;
    currentIndex = index;
    const summary = displayCards[index];
    let card = summary;
    if (api()) {
      try {
        const full = await api().get_card(currentDeck, summary.id);
        if (full) card = full;
      } catch (e) {
        /* fall back to the summary */
      }
    }
    fillDetail(card);
    window.showMyCardView("card");
  }

  async function deleteDeck(name) {
    const ok = await askConfirm(
      "Delete deck",
      `Delete “${name}” and all its cards? This can’t be undone.`,
      "Delete deck"
    );
    if (!ok) return;
    if (!api()) {
      renderDecks(deckState.filter((d) => d.name !== name));
      return;
    }
    let res;
    try {
      res = await api().delete_deck(name);
    } catch (e) {
      res = { ok: false, error: String(e) };
    }
    if (!res || !res.ok) {
      alert((res && res.error) || "Could not delete the deck.");
      return;
    }
    renderDecks(res.decks);
  }

  async function exportDeck(name, btn) {
    if (!api()) {
      alert("Export needs the app running.");
      return;
    }
    btn.disabled = true; // guard against a double-click; the icon/label are kept
    let res;
    try {
      res = await api().export_deck(name);
    } catch (e) {
      res = { ok: false, error: String(e) };
    }
    btn.disabled = false;
    if (res && res.cancelled) return; // user closed the save dialog
    if (!res || !res.ok) {
      alert((res && res.error) || "Could not export the deck.");
      return;
    }
    const n = res.count;
    alert(`Exported ${n} card${n === 1 ? "" : "s"} to:\n${res.path}`);
  }

  // --- Deck "⋯" menu (delete) ---
  const deckMenu = document.getElementById("deck-menu");
  let menuDeck = null;

  function openDeckMenu(btn, name) {
    menuDeck = name;
    deckMenu.hidden = false; // unhide first so we can measure it
    const r = btn.getBoundingClientRect();
    let left = r.right - deckMenu.offsetWidth;
    if (left < 8) left = 8;
    deckMenu.style.left = `${left}px`;
    deckMenu.style.top = `${r.bottom + 4}px`;
  }
  function closeDeckMenu() {
    deckMenu.hidden = true;
    menuDeck = null;
  }
  document.getElementById("deck-menu-delete").addEventListener("click", () => {
    const name = menuDeck;
    closeDeckMenu();
    if (name) deleteDeck(name);
  });
  // Any click outside the menu (or Escape) closes it.
  document.addEventListener("click", (e) => {
    if (deckMenu.hidden) return;
    if (e.target.closest("#deck-menu") || e.target.closest(".deck-menu-btn")) return;
    closeDeckMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !deckMenu.hidden) closeDeckMenu();
  });

  // Deck grid -> new deck, ⋯ menu, export, or open a deck (delegated).
  deckListEl.addEventListener("click", (e) => {
    if (e.target.closest("#deck-new-tile")) {
      openDeckModal();
      return;
    }
    const menuBtn = e.target.closest(".deck-menu-btn");
    if (menuBtn) {
      e.stopPropagation(); // don't let the document handler close it immediately
      const dc = menuBtn.closest(".deck-card");
      openDeckMenu(menuBtn, dc && dc.dataset.deck);
      return;
    }
    const exportBtn = e.target.closest(".export-btn");
    if (exportBtn) {
      const dc = exportBtn.closest(".deck-card");
      if (dc) exportDeck(dc.dataset.deck, exportBtn);
      return;
    }
    const dc = e.target.closest(".deck-card");
    if (dc && dc.dataset.deck) openDeck(dc.dataset.deck);
  });

  // Card list -> open a card (delegated for the same reason).
  cardListEl.addEventListener("click", (e) => {
    const row = e.target.closest(".card-row");
    if (row) openCard(Number(row.dataset.index));
  });

  prevBtn.addEventListener("click", () => openCard(currentIndex - 1));
  nextBtn.addEventListener("click", () => openCard(currentIndex + 1));

  // Arrow keys page through cards while reading (not editing, not in a field).
  document.addEventListener("keydown", (e) => {
    const cardView = document.getElementById("view-card");
    if (cardView.offsetParent === null || editing) return; // not visible / editing
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (e.key === "ArrowLeft" && !prevBtn.disabled) openCard(currentIndex - 1);
    else if (e.key === "ArrowRight" && !nextBtn.disabled) openCard(currentIndex + 1);
  });

  async function deleteCurrentCard() {
    if (!currentFullCard) return;
    const ok = await askConfirm(
      "Delete card",
      "Delete this card? This can’t be undone.",
      "Delete card"
    );
    if (!ok) return;
    if (!api()) return;
    let res;
    try {
      res = await api().delete_card(currentDeck, currentFullCard.id);
    } catch (e) {
      res = { ok: false, error: String(e) };
    }
    if (!res || !res.ok) {
      detailHint((res && res.error) || "Could not delete the card.", "err");
      return;
    }
    renderDecks(res.decks); // refresh deck counts
    await loadCards(currentDeck); // refresh the card list
    window.showMyCardView("deck"); // back to the deck's card list
  }

  // Back to the decks list (from a deck's card list).
  document.getElementById("deck-back-btn")
    .addEventListener("click", () => window.showMyCardView("decks"));

  // Back to the deck's card list (from a card's detail).
  backBtn.addEventListener("click", () => window.showMyCardView("deck"));

  // Edit / Delete / Cancel / Save.
  editBtn.addEventListener("click", enterEdit);
  deleteCardBtn.addEventListener("click", deleteCurrentCard);
  cancelBtn.addEventListener("click", cancelEdit);
  saveEditBtn.addEventListener("click", saveEdit);

  // Image upload: file picker, drag & drop, and paste (only while editing).
  uploadBtn.addEventListener("click", () => uploadInput.click());
  uploadInput.addEventListener("change", () => {
    if (uploadInput.files[0]) useImageFile(uploadInput.files[0]);
    uploadInput.value = ""; // allow re-selecting the same file
  });
  detailMediaBox.addEventListener("dragover", (e) => {
    if (!editing) return;
    e.preventDefault();
    detailMediaBox.classList.add("drop-active");
  });
  detailMediaBox.addEventListener("dragleave", () =>
    detailMediaBox.classList.remove("drop-active"));
  detailMediaBox.addEventListener("drop", (e) => {
    if (!editing) return;
    e.preventDefault();
    detailMediaBox.classList.remove("drop-active");
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) useImageFile(file);
  });
  document.addEventListener("paste", (e) => {
    if (!editing) return;
    const items = (e.clipboardData && e.clipboardData.items) || [];
    for (const it of items) {
      if (it.type && it.type.startsWith("image/")) {
        const file = it.getAsFile();
        if (file) {
          useImageFile(file);
          e.preventDefault();
        }
        break;
      }
    }
  });

  // --- Create card: image upload (file / drag-drop / paste) ---
  const createUploadBtn = document.getElementById("create-upload-btn");
  const createUploadInput = document.getElementById("create-upload-input");

  async function useCreateImageFile(file) {
    try {
      const url = await fileToCardImage(file); // shared ≤960px encoder
      setMediaImage(url);
      flashHint("Image added.", "ok");
    } catch (e) {
      flashHint("That file isn't a valid image.", "err");
    }
  }

  createUploadBtn.addEventListener("click", () => createUploadInput.click());
  createUploadInput.addEventListener("change", () => {
    if (createUploadInput.files[0]) useCreateImageFile(createUploadInput.files[0]);
    createUploadInput.value = ""; // allow re-selecting the same file
  });
  mediaBox.addEventListener("dragover", (e) => {
    e.preventDefault();
    mediaBox.classList.add("drop-active");
  });
  mediaBox.addEventListener("dragleave", () => mediaBox.classList.remove("drop-active"));
  mediaBox.addEventListener("drop", (e) => {
    e.preventDefault();
    mediaBox.classList.remove("drop-active");
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) useCreateImageFile(file);
  });
  // Paste an image while the Create card page is showing.
  document.addEventListener("paste", (e) => {
    if (!document.getElementById("page-create").classList.contains("active")) return;
    const items = (e.clipboardData && e.clipboardData.items) || [];
    for (const it of items) {
      if (it.type && it.type.startsWith("image/")) {
        const file = it.getAsFile();
        if (file) {
          useCreateImageFile(file);
          e.preventDefault();
        }
        break;
      }
    }
  });

  // --- Capture-status badge (Create card) ---
  const captureBadge = document.getElementById("capture-status-badge");

  // Update Save readiness as the user types.
  [wordEl, wordTrEl, sentenceEl, sentenceTrEl, noteEl].forEach((el) =>
    el.addEventListener("input", setReadinessHint));

  function updateCaptureBadge() {
    const nv = window.NihongoViewer;
    const capturing = !!(nv && nv.isCapturing && nv.isCapturing());
    const engine = (nv && nv.currentEngine && nv.currentEngine()) || "";
    if (capturing) {
      captureBadge.className = "status-badge live";
      captureBadge.innerHTML =
        `<span class="live-dot"></span> Capturing${engine ? " · " + esc(engine) : ""}`;
    } else {
      captureBadge.className = "status-badge idle";
      captureBadge.textContent = "Start capture first";
    }
  }
  document.addEventListener("nv:capture-changed", updateCaptureBadge);

  // Initial paint of the readiness hint and capture badge.
  setReadinessHint();
  updateCaptureBadge();

  // --- Load decks on launch ---
  // Render the fallback immediately (so a plain browser isn't empty), then
  // refresh from the backend once the pywebview API is ready.
  loadDecks();
  window.addEventListener("pywebviewready", loadDecks);
})();

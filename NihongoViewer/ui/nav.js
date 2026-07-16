// ---- Sidebar navigation + card views (UI-only) ------------------------------
// Pure view switching for the new "Create card" / "My card" screens. No backend
// logic yet — the card/deck data shown is static placeholder markup.

// --- Top-level pages: Capture / Create card / My card ---
const navItems = document.querySelectorAll("#side-nav .nav-item");
const pages = document.querySelectorAll(".content > .page");

function showPage(name) {
  navItems.forEach((b) => b.classList.toggle("active", b.dataset.page === name));
  pages.forEach((p) => p.classList.toggle("active", p.id === `page-${name}`));
  // Let feature code react to a page becoming visible (e.g. cards.js loads the
  // current capture-stack item into the form when Create card opens).
  document.dispatchEvent(new CustomEvent("nv:page-changed", { detail: { page: name } }));
}

navItems.forEach((btn) => {
  btn.addEventListener("click", () => showPage(btn.dataset.page));
});

// --- My card sub-views: deck list -> deck -> single card ---
// The deck list, card list, card detail, and Save/open logic all live in
// cards.js (they need the backend + dynamic rendering). Here we only expose the
// view switcher and wire the breadcrumbs that jump back up the hierarchy.
const myCardViews = document.querySelectorAll("#page-mycard .mycard-view");

function showMyCardView(name) {
  myCardViews.forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
}
window.showMyCardView = showMyCardView;

// Breadcrumb links jump back up the hierarchy (Decks / the current deck).
document.querySelectorAll("#page-mycard .crumb[data-goto]").forEach((crumb) => {
  crumb.addEventListener("click", (e) => {
    e.preventDefault();
    showMyCardView(crumb.dataset.goto);
  });
});

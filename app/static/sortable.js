// Client-side column sorting for the transaction tables (the preview and
// preview-all pages). Server-side sorting is wrong here: every row carries
// hidden form fields (amount/memo/rate) plus live listeners (the row
// checkbox, the action <select>, the editable rate input), and a round-trip
// would either drop the in-progress edits or need the whole preview rebuilt.
// Reordering the <tr> nodes in place with appendChild keeps every field,
// value, and listener intact. Totals sum all rows regardless of order, so
// they never need recomputing after a sort.
//
// Opt in per table with class="sortable" and mark each sortable header with
// data-sort (name) + data-sort-type ("text" | "number"). A numeric column
// whose visible text is formatted (grouping commas, a currency suffix) should
// stash its raw value on the cell as data-sort-value so the sort compares the
// number, not the rendered string.
(function () {
  function cellValue(row, index, type) {
    const cell = row.children[index];
    if (!cell) return type === "number" ? 0 : "";
    // Prefer an explicit raw value; then a form field's current value (the
    // rate column is an <input>, whose text content is empty); then the text.
    let raw = cell.dataset.sortValue;
    if (raw === undefined) {
      const field = cell.querySelector("input, select");
      raw = field ? field.value : cell.textContent;
    }
    raw = (raw || "").trim();
    if (type === "number") {
      const n = parseFloat(raw.replace(/[^0-9eE.+-]/g, ""));
      return Number.isNaN(n) ? 0 : n;
    }
    return raw.toLowerCase();
  }

  function sortBy(table, th) {
    const headerRow = th.parentElement;
    const index = Array.prototype.indexOf.call(headerRow.children, th);
    const type = th.dataset.sortType === "number" ? "number" : "text";
    const tbody = table.tBodies[0];
    if (!tbody) return;
    // Same column flips direction; a fresh column starts ascending.
    const asc = !th.classList.contains("sorted-asc");
    // Stamp each row with its current position so equal keys keep YNAB's fetch
    // order (a stable sort) instead of reshuffling on every click.
    const rows = Array.prototype.slice.call(tbody.rows);
    rows.forEach(function (r, i) { r._sortIndex = i; });
    rows.sort(function (a, b) {
      const av = cellValue(a, index, type);
      const bv = cellValue(b, index, type);
      let cmp = av < bv ? -1 : av > bv ? 1 : 0;
      if (cmp === 0) cmp = a._sortIndex - b._sortIndex;
      return asc ? cmp : -cmp;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
    // Reset the sibling headers, then mark this one (state + arrow + aria).
    headerRow.querySelectorAll("th[data-sort]").forEach(function (other) {
      other.classList.remove("sorted-asc", "sorted-desc");
      other.setAttribute("aria-sort", "none");
      const a = other.querySelector(".arrow");
      if (a) a.textContent = "";
    });
    th.classList.add(asc ? "sorted-asc" : "sorted-desc");
    th.setAttribute("aria-sort", asc ? "ascending" : "descending");
    const arrow = th.querySelector(".arrow");
    if (arrow) arrow.textContent = asc ? "▲" : "▼";
  }

  document.querySelectorAll("table.sortable").forEach(function (table) {
    table.querySelectorAll("th[data-sort]").forEach(function (th) {
      th.setAttribute("aria-sort", "none");
      const btn = th.querySelector("button.sort");
      if (btn) btn.addEventListener("click", function () { sortBy(table, th); });
    });
  });
})();

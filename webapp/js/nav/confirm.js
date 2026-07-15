// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Promise-based confirm dialog on the app's .modal design (the same classes
// collect/training hand-roll), replacing window.confirm for the Nav page's
// destructive actions. Escape, backdrop, and ✕ all cancel; for dangerous
// actions the cancel button takes initial focus so a stray Enter can't
// delete anything.

/**
 * @param {{ title: string, body: string, confirmLabel: string, danger?: boolean }} opts
 * @returns {Promise<boolean>}
 */
export function confirmDialog(opts) {
  return new Promise((resolve) => {
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    const panel = document.createElement("div");
    panel.className = "modal";
    panel.addEventListener("click", (e) => e.stopPropagation());

    const head = document.createElement("div");
    head.className = "modal-head";
    const title = document.createElement("h2");
    title.className = "modal-title";
    title.textContent = opts.title;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "modal-close";
    close.textContent = "✕";
    head.append(title, close);

    const body = document.createElement("div");
    body.className = "modal-body";
    const text = document.createElement("p");
    text.className = "confirm-body";
    text.textContent = opts.body;
    const row = document.createElement("div");
    row.className = "confirm-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "confirm-btn";
    cancel.textContent = "Cancel";
    const ok = document.createElement("button");
    ok.type = "button";
    ok.className = `confirm-btn confirm-primary${opts.danger ? " danger" : ""}`;
    ok.textContent = opts.confirmLabel;
    row.append(cancel, ok);
    body.append(text, row);

    panel.append(head, body);
    backdrop.appendChild(panel);
    document.body.appendChild(backdrop);

    /** @param {boolean} result */
    function settle(result) {
      document.removeEventListener("keydown", onKey, true);
      backdrop.remove();
      resolve(result);
    }
    /** @param {KeyboardEvent} e */
    function onKey(e) {
      if (e.key === "Escape") {
        e.stopPropagation();
        settle(false);
      }
    }
    backdrop.addEventListener("click", () => settle(false));
    close.addEventListener("click", () => settle(false));
    cancel.addEventListener("click", () => settle(false));
    ok.addEventListener("click", () => settle(true));
    document.addEventListener("keydown", onKey, true);

    (opts.danger ? cancel : ok).focus();
  });
}

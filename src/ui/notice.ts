/* Transient notices (Obsidian's Notice equivalent), stacked bottom-right. */

let host: HTMLDivElement | null = null;

/** action: one button in the notice (e.g. Undo); running it dismisses the notice. */
export function notice(message: string, timeout = 5000, kind: "info" | "error" = "info", action?: { label: string; run: () => unknown }): void {
  if (!host || !host.isConnected) {
    host = document.createElement("div");
    host.className = "rd-notices";
    host.setAttribute("aria-live", "polite");
    document.body.appendChild(host);
  }
  const item = document.createElement("div");
  item.className = "rd-notice" + (kind === "error" ? " is-error" : "");
  item.textContent = message;
  if (action) {
    const button = document.createElement("button");
    button.type = "button"; button.className = "rd-notice-action"; button.textContent = action.label;
    button.addEventListener("click", event => { event.stopPropagation(); item.remove(); void action.run(); });
    item.appendChild(button);
  }
  item.addEventListener("click", () => item.remove());
  host.appendChild(item);
  window.setTimeout(() => { item.classList.add("is-leaving"); window.setTimeout(() => item.remove(), 200); }, timeout);
}

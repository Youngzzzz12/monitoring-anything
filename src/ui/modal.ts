/* Minimal modal + setting-row toolkit replacing Obsidian's Modal/Setting, with the same structure (setting-item,
 * setting-item-info, setting-item-control) so dialogs read like the plugin's forms. */

export function h<K extends keyof HTMLElementTagNameMap>(parent: Element | null, tag: K, options: { text?: string; cls?: string; attr?: Record<string, string> } = {}): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (options.cls) node.className = options.cls;
  if (options.text != null) node.textContent = options.text;
  for (const [key, value] of Object.entries(options.attr || {})) node.setAttribute(key, value);
  parent?.appendChild(node);
  return node;
}

export class Modal {
  readonly backdrop: HTMLDivElement;
  readonly modalEl: HTMLDivElement;
  readonly contentEl: HTMLDivElement;
  closed = false;
  private onKey = (event: KeyboardEvent) => { if (event.key === "Escape" && !this.busyLock) { event.preventDefault(); this.close(); } };
  busyLock = false;
  onClose: (() => void) | null = null;

  constructor(title: string, className = "") {
    this.backdrop = h(document.body, "div", { cls: "rd-modal-backdrop" });
    this.modalEl = h(this.backdrop, "div", { cls: ("rd-modal " + className).trim(), attr: { role: "dialog", "aria-modal": "true", "aria-label": title } });
    const close = h(this.modalEl, "button", { cls: "rd-modal-close", attr: { type: "button", "aria-label": "Close" } });
    close.addEventListener("click", () => { if (!this.busyLock) this.close(); });
    this.contentEl = h(this.modalEl, "div", { cls: "rd-modal-content" });
    h(this.contentEl, "h2", { text: title });
    this.backdrop.addEventListener("mousedown", event => { if (event.target === this.backdrop && !this.busyLock) this.close(); });
    document.addEventListener("keydown", this.onKey, true);
    requestAnimationFrame(() => this.backdrop.classList.add("is-open"));
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    document.removeEventListener("keydown", this.onKey, true);
    this.backdrop.classList.remove("is-open");
    window.setTimeout(() => this.backdrop.remove(), 160);
    this.onClose?.();
  }

  description(text: string): HTMLParagraphElement { return h(this.contentEl, "p", { text, cls: "rd-connect-description" }); }
  errorBox(): HTMLDivElement { return h(this.contentEl, "div", { cls: "rd-connect-error", attr: { role: "alert" } }); }
}

export interface Row { el: HTMLDivElement; info: HTMLDivElement; control: HTMLDivElement; desc: HTMLDivElement }

export function setting(parent: Element, name: string, desc = ""): Row {
  const el = h(parent, "div", { cls: "setting-item" });
  const info = h(el, "div", { cls: "setting-item-info" });
  if (name) h(info, "div", { cls: "setting-item-name", text: name });
  const descEl = h(info, "div", { cls: "setting-item-description", text: desc });
  const control = h(el, "div", { cls: "setting-item-control" });
  return { el, info, control, desc: descEl };
}

export function heading(parent: Element, text: string): HTMLHeadingElement { return h(parent, "h3", { cls: "rd-settings-heading", text }); }

export function textInput(row: Row, value: string, options: { placeholder?: string; type?: string; maxLength?: number; onChange?: (value: string) => void } = {}): HTMLInputElement {
  const input = h(row.control, "input", { attr: { type: options.type || "text" } });
  input.value = value;
  if (options.placeholder) input.placeholder = options.placeholder;
  if (options.maxLength) input.maxLength = options.maxLength;
  input.addEventListener("input", () => options.onChange?.(input.value));
  return input;
}

export function dropdown(row: Row, choices: [string, string][], value: string, onChange?: (value: string) => void): HTMLSelectElement {
  const select = h(row.control, "select");
  fillSelect(select, choices, value);
  select.addEventListener("change", () => onChange?.(select.value));
  return select;
}

export function fillSelect(select: HTMLSelectElement, choices: [string, string][], value: string): void {
  select.replaceChildren();
  for (const [key, label] of choices) { const option = h(select, "option", { text: label }); option.value = key; }
  if (value && !choices.some(([key]) => key === value)) { const option = h(select, "option", { text: `${value} (unavailable)` }); option.value = value; }
  select.value = value;
}

export function toggle(row: Row, value: boolean, onChange: (value: boolean) => void): HTMLInputElement {
  const label = h(row.control, "label", { cls: "rd-switch" });
  const input = h(label, "input", { attr: { type: "checkbox", role: "switch" } });
  input.checked = value;
  h(label, "span", { cls: "rd-switch-track" });
  input.addEventListener("change", () => onChange(input.checked));
  return input;
}

export function button(parent: Element, text: string, onClick: () => void, cta = false): HTMLButtonElement {
  const node = h(parent, "button", { text, cls: cta ? "mod-cta" : "", attr: { type: "button" } });
  node.addEventListener("click", onClick);
  return node;
}

export function buttons(parent: Element): HTMLDivElement { return h(parent, "div", { cls: "rd-modal-buttons" }); }

/** Disable every control of a form while an action runs. */
export function setBusy(root: Element, busy: boolean): void {
  for (const control of Array.from(root.querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement | HTMLTextAreaElement>("input, button, select, textarea"))) {
    if (control.classList.contains("rd-modal-close")) continue;
    if (busy) { control.dataset.rdWasDisabled = String(control.disabled); control.disabled = true; }
    else if (control.dataset.rdWasDisabled !== undefined) { control.disabled = control.dataset.rdWasDisabled === "true"; delete control.dataset.rdWasDisabled; }
  }
}

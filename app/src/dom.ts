type Attrs = Record<string, string | number | boolean | null | undefined | EventListener>;
type Child = Node | string | number | null | undefined | false;

/**
 * Minimal element builder. Text always goes through text nodes, so file
 * names and rclone messages can never be interpreted as HTML.
 */
export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  ...children: (Child | Child[])[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) {
      continue;
    }
    if (key.startsWith('on') && typeof value === 'function') {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'value' && 'value' in el) {
      (el as HTMLInputElement).value = String(value);
    } else if (key === 'checked' && 'checked' in el) {
      (el as HTMLInputElement).checked = value === true;
    } else {
      el.setAttribute(key, value === true ? '' : String(value));
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) {
      continue;
    }
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function humanSize(n: number): string {
  let size = Math.max(n, 0);
  for (const unit of ['B', 'KiB', 'MiB', 'GiB']) {
    if (size < 1024) {
      return unit === 'B' ? `${size.toFixed(0)} B` : `${size.toFixed(2)} ${unit}`;
    }
    size /= 1024;
  }
  return `${size.toFixed(2)} TiB`;
}

/** Accepts "0", "512", "50M", "1.5G" (binary units). Returns null if invalid. */
export function parseSize(text: string): number | null {
  const m = /^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*$/i.exec(text);
  if (m === null) {
    return null;
  }
  const mult: Record<string, number> = { '': 1, k: 1024, m: 1024 ** 2, g: 1024 ** 3, t: 1024 ** 4 };
  return Math.floor(parseFloat(m[1]) * mult[m[2].toLowerCase()]);
}

export function humanise(optionName: string): string {
  const text = optionName.replace(/_/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

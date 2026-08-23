import assert from "node:assert/strict";

class FakeClassList {
  constructor(node) {
    this.node = node;
  }

  add(...names) {
    const values = new Set(this.node.className.split(/\s+/).filter(Boolean));
    for (const name of names) values.add(name);
    this.node.className = [...values].join(" ");
  }
}

export class FakeNode {
  constructor(tagName = "#text", text = "") {
    this.tagName = tagName.toUpperCase();
    this.nodeType = tagName === "#text" ? 3 : 1;
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.className = "";
    this.dataset = Object.create(null);
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this._text = String(text);
    this.classList = new FakeClassList(this);
  }

  get textContent() {
    if (this.nodeType === 3) return this._text;
    return this._text + this.children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this._text = String(value ?? "");
    this.children = [];
  }

  set innerHTML(_value) {
    throw new Error("unsafe innerHTML use");
  }

  get innerHTML() {
    throw new Error("unsafe innerHTML read");
  }

  append(...nodes) {
    for (const node of nodes.flat()) {
      this.children.push(
        typeof node === "string" ? new FakeNode("#text", node) : node,
      );
    }
  }

  appendChild(node) {
    this.append(node);
    return node;
  }

  replaceChildren(...nodes) {
    this._text = "";
    this.children = [];
    this.append(...nodes);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) ?? [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  dispatchEvent(event) {
    event.target ??= this;
    for (const listener of this.listeners.get(event.type) ?? []) listener(event);
  }

  click() {
    this.dispatchEvent({ type: "click", preventDefault() {} });
  }

  querySelectorAll(selector) {
    const matches = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (matchesSelector(child, selector)) matches.push(child);
        visit(child);
      }
    };
    visit(this);
    return matches;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }
}

function matchesSelector(node, selector) {
  if (node.nodeType !== 1) return false;
  if (selector.startsWith(".")) {
    return node.className.split(/\s+/).includes(selector.slice(1));
  }
  if (selector.startsWith("[data-") && selector.endsWith("]")) {
    const key = selector.slice(6, -1).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    return Object.hasOwn(node.dataset, key);
  }
  return node.tagName === selector.toUpperCase();
}

export class FakeDocument {
  createElement(tagName) {
    return new FakeNode(tagName);
  }

  createTextNode(text) {
    return new FakeNode("#text", text);
  }
}

export function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
    removeItem(key) {
      values.delete(key);
    },
    dump() {
      return Object.fromEntries(values);
    },
  };
}

export function descendants(node, tagName) {
  return node.querySelectorAll(tagName);
}

export function findButton(node, label) {
  const button = descendants(node, "button").find(
    (candidate) => candidate.textContent === label,
  );
  assert.ok(button, `button ${JSON.stringify(label)} was not rendered`);
  return button;
}

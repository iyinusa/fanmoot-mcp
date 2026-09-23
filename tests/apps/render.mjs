/* Render one view against one fixture payload, and report what it did.
 *
 * `S6-8` asks for each view to render from a fixture payload and for bridge
 * calls to be asserted. Neither is a thing a regex can answer, and neither is
 * worth a headless browser in a Python repo — so this is the smallest honest
 * middle: the view's real JavaScript, the real bridge, a DOM with exactly the
 * surface the four views touch, and a host on the other end of `postMessage`
 * playing the sequence SEP-1865 defines.
 *
 * What it is not: a browser. It does no layout, no CSS, no paint. A test built
 * on it can say "the scoreline is in the document and the crest has that alt
 * text"; it cannot say "the crest is on the left", and the tests that use it
 * do not claim to.
 *
 * Usage:  node render.mjs <view.html> <<< '{"payload": {...}, "act": {...}}'
 * Output: JSON on stdout — { sent, text, nodes, images, errors }
 */

import { readFileSync } from 'node:fs';
import { createContext, runInContext } from 'node:vm';

// ── The smallest DOM the four views touch ───────────────────────────────────
class Node {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.style = makeStyle();
    this._text = '';
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.placeholder = '';
  }

  get childNodes() {
    return this.children;
  }

  set textContent(value) {
    this._text = value == null ? '' : String(value);
    this.children = [];
  }

  get textContent() {
    if (this.children.length === 0) return this._text;
    return this.children.map((child) => child.textContent).join('');
  }

  appendChild(child) {
    if (child) this.children.push(child);
    return child;
  }

  replaceChildren() {
    this.children = [];
    this._text = '';
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  addEventListener(type, handler) {
    (this.listeners[type] ||= []).push(handler);
  }

  dispatch(type, event = {}) {
    (this.listeners[type] || []).forEach((handler) =>
      handler({ preventDefault() {}, ...event })
    );
  }

  /* Walk, for the assertions on the Python side. */
  walk(visit) {
    visit(this);
    this.children.forEach((child) => child.walk(visit));
  }
}

function makeStyle() {
  const style = {};
  style.setProperty = (name, value) => {
    style[name] = value;
  };
  return style;
}

function makeDocument(html) {
  const byId = new Map();
  const documentElement = new Node('html');
  const head = new Node('head');
  const body = new Node('body');
  documentElement.appendChild(head);
  documentElement.appendChild(body);

  /* The views address their own markup by id. Rather than parse the HTML —
   * which would mean writing a parser to test a renderer — the ids the
   * document declares are collected and given empty elements, which is
   * precisely what `getElementById` hands back before anything is rendered. */
  for (const [, tag, id] of html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"/g)) {
    const node = new Node(tag);
    node.id = id;
    byId.set(id, node);
    body.appendChild(node);
  }

  return {
    documentElement,
    head,
    body,
    createElement: (tag) => new Node(tag),
    getElementById: (id) => byId.get(id) ?? null,
    _byId: byId,
  };
}

// ── The host on the other end of postMessage ────────────────────────────────
const input = JSON.parse(readFileSync(0, 'utf8'));
const html = readFileSync(process.argv[2], 'utf8');

const sent = [];
const errors = [];
const document = makeDocument(html);
const listeners = [];

const window = {
  addEventListener(type, handler) {
    if (type === 'message') listeners.push(handler);
  },
  parent: {
    postMessage(message) {
      sent.push(message);
      queueMicrotask(() => host(message));
    },
  },
};

function deliver(message) {
  listeners.forEach((handler) => handler({ data: message }));
}

/* The sequence in the spec: the View initializes, the Host answers with its
 * context, the View says initialized, the Host sends the tool input and then
 * the tool result. Anything the View asks for afterwards — `tools/call` from
 * the composer — is answered from the fixture. */
function host(message) {
  if (message.method === 'ui/initialize') {
    deliver({
      jsonrpc: '2.0',
      id: message.id,
      result: {
        protocolVersion: '2026-01-26',
        hostCapabilities: { serverTools: {}, openLinks: {} },
        hostInfo: { name: 'test-host', version: '1.0.0' },
        hostContext: {
          theme: input.theme ?? 'dark',
          styles: { variables: input.variables ?? {} },
          displayMode: 'inline',
          availableDisplayModes: ['inline', 'fullscreen'],
          containerDimensions: { maxWidth: 420, maxHeight: 600 },
        },
      },
    });
    return;
  }

  if (message.method === 'ui/notifications/initialized') {
    deliver({
      jsonrpc: '2.0',
      method: 'ui/notifications/tool-input',
      params: { arguments: input.arguments ?? {} },
    });
    deliver({
      jsonrpc: '2.0',
      method: 'ui/notifications/tool-result',
      params: { content: [{ type: 'text', text: input.payload.spoken ?? '' }], structuredContent: input.payload },
    });
    return;
  }

  if (message.id !== undefined) {
    /* Every other request the View makes: answered, so a `.then` runs and the
     * test can see what the View did with the answer. `toolResult` in the
     * fixture is what a `tools/call` comes back with. */
    const result =
      message.method === 'tools/call' ? (input.toolResult ?? { structuredContent: {} }) : {};
    deliver({ jsonrpc: '2.0', id: message.id, result });
  }
}

// ── Run the view's own scripts ──────────────────────────────────────────────
const context = createContext({
  window,
  document,
  console,
  queueMicrotask,
  Promise,
  Object,
  Math,
  String,
  JSON,
  setTimeout,
});

for (const [, code] of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
  try {
    runInContext(code, context, { filename: process.argv[2] });
  } catch (error) {
    errors.push(String(error));
  }
}

/* Two turns of the microtask queue: one for the initialize round trip, one for
 * whatever the render did. Then the optional interaction, then one more. */
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));

if (input.act && input.act.submit) {
  const box = document.getElementById(input.act.field);
  if (box) box.value = input.act.text ?? '';
  const form = document.getElementById(input.act.submit);
  if (form) form.dispatch('submit');
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

// ── Report ──────────────────────────────────────────────────────────────────
const nodes = [];
const images = [];
document.body.walk((node) => {
  nodes.push({
    tag: node.tagName,
    id: node.id ?? null,
    class: node.className || '',
    text: node.children.length ? '' : node.textContent,
    attributes: node.attributes,
    hidden: node.hidden,
    style: { ...node.style, setProperty: undefined },
  });
  if (node.tagName === 'img') images.push({ src: node.src ?? '', alt: node.alt ?? '' });
});

process.stdout.write(
  JSON.stringify({
    sent,
    text: document.body.textContent,
    nodes,
    images,
    errors,
    theme: document.documentElement.getAttribute('data-theme'),
  })
);

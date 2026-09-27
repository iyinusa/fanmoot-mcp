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
/* What the ext-apps SDK's zod schemas require of the View's messages — the
 * checks a real host (Claude's among them) runs before it answers. A harness
 * that accepted anything is how `clientInfo` shipped in place of `appInfo`,
 * and every card rendered here and nowhere else. Violations are recorded as
 * errors, which fail the test, and the request is refused as a host would. */
function invalid(message) {
  const p = message.params ?? {};
  const named = (v) => v && typeof v.name === 'string' && typeof v.version === 'string';
  switch (message.method) {
    case 'ui/initialize':
      if (!named(p.appInfo)) return 'ui/initialize needs appInfo {name, version}';
      if (typeof p.appCapabilities !== 'object' || p.appCapabilities === null)
        return 'ui/initialize needs appCapabilities';
      if (typeof p.protocolVersion !== 'string') return 'ui/initialize needs protocolVersion';
      return null;
    case 'ui/message':
      if (p.role !== 'user') return 'ui/message role must be "user"';
      if (!Array.isArray(p.content)) return 'ui/message content must be an array of blocks';
      return null;
    case 'ui/open-link':
      return typeof p.url === 'string' ? null : 'ui/open-link needs a url';
    case 'ui/update-model-context':
      if (p.content !== undefined && !Array.isArray(p.content))
        return 'ui/update-model-context content must be an array';
      return null;
    default:
      return null;
  }
}

/* A request the host sends first, after the View is up, so the test can see
 * it answered: `ping`, `ui/resource-teardown`, or anything else in
 * `input.hostRequests`. Numbered from 1, like the View's own — a collision
 * is exactly what the View has to survive. */
const answered = [];
function hostRequests() {
  (input.hostRequests ?? []).forEach((method, index) => {
    deliver({ jsonrpc: '2.0', id: index + 1, method, params: {} });
  });
}

function host(message) {
  if (message.method === undefined && message.id !== undefined) {
    answered.push(message);
    return;
  }
  const problem = invalid(message);
  if (problem) {
    errors.push(problem);
    if (message.id !== undefined) {
      deliver({ jsonrpc: '2.0', id: message.id, error: { code: -32602, message: problem } });
    }
    return;
  }
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
    hostRequests();
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

/* The interactions a test can ask for, in order. `act` is one; `acts` is a
 * list. Controls a view builds in script have no id, so they are found by the
 * `data-act` / `data-field` attributes the views put on them for exactly this:
 *   { submit: <button id>, field: <textarea id>, text } type, then press send
 *   { fill: { <data-field>: <value>, … } }              type into built inputs
 *   { click: <data-act> }                               press a built button
 *
 * There is deliberately no way to fire a form's `submit` event. Inside a host
 * sandbox without `allow-forms` — Claude's — the browser drops a submission
 * before that event exists, so a test that fired it would pass for a button
 * that does nothing in the one place it has to work. */
function byAttribute(name, value) {
  let found = null;
  document.body.walk((node) => {
    if (!found && node.attributes[name] === value) found = node;
  });
  return found;
}

const acts = input.acts ?? (input.act ? [input.act] : []);
for (const act of acts) {
  if (act.submit) {
    const box = document.getElementById(act.field);
    if (box) box.value = act.text ?? '';
    const button = document.getElementById(act.submit);
    if (!button) errors.push(`no button ${act.submit}`);
    else button.dispatch('click', { stopPropagation() {} });
  }
  for (const [field, value] of Object.entries(act.fill ?? {})) {
    const box = byAttribute('data-field', field);
    if (!box) errors.push(`no field ${field}`);
    else box.value = value;
  }
  if (act.click) {
    const button = byAttribute('data-act', act.click);
    if (!button) errors.push(`no control ${act.click}`);
    else button.dispatch('click', { stopPropagation() {} });
  }
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
    answered,
    theme: document.documentElement.getAttribute('data-theme'),
  })
);

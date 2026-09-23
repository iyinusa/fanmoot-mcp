/* The app bridge: JSON-RPC 2.0 over postMessage, as SEP-1865 defines it.
 *
 * Inlined into all four views (`S6-6`), so there is one implementation of the
 * handshake rather than four slightly different ones. No SDK — the spec says
 * outright that you do not need one to talk MCP with the host, and a bundled
 * dependency inside a sandboxed iframe is a supply chain nobody reviews.
 *
 * The lifecycle, in the spec's order:
 *   View  → Host   ui/initialize                 (with appCapabilities)
 *   Host  → View   McpUiInitializeResult         (hostContext: theme, styles…)
 *   View  → Host   ui/notifications/initialized
 *   Host  → View   ui/notifications/tool-input   (at most once)
 *   Host  → View   ui/notifications/tool-result  (the CallToolResult)
 *
 * A view supplies `render(structuredContent, result)` and nothing else.
 */
(function () {
  'use strict';

  var PROTOCOL = '__PROTOCOL__';
  var nextId = 1;
  var waiting = {};
  var handlers = {};
  var host = { capabilities: {}, context: {} };

  function post(message) {
    window.parent.postMessage(message, '*');
  }

  function request(method, params) {
    var id = nextId++;
    return new Promise(function (resolve, reject) {
      waiting[id] = { resolve: resolve, reject: reject };
      post({ jsonrpc: '2.0', id: id, method: method, params: params || {} });
    });
  }

  function notify(method, params) {
    post({ jsonrpc: '2.0', method: method, params: params || {} });
  }

  window.addEventListener('message', function (event) {
    var data = event.data;
    if (!data || data.jsonrpc !== '2.0') return;

    if (data.id !== undefined && waiting[data.id]) {
      var pending = waiting[data.id];
      delete waiting[data.id];
      if (data.error) {
        pending.reject(new Error(data.error.message || 'request failed'));
      } else {
        pending.resolve(data.result);
      }
      return;
    }
    // A notification, or a request from the host. Unknown methods are ignored
    // rather than answered: a view that guessed at a method it does not
    // implement would be worse than one that stayed quiet.
    var handler = handlers[data.method];
    if (handler) handler(data.params || {});
  });

  function on(method, handler) {
    handlers[method] = handler;
  }

  /* ── Theming ───────────────────────────────────────────────────────────────
   * The host may pass any subset of the standardised CSS variables, or none.
   * Every one this view uses has a Fanmoot fallback in the stylesheet, so a
   * host that passes nothing gets Floodlit and a host that passes its own
   * palette gets its own — which is what "visual cohesion with the host
   * environment" means in the spec's theming section.
   */
  function applyHostContext(context) {
    if (!context) return;
    host.context = context;
    var root = document.documentElement;
    if (context.theme) root.setAttribute('data-theme', context.theme);

    var styles = context.styles || {};
    var variables = styles.variables || {};
    Object.keys(variables).forEach(function (key) {
      if (variables[key] != null) root.style.setProperty(key, variables[key]);
    });
    if (styles.css && styles.css.fonts) {
      var sheet = document.createElement('style');
      sheet.textContent = styles.css.fonts;
      document.head.appendChild(sheet);
    }

    var box = context.containerDimensions;
    if (box) {
      if ('height' in box) root.style.height = '100vh';
      else if (box.maxHeight) root.style.maxHeight = box.maxHeight + 'px';
      if ('width' in box) root.style.width = '100vw';
      else if (box.maxWidth) root.style.maxWidth = box.maxWidth + 'px';
    }
  }

  /* Flexible dimensions mean the host sizes the frame from what we report. */
  function watchSize() {
    if (typeof ResizeObserver === 'undefined') return;
    var last = { width: 0, height: 0 };
    var observer = new ResizeObserver(function () {
      var width = Math.ceil(document.documentElement.scrollWidth);
      var height = Math.ceil(document.documentElement.scrollHeight);
      if (width === last.width && height === last.height) return;
      last = { width: width, height: height };
      notify('ui/notifications/size-changed', { width: width, height: height });
    });
    observer.observe(document.documentElement);
  }

  /* ── What a view is handed ─────────────────────────────────────────────── */
  var view = {
    host: host,

    /** Call a tool on this server, proxied by the host. */
    callTool: function (name, args) {
      return request('tools/call', { name: name, arguments: args || {} });
    },

    /** Ask the host to open a link. Never `window.open`: the sandbox has no
     *  navigation, and the host is the one that can ask the person first. */
    openLink: function (url) {
      return request('ui/open-link', { url: url });
    },

    /** Put words in the conversation, as the person. */
    say: function (text) {
      return request('ui/message', {
        role: 'user',
        content: { type: 'text', text: text },
      });
    },

    /** Tell the model what the person is now looking at. */
    updateModelContext: function (structuredContent) {
      return request('ui/update-model-context', { structuredContent: structuredContent });
    },

    requestDisplayMode: function (mode) {
      var available = (host.context && host.context.availableDisplayModes) || [];
      if (available.indexOf(mode) === -1) return Promise.resolve({ mode: host.context.displayMode });
      return request('ui/request-display-mode', { mode: mode });
    },
  };

  /** Boot. `render` is called with the tool's structuredContent. */
  window.fanmoot = function (render) {
    on('ui/notifications/tool-result', function (result) {
      var payload = result && result.structuredContent;
      if (payload) render(payload, result, view);
    });
    on('ui/notifications/tool-input', function () {
      /* Arguments arrive before the result. Nothing here needs them — every
       * view renders from the answer — so this exists to be explicit that they
       * were received and deliberately unused. */
    });
    on('ui/notifications/tool-cancelled', function (params) {
      var status = document.getElementById('status');
      if (status) status.textContent = (params && params.reason) || 'That was cancelled.';
    });
    on('ui/notifications/host-context-changed', function (params) {
      applyHostContext(Object.assign({}, host.context, params));
    });

    request('ui/initialize', {
      protocolVersion: PROTOCOL,
      appCapabilities: { availableDisplayModes: ['inline', 'fullscreen'] },
      clientInfo: { name: 'fanmoot-view', version: '__VERSION__' },
    })
      .then(function (result) {
        host.capabilities = (result && result.hostCapabilities) || {};
        applyHostContext(result && result.hostContext);
        notify('ui/notifications/initialized', {});
        watchSize();
      })
      .catch(function () {
        /* No host, or a host that refused the handshake. The view stays as its
         * own markup left it — which is the empty state, not a blank frame. */
      });

    return view;
  };
})();

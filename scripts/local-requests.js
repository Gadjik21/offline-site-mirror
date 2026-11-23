// Resolve only recorded, read-only requests. Leave unresolved requests visible to offline QA.
const source = document.currentScript.dataset.source;
const localFiles = new Set(routes.values());

function localAddress(value) {
  let parsed;
  try {
    parsed = new URL(value, window.location.href);
  } catch {
    return null;
  }
  if (parsed.origin === window.location.origin) {
    if (localFiles.has(parsed.pathname + parsed.search)) return null;
    parsed = new URL(parsed.pathname + parsed.search + parsed.hash, source);
  }
  parsed.hash = "";
  const direct = routes.get(parsed.href);
  if (direct) return direct;
  try {
    const againstSource = new URL(value, source);
    againstSource.hash = "";
    return routes.get(againstSource.href) || null;
  } catch {
    return null;
  }
}

const nativeFetch = window.fetch;
window.fetch = function (input, init) {
  const method = init?.method || (input instanceof Request ? input.method : "GET");
  if (typeof method === "string" && method.toUpperCase() === "GET") {
    const request = input instanceof Request ? input : null;
    const local = localAddress(request ? request.url : input);
    if (local) {
      try {
        const target = new URL(local, window.location.href);
        return nativeFetch.call(this, request ? new Request(target, request) : target, init);
      } catch {
        // Let fetch handle malformed or unsupported inputs with its usual behavior.
      }
    }
  }
  return nativeFetch.call(this, input, init);
};

const nativeOpen = XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open = function (method, url, ...options) {
  const local = typeof method === "string" && method.toUpperCase() === "GET" && localAddress(url);
  return nativeOpen.call(this, method, local || url, ...options);
};

// Some site scripts append a slash after an anchor has been rewritten to an HTML file.
function fixLocalLink(anchor) {
  const url = new URL(anchor.href, location.href);
  if (url.origin !== location.origin || !/\.html?\/$/i.test(url.pathname)) return;
  const path = url.pathname.slice(0, -1);
  if (localFiles.has(path + url.search)) anchor.href = path + url.search + url.hash;
}

function fixLinks(node) {
  if (node.nodeType !== Node.ELEMENT_NODE) return;
  if (node.matches("a[href]")) fixLocalLink(node);
  for (const anchor of node.querySelectorAll("a[href]")) fixLocalLink(anchor);
}

function watchLinks() {
  fixLinks(document.documentElement);
  new MutationObserver(changes => {
    for (const change of changes) {
      if (change.type === "attributes") {
        if (change.target.matches("a[href]")) fixLocalLink(change.target);
      } else {
        for (const node of change.addedNodes) fixLinks(node);
      }
    }
  }).observe(document.documentElement, {
    subtree: true, childList: true, attributes: true, attributeFilter: ["href"],
  });
}
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", watchLinks, { once: true });
else watchLinks();

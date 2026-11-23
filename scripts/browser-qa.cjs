const fs = require("node:fs");
const { dirname, join } = require("node:path");
const puppeteer = require("/app/node_modules/puppeteer-core");

const [, , origin, manifestFile] = process.argv;
const manifest = JSON.parse(fs.readFileSync(manifestFile, "utf8"));
const urlMap = JSON.parse(fs.readFileSync(join(dirname(manifestFile), "url-map.json"), "utf8"));
const scopeHosts = new Set(manifest.source_hosts || []);
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function inspect(page, url, onOpen) {
  const response = await page.goto(url, { waitUntil: "domcontentloaded", timeout: 25000 });
  if (response?.status() !== 200 || !(response.headers()["content-type"] || "").includes("text/html")) {
    throw new Error("Expected a local HTML document");
  }
  onOpen();
  let loadTimedOut = false;
  try {
    // Timer polling also works in tabs that are not painting.
    await page.waitForFunction(() => document.readyState === "complete", { polling: 100, timeout: 15000 });
  } catch (error) {
    if (error.name !== "TimeoutError") throw error;
    loadTimedOut = true;
  }
  await page.evaluate(async () => {
    // Exercise scroll-triggered lazy loads without submitting a form or following a link.
    const step = Math.max(700, window.innerHeight);
    const last = document.documentElement.scrollHeight;
    let top = 0;
    for (let n = 0; n < 12 && top < last; n += 1) {
      window.scrollTo(0, top);
      await new Promise(resolve => setTimeout(resolve, 45));
      top += step;
    }
    window.scrollTo(0, 0);
    for (const img of document.images) img.loading = "eager";
  });
  await pause(150);
  const details = await page.evaluate(async () => {
    const images = [...document.images].filter(img => img.currentSrc || img.getAttribute("src") || img.srcset);
    const deadline = performance.now() + 7500;
    while (performance.now() < deadline &&
           (document.fonts.status === "loading" || images.some(img => !img.complete))) {
      await new Promise(resolve => setTimeout(resolve, 60));
    }
    // Draw a sample to exercise the decoder without waiting for a background
    // tab to paint. A complete zero-width image is a load failure.
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const context = canvas.getContext("2d");
    const failures = [];
    for (const img of images) {
      try {
        if (!img.complete || !img.naturalWidth) throw new Error("Image unavailable");
        context.drawImage(img, 0, 0, 1, 1);
      } catch {
        failures.push(img.currentSrc || img.getAttribute("src") || img.srcset);
      }
    }
    const timedOut = document.fonts.status === "loading";
    return {
      images: images.length - failures.length,
      failures,
      timedOut,
      fonts: [...document.fonts].filter(font => font.status === "loaded").length,
      stylesheets: [...document.styleSheets].filter(sheet => sheet.href).length,
      links: [...document.querySelectorAll("a[href]")].map(anchor => anchor.href),
      declaredIcon: Boolean(document.querySelector('link[rel~="icon"][href]')),
    };
  });
  return { ...details, loadTimedOut };
}

async function check() {
  const browser = await puppeteer.launch({
    executablePath: "/usr/bin/brave-origin",
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--no-proxy-server",
      "--disable-background-timer-throttling", "--disable-renderer-backgrounding"],
  });
  const result = {
    passed: false, html_pages: manifest.pages.length, opened_pages: 0, checked_pages: 0,
    fully_offline_pages: 0, images_decoded: 0,
    fonts_loaded: 0, stylesheets_loaded: 0, local_links_checked: 0,
    external_requests: [], resource_errors: [], page_errors: [],
    optional_browser_404s: [], outbound_links_not_followed: 0,
  };
  const external = new Set();
  const broken = new Set();
  const localLinks = new Set();
  const outbound = new Set();
  const unmirrored = new Set();
  const pageResults = [];
  const linkErrors = new Map();
  const optionalIcons = new Set();
  let cursor = 0;
  try {
    async function worker() {
      const page = await browser.newPage();
      await page.setViewport({ width: 1280, height: 900 });
      await page.setCacheEnabled(false);
      await page.setRequestInterception(true);
      let current = null;
      page.on("request", request => {
        const url = new URL(request.url());
        if (/^https?:$/.test(url.protocol) && url.origin !== origin) {
          external.add(url.href);
          current?.external.add(url.href);
          void request.abort();
        } else {
          void request.continue();
        }
      });
      page.on("response", response => {
        if (response.status() >= 400) {
          const message = response.status() + " " + response.url();
          if (response.status() === 404 && response.url() === origin + "/favicon.ico") {
            current?.icons.add(message);
          } else {
            broken.add(message);
            current?.broken.add(message);
          }
        }
      });
      page.on("requestfailed", request => {
        const message = request.failure()?.errorText;
        if (new URL(request.url()).origin === origin && message !== "net::ERR_ABORTED") {
          broken.add(message + " " + request.url());
          current?.broken.add(message + " " + request.url());
        }
      });
      try {
        while (cursor < manifest.pages.length) {
          const path = manifest.pages[cursor++];
          current = { path, opened: false, error: null, links: new Set(), external: new Set(),
            broken: new Set(), icons: new Set(), linksStillOnline: new Set() };
          try {
            const info = await inspect(page, origin + path, () => {
              current.opened = true;
              result.opened_pages += 1;
            });
            if (info.loadTimedOut) {
              current.error = { path, error: "The document opened but its load event timed out", urls: info.failures };
            } else if (info.timedOut || info.failures.length) {
              current.error = { path, error: "Images or fonts did not load", urls: info.failures };
            } else {
              result.checked_pages += 1;
            }
            for (const error of current.icons) {
              if (info.declaredIcon) {
                current.broken.add(error);
                broken.add(error);
              } else {
                optionalIcons.add(error);
              }
            }
            result.images_decoded += info.images;
            result.fonts_loaded += info.fonts;
            result.stylesheets_loaded += info.stylesheets;
            for (const href of info.links) {
              const url = new URL(href);
              if (!/^https?:$/.test(url.protocol)) continue;
              url.hash = "";
              if (url.origin === origin) {
                current.links.add(url.href);
                localLinks.add(url.href);
              } else {
                outbound.add(url.href);
                if (Object.hasOwn(urlMap, url.href)) current.linksStillOnline.add(url.href);
                else if (scopeHosts.has(url.hostname)) unmirrored.add(url.href);
              }
            }
          } catch (error) {
            current.error = { path, error: error.message };
          }
          pageResults.push(current);
          current = null;
          if (pageResults.length % 50 === 0) console.error("Verified " + pageResults.length + " pages");
        }
      } finally {
        await page.close();
      }
    }
    await Promise.all(Array.from({ length: Math.min(4, manifest.pages.length) }, worker));
    const targets = [...localLinks];
    let linkIndex = 0;
    async function checkLinks() {
      while (linkIndex < targets.length) {
        const link = targets[linkIndex++];
        try {
          const response = await fetch(link, { method: "HEAD", redirect: "manual", signal: AbortSignal.timeout(5000) });
          if (!response.ok) throw new Error("HTTP " + response.status);
          result.local_links_checked += 1;
        } catch (error) {
          const detail = error.message + " " + link;
          broken.add(detail);
          linkErrors.set(link, detail);
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(8, targets.length) }, checkLinks));
  } finally {
    await browser.close();
  }
  result.external_requests = [...external].sort();
  result.resource_errors = [...broken].sort();
  result.optional_browser_404s = [...optionalIcons].filter(item => !broken.has(item));
  result.unmirrored_internal_links = [...unmirrored].sort();
  result.offline_page_results = pageResults.sort((a, b) => a.path.localeCompare(b.path)).map(item => {
    const failedLinks = [...item.links].flatMap(link => linkErrors.has(link) ? [linkErrors.get(link)] : []);
    const externalResources = [...item.external].sort();
    const localErrors = [...item.broken, ...failedLinks].sort();
    const onlineCapturedLinks = [...item.linksStillOnline].sort();
    const passed = item.opened && !item.error && !externalResources.length && !localErrors.length && !onlineCapturedLinks.length;
    if (passed) result.fully_offline_pages += 1;
    if (item.error) result.page_errors.push(item.error);
    return { path: item.path, source: manifest.source_pages?.[item.path], opened: item.opened,
      passed, error: item.error?.error, external_resources: externalResources,
      resource_or_link_errors: localErrors, captured_links_still_online: onlineCapturedLinks };
  });
  result.outbound_links_not_followed = outbound.size;
  result.passed = result.checked_pages > 0 && result.checked_pages === result.html_pages &&
    result.fully_offline_pages === result.html_pages && !broken.size && !external.size;
  if (!result.passed) {
    const first = result.page_errors[0];
    result.error = first ? first.path + ": " + first.error :
      ([...broken, ...external][0] || "A captured page or link did not verify offline");
  }
  return result;
}

check().then(report => console.log(JSON.stringify(report))).catch(error => {
  console.error(error);
  process.exitCode = 1;
});

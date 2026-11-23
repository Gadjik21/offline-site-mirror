"""Run a small Browsertrix/export regression suite with loopback networking only."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from capture_browser import collect, run_crawler
from verify import verify


PAGE = b"""<!doctype html><meta charset=utf-8><html><head>
<link rel=stylesheet href=/style.css>
<link rel=preload as=image href="/photo.svg?v=two">
</head><body>
<h1>Offline gallery</h1><a href="/details/">Details</a>
<a href="https://outside.invalid/public-page">External</a>
<iframe id=embedded src="/embed/?show=one"></iframe>
<img id=hero src="/photo.svg?v=one" width=160 height=90>
<button id=next data-src="/photo.svg?v=two">Next photo</button>
<div style="height:1200px"></div>
<img id=lazy loading=lazy src="/path/lazy.svg" width=160 height=90>
<script src="/gallery.js"></script>
</body></html>"""
SCRIPT = b"""if (document.querySelector('a').pathname.endsWith('.html')) document.querySelector('a').href += '/';
document.querySelector('#next').addEventListener('click', e => {
  document.querySelector('#hero').src = e.currentTarget.dataset.src;
});
fetch('/label.json?n=one').then(response => response.json()).then(value => {
  document.body.dataset.label = value.label;
});"""
STYLE = b"""body { color: rgb(24, 69, 120); }
#hero { background: url('/tile.svg?v=1'); }"""
COLORS = {"one": "green", "two": "blue", "lazy": "red", "tile": "gray"}


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        address = urlsplit(self.path)
        if address.path == "/":
            mime, body = "text/html", PAGE
        elif address.path == "/embed/":
            mime, body = "text/html", (
                b"<!doctype html><html><head><title>Embed</title></head><body>"
                b"<script>document.body.textContent = new URLSearchParams(location.search).get('show')</script>"
            )
        elif address.path == "/details/":
            mime, body = "text/html", b"<!doctype html><h1>Details</h1><a href=/>Gallery</a><a href=/second-level/>More</a>"
        elif address.path == "/second-level/":
            mime, body = "text/html", b"<!doctype html><h1>Second level</h1>"
        elif address.path == "/gallery.js":
            mime, body = "text/javascript", SCRIPT
        elif address.path == "/style.css":
            mime, body = "text/css", STYLE
        elif address.path == "/label.json":
            mime, body = "application/json", b'{"label":"recorded"}'
        elif address.path == "/robots.txt":
            mime = "text/plain"
            body = ("User-agent: *\nAllow: /\n"
                    f"Sitemap: http://{self.headers['Host']}/sitemap-index.xml\n").encode()
        elif address.path == "/sitemap-index.xml":
            mime = "application/xml"
            body = (f"<sitemapindex><sitemap><loc>http://{self.headers['Host']}/sitemap.xml</loc>"
                    "</sitemap></sitemapindex>").encode()
        elif address.path == "/sitemap.xml":
            mime = "application/xml"
            body = (f"<urlset><url><loc>http://{self.headers['Host']}/unlinked/</loc></url>"
                    "<url><loc>https://outside.invalid/</loc></url></urlset>").encode()
        elif address.path == "/unlinked/":
            mime, body = "text/html", b"<!doctype html><h1>Only in the sitemap</h1>"
        elif address.path in {"/photo.svg", "/path/lazy.svg", "/tile.svg"}:
            key = parse_qs(address.query).get("v", ["one"])[0]
            color = COLORS.get(key, COLORS["lazy"] if address.path != "/tile.svg" else COLORS["tile"])
            mime = "image/svg+xml"
            body = ('<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90">'
                    f'<rect width="160" height="90" fill="{color}"/></svg>').encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def check_rate_guard(work):
    script = """
import json, time
for index in range(4):
    print(json.dumps({"context": "pageStatus", "details": {
        "status": 429, "page": "http://127.0.0.1/" + str(index)
    }}), flush=True)
time.sleep(20)
"""
    with (work / "rate-check.log").open("w+", encoding="utf-8") as log:
        _, reason = run_crawler([sys.executable, "-c", script], work, log, timeout=10)
    assert reason and "HTTP 429" in reason, reason


def run(output, shallow=False):
    with tempfile.TemporaryDirectory() as temp:
        work = Path(temp)
        check_rate_guard(work)
        with ThreadingHTTPServer(("127.0.0.1", 0), Fixture) as server:
            origin = f"http://127.0.0.1:{server.server_port}"
            config = {"seed": origin + "/", "expected": [origin + "/", origin + "/details/"],
                      "workers": 4, "time_limit": 90, "discover_sitemap": True, "exclude": []}
            if shallow:
                config.update(discover_sitemap=False, depth=1)
            (work / "profile.json").write_text(json.dumps(config))
            Thread(target=server.serve_forever, daemon=True).start()
            try:
                capture = collect(work)
            finally:
                server.shutdown()

        if shallow:
            if capture.get("error"):
                raise RuntimeError(str(capture) + "\n" + (work / "browsertrix.log").read_text()[-3000:])
            assert set(capture["source_pages"].values()) == {origin + "/", origin + "/details/"}, capture
            assert not capture["sitemap_count"], capture
            assert capture["crawl_statistics"]["crawled"] == 2, capture
            report = verify(work)
            assert report["passed"] and report["fully_offline_pages"] == 2, report
            print(json.dumps({"shallow_pages": 2, "second_level_excluded": True, "offsite_excluded": True}))
            return

        if capture.get("error"):
            raise RuntimeError(json.dumps(capture))
        assert capture["sitemap_count"] == 1 and not capture["sitemap_missing"], capture
        assert capture["sitemap_outside_site"] == ["https://outside.invalid/"], capture
        good = verify(work)
        assert good["passed"] and good["opened_pages"] == good["fully_offline_pages"] == 4, good
        candidate = work / "candidate.zip"
        with ZipFile(candidate) as archive:
            mapping = json.loads(archive.read("url-map.json"))
            first = mapping[origin + "/photo.svg?v=one"]
            second = mapping[origin + "/photo.svg?v=two"]
            assert first != second
            archive.extractall(work / "offline")
        handler = partial(SimpleHTTPRequestHandler, directory=str(work / "offline"))
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            Thread(target=server.serve_forever, daemon=True).start()
            try:
                result = subprocess.check_output([
                    "node", str(ROOT / "tests" / "gallery.cjs"), f"http://127.0.0.1:{server.server_port}/",
                    first, second,
                ], text=True)
            finally:
                server.shutdown()
        with ZipFile(candidate) as source, ZipFile(work / "negative.zip", "w", ZIP_DEFLATED) as bad:
            for member in source.namelist():
                if member != first.lstrip("/"):
                    bad.writestr(member, source.read(member))
        shutil.copyfile(candidate, output / "fixture.zip")
        (work / "negative.zip").replace(candidate)
        negative = verify(work)
        assert not negative["passed"] and any(first in error for error in negative["resource_errors"]), negative
        report = {"capture": capture, "offline": good, "gallery": json.loads(result), "negative": negative}
        (output / "fixture.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"verified_pages": good["checked_pages"], "gallery": json.loads(result),
                          "negative_detected_missing_photo": not negative["passed"],
                          "sitemap_only_page": capture["sitemap_count"], "rate_guard": True}))


if __name__ == "__main__":
    run(Path(sys.argv[1]), "--shallow" in sys.argv[2:])

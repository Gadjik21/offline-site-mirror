"""Turn the recorded HTTP responses in a WACZ into files for a static server."""
import gzip
import json
import mimetypes
import re
import shutil
import sys
import zlib
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from zipfile import ZIP_DEFLATED, ZipFile


HTML = {"text/html", "application/xhtml+xml"}
SUFFIXES = {
    "text/css": ".css", "text/javascript": ".js", "application/javascript": ".js",
    "application/x-javascript": ".js", "application/json": ".json",
    "image/jpeg": ".jpg", "image/svg+xml": ".svg", "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico", "font/woff2": ".woff2", "font/woff": ".woff",
    "font/ttf": ".ttf", "application/font-woff": ".woff",
    "audio/mpeg": ".mp3", "application/wasm": ".wasm",
}
SAFE_COMPONENT = re.compile(r"[A-Za-z0-9_.~-]{1,100}\Z")
WINDOWS_DEVICE = re.compile(r"(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)
CHARSET = re.compile(rb"""charset\s*=\s*["']?\s*([-\w]+)""", re.I)
CSS_URL = re.compile(r"""(url\(\s*)(?:"([^"]*)"|'([^']*)'|([^)]*?))(\s*\))""", re.I)
CSS_IMPORT = re.compile(r"""(@import\s+)(["'])([^"']+)\2""", re.I)
SCRIPT_URL = re.compile(r"""(?P<quote>["'])(?P<url>(?:https?:)?//[^"'\\\s]+)(?P=quote)""")
CSS_CHARSET = re.compile(r"""^\ufeff?\s*@charset\s+["'][^"']+["']\s*;""", re.I)
SRCSET_URL = re.compile(r"\S+")
SRCSET_DESCRIPTOR = re.compile(r"[^,]*,?")


def canonical(value):
    address = urlsplit(value)
    if address.scheme not in ("http", "https") or not address.hostname:
        return value
    hostname = address.hostname.lower()
    if ":" in hostname:
        hostname = "[" + hostname + "]"
    port = address.port
    authority = hostname if port in (None, 80 if address.scheme == "http" else 443) else f"{hostname}:{port}"
    return urlunsplit((address.scheme, authority, address.path or "/", address.query, ""))


def headers(lines):
    return {name.strip().lower(): value.strip() for line in lines
            if ":" in line for name, value in [line.split(":", 1)]}


def unchunk(body):
    chunks = []
    offset = 0
    while True:
        end = body.index(b"\r\n", offset)
        size = int(body[offset:end].split(b";", 1)[0], 16)
        if size == 0:
            return b"".join(chunks)
        offset = end + 2
        chunks.append(body[offset:offset + size])
        offset += size
        if body[offset:offset + 2] != b"\r\n":
            raise ValueError("Invalid HTTP chunk delimiter")
        offset += 2


def response_body(body, fields):
    # Browsertrix usually records the decoded body and strips wire-encoding headers.
    if fields.get("transfer-encoding", "").lower() == "chunked":
        try:
            body = unchunk(body)
        except ValueError:
            pass
    encoding = fields.get("content-encoding", "").lower()
    if encoding in ("gzip", "x-gzip") and body.startswith(b"\x1f\x8b"):
        return gzip.decompress(body)
    if encoding == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            pass
    return body


def read_capture(archive, bodies):
    pages, responses, redirects, issues, duplicates = {}, {}, {}, {}, {}
    with ZipFile(archive) as capture:
        for member in capture.namelist():
            if member.startswith("pages/") and member.endswith(".jsonl"):
                for line in capture.read(member).splitlines():
                    row = json.loads(line)
                    if row.get("url", "").startswith(("http://", "https://")):
                        pages.setdefault(canonical(row["url"]), row.get("status"))
            if not member.startswith("archive/") or not member.endswith(".warc.gz"):
                continue
            with capture.open(member) as compressed, gzip.GzipFile(fileobj=compressed) as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    if not line.startswith(b"WARC/"):
                        raise ValueError("Invalid WARC record boundary")
                    raw_headers = []
                    while header := stream.readline().strip():
                        raw_headers.append(header.decode("latin-1"))
                    record = headers(raw_headers)
                    length = int(record["content-length"])
                    kind = record.get("warc-type")
                    source = record.get("warc-target-uri", "").strip("<>")
                    if kind not in {"response", "revisit"} or not source.startswith(("http://", "https://")):
                        stream.seek(length, 1)
                        continue
                    raw = stream.read(length)
                    if len(raw) != length:
                        raise ValueError("Truncated WARC response")
                    source = canonical(source)
                    if kind == "revisit":
                        duplicates[source] = (
                            record.get("warc-refers-to-target-uri", "").strip("<>"),
                            record.get("warc-payload-digest"),
                        )
                        continue
                    head, separator, body = raw.partition(b"\r\n\r\n")
                    if not separator:
                        continue
                    first, *fields = head.decode("latin-1").split("\r\n")
                    status_fields = first.split()
                    if len(status_fields) < 2 or not status_fields[1].isdecimal():
                        continue
                    status = int(status_fields[1])
                    fields = headers(fields)
                    if 300 <= status < 400 and fields.get("location"):
                        redirects[source] = canonical(urljoin(source, fields["location"]))
                    elif status >= 400:
                        issues[(source, status)] = {"url": source, "status": status}
                    elif status in (200, 203) and source not in responses:
                        content_type = fields.get("content-type", "")
                        mime = content_type.split(";", 1)[0].strip().lower()
                        if not mime or mime == "application/octet-stream":
                            mime = mimetypes.guess_type(urlsplit(source).path)[0] or mime
                        payload = response_body(body, fields)
                        location = bodies / sha256(source.encode()).hexdigest()
                        location.write_bytes(payload)
                        responses[source] = (mime, content_type, location, record.get("warc-payload-digest"))
    by_digest = {value[3]: value for value in responses.values() if value[3]}
    for source, (reference, digest) in duplicates.items():
        original = responses.get(canonical(reference)) or by_digest.get(digest)
        if original:
            responses.setdefault(source, original)
    return pages, responses, redirects, list(issues.values())


def decode(record):
    mime, content_type, file, _ = record
    body = file.read_bytes()
    charset = CHARSET.search(content_type.encode("latin-1")) or CHARSET.search(body[:2048])
    encoding = charset[1].decode("ascii") if charset else "utf-8-sig"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8-sig", errors="replace")


def suffix_for(url, mime):
    suffix = Path(urlsplit(url).path).suffix.lower()
    if mime in HTML:
        return suffix if suffix in (".html", ".htm") else ".html"
    if mime in SUFFIXES:
        return SUFFIXES[mime]
    if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) and suffix not in (".html", ".htm"):
        return suffix
    return mimetypes.guess_extension(mime, strict=False) or ".bin"


class Paths:
    def __init__(self, seed):
        self.hostname = urlsplit(seed).hostname
        self.files = {}
        self.dirs = set()

    def link(self, url, mime):
        page = mime in HTML
        path = self.allocate(url, mime, page)
        query = urlsplit(url).query
        # The filename identifies the captured response; page scripts still read location.search.
        return path + "?" + query if page and query else path

    def allocate(self, url, mime, page=False):
        address = urlsplit(url)
        suffix = suffix_for(url, mime)
        same_host = address.hostname == self.hostname
        parts = address.path.strip("/").split("/") if address.path.strip("/") else []
        safe = not address.query and not (same_host and parts and parts[0].lower() in {
            "_mirror", "start.txt", "mirror-manifest.json", "url-map.json"
        }) and all(SAFE_COMPONENT.fullmatch(part) and not
                   (part in (".", "..") or part.endswith(".") or WINDOWS_DEVICE.match(part))
                   for part in parts)
        if safe:
            natural = "/".join(parts)
            if page and not natural.lower().endswith((".html", ".htm")):
                natural = "/".join([*parts, "index.html"])
            elif not page:
                current_suffix = Path(natural).suffix.lower()
                if not natural or (current_suffix != suffix and
                                   mimetypes.guess_type(natural)[0] != mime):
                    natural = None
            if natural and not same_host:
                namespace = sha256(f"{address.scheme}://{address.netloc}".encode()).hexdigest()[:16]
                natural = f"_mirror/hosts/{namespace}/{natural}"
            if natural and self.reserve(natural):
                return "/" + natural
        group = "pages" if page else "assets"
        name = sha256(url.encode()).hexdigest()
        path = f"_mirror/{group}/{name}{suffix}"
        if not self.reserve(path):
            raise ValueError(f"Export path collision for {url}")
        return "/" + path

    def reserve(self, path):
        normalized = path.lower()
        parents = [str(item) for item in Path(normalized).parents if str(item) != "."]
        if normalized in self.files or normalized in self.dirs or any(p in self.files for p in parents):
            return False
        self.files[normalized] = path
        self.dirs.update(parents)
        return True


class Localizer:
    def __init__(self, mapping, redirects):
        self.mapping = mapping
        self.redirects = redirects
        self.mixed_content = set()

    def target(self, source):
        seen = set()
        while source not in self.mapping and source in self.redirects and source not in seen:
            seen.add(source)
            source = self.redirects[source]
        return self.mapping.get(source)

    def url(self, value, base):
        value = value.strip()
        if not value or value.startswith("#"):
            return value
        try:
            absolute = urljoin(base, value)
            if urlsplit(absolute).scheme not in ("http", "https"):
                return value
            source, fragment = urldefrag(absolute)
            target = canonical(source)
            localized = self.target(target)
            if not localized and urlsplit(base).scheme == "https" and target.startswith("http://"):
                localized = self.target("https://" + target[len("http://"):])
        except ValueError:
            return value
        return (localized + ("#" + fragment if fragment else "")) if localized else absolute

    def script(self, value, base):
        def localize(match):
            original = match["url"]
            # Keep unrecorded literals and templated URLs exactly as written.
            localized = self.url(original, base)
            if not localized.startswith("/") or localized.startswith("//"):
                return match[0]
            return match["quote"] + localized + match["quote"]

        return SCRIPT_URL.sub(localize, value)

    def css(self, value, base):
        value = CSS_CHARSET.sub("", value)
        value = CSS_URL.sub(
            lambda match: match[1] + '"' + self.url(
                next(part for part in match.groups()[1:4] if part is not None), base
            ).replace('"', '%22') + '"' + match[5], value
        )
        return CSS_IMPORT.sub(
            lambda match: match[1] + '"' + self.url(match[3], base).replace('"', '%22') + '"', value
        )

    def srcset(self, value, base):
        candidates = []
        pos = 0
        while pos < len(value):
            while pos < len(value) and (value[pos].isspace() or value[pos] == ","):
                pos += 1
            token = SRCSET_URL.match(value, pos)
            if not token:
                break
            url = token.group()
            pos = token.end()
            descriptor = ""
            if not url.endswith(","):
                tail = SRCSET_DESCRIPTOR.match(value, pos)
                descriptor = tail.group().rstrip(",").strip()
                pos = tail.end()
            candidate = self.url(url.rstrip(","), base)
            candidates.append(" ".join(filter(None, (candidate, descriptor))))
        return ", ".join(candidates)


URL_ATTRIBUTES = {
    "href", "src", "poster", "data", "action", "formaction", "background",
    "cite", "xlink:href", "data-src", "data-original", "data-lazy-src",
}


class BaseHref(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.source = source
        self.base = None

    def handle_starttag(self, tag, attrs):
        if tag == "base" and self.base is None and (href := dict(attrs).get("href")):
            self.base = urljoin(self.source, href)


class Page(HTMLParser):
    def __init__(self, localizer, base):
        super().__init__(convert_charrefs=False)
        self.localizer = localizer
        self.base = base
        self.parts = []
        self.in_style = False
        self.in_script = False
        self.head_script_added = False

    def render_tag(self, tag, attrs, closing):
        fields = dict(attrs)
        if tag == "base" and "href" in fields:
            attrs = [(key, value) for key, value in attrs if key != "href"]
            if not attrs:
                return
        if tag == "meta":
            equivalent = (fields.get("http-equiv") or "").lower()
            if equivalent == "content-security-policy":
                return
            if "charset" in fields or equivalent == "content-type":
                self.parts.append('<meta charset="utf-8">')
                return
        if tag == "script" and (src := fields.get("src")) and urlsplit(self.base).scheme == "https":
            try:
                target = self.localizer.url(src, self.base)
                if target.startswith("http://"):
                    # The capture browser blocks active HTTP scripts on HTTPS pages.
                    self.localizer.mixed_content.add(target)
                    attrs = [(key, val) for key, val in attrs if key not in {"src", "type"}]
                    attrs.append(("type", "application/x-offline-mixed-content"))
            except ValueError:
                pass
        if tag == "style":
            self.in_style = True
        self.parts.append("<" + tag)
        for key, value in attrs:
            if value is None:
                self.parts.append(" " + key)
                continue
            if key == "integrity" and (tag == "script" or
                                       (tag == "link" and "stylesheet" in (fields.get("rel") or "").lower().split())):
                href = fields.get("href") or fields.get("src")
                if href and self.localizer.url(href, self.base).startswith("/"):
                    continue
            if key in URL_ATTRIBUTES:
                value = self.localizer.url(value, self.base)
            elif key in {"srcset", "data-srcset", "imagesrcset"}:
                value = self.localizer.srcset(value, self.base)
            elif key == "style" or (key in {"fill", "stroke", "filter", "mask", "clip-path"} and "url(" in value):
                value = self.localizer.css(value, self.base)
            self.parts.append(f' {key}="{escape(value, quote=True)}"')
        self.parts.append(closing)
        if tag == "head" and not self.head_script_added:
            source = escape(self.base, quote=True)
            self.parts.append(f'<script src="/_mirror/requests.js" data-source="{source}"></script>')
            self.head_script_added = True
        if tag == "script":
            self.in_script = True

    def handle_starttag(self, tag, attrs):
        self.render_tag(tag, attrs, ">")

    def handle_startendtag(self, tag, attrs):
        self.render_tag(tag, attrs, "/>")

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False
        if tag == "script":
            self.in_script = False
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self.in_style:
            data = self.localizer.css(data, self.base)
        elif self.in_script:
            data = self.localizer.script(data, self.base)
        self.parts.append(data)

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.parts.append(f"<?{data}>")


def export(archive, work, config):
    bodies = work / "bodies"
    bodies.mkdir()
    pages, responses, redirects, issues = read_capture(archive, bodies)
    seed = canonical(config["seed"])
    sitemap = sorted({canonical(url) for url in config.get("sitemap_urls", [])})
    expected = sorted({canonical(url) for url in config["expected"]})
    hosts = {urlsplit(url).hostname for url in [seed, *sitemap, *config.get("additional_seeds", [])]}
    hosts.update(config.get("crawl_hosts", []))
    # A redirected seed is tracked even when Browsertrix indexes only its destination.
    requested = [url for url in pages if urlsplit(url).hostname in hosts]
    paths = Paths(seed)
    mapping = {}
    page_sources = []
    for source in dict.fromkeys([seed, *requested, *sitemap, *expected, *redirects.values()]):
        value = responses.get(source)
        if urlsplit(source).hostname in hosts and value and value[0] in HTML:
            mapping[source] = paths.link(source, value[0])
            page_sources.append(source)
    page_set = set(page_sources)
    for source, record in responses.items():
        if source not in mapping:
            mapping[source] = paths.link(source, record[0])
    localizer = Localizer(mapping, redirects)
    page_paths = {mapping[page] for page in page_set}
    issue_rows = sorted(issues, key=lambda issue: (issue["url"], issue["status"]))
    entry = localizer.target(seed)
    sitemap_missing = [url for url in sitemap if localizer.target(url) not in page_paths]
    manifest = {
        "entry": entry if entry in page_paths else (mapping[page_sources[0]] if page_sources else None),
        "seed_missing": entry not in page_paths,
        "pages": [mapping[url] for url in page_sources],
        "source_pages": {mapping[page]: page for page in page_sources},
        "source_hosts": sorted(hosts),
        "expected_count": len(expected) if expected else None,
        "sitemap_count": len(sitemap),
        "sitemap_missing": sitemap_missing,
        "expected_missing": [url for url in expected if localizer.target(url) not in
                             page_paths],
        "capture_missing": [url for url in requested if not localizer.target(url)],
        "found_pages": len(requested),
        "source_http_errors": issue_rows,
        "source_limits": [issue for issue in issue_rows if issue["status"] in {401, 403, 418, 429, 451, 503}],
        "archived_assets": len(mapping) - len(page_sources),
    }
    if not manifest["entry"]:
        return manifest
    site = work / "site"
    site.mkdir()
    for source, local in mapping.items():
        record = responses[source]
        destination = site / urlsplit(local).path.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if record[0] in HTML:
            original = decode(record)
            base = BaseHref(source)
            base.feed(original)
            parser = Page(localizer, base.base or source)
            parser.feed(original)
            parser.close()
            destination.write_text("".join(parser.parts), encoding="utf-8")
        elif record[0] == "text/css":
            destination.write_text(localizer.css(decode(record), source), encoding="utf-8")
        elif suffix_for(source, record[0]) in {".js", ".mjs"}:
            text = decode(record)
            edited = localizer.script(text, source)
            if edited == text:
                shutil.copyfile(record[2], destination)
            else:
                destination.write_text(edited, encoding="utf-8")
        else:
            shutil.copyfile(record[2], destination)

    if not (site / "index.html").exists():
        entry = escape(manifest["entry"], quote=True)
        (site / "index.html").write_text(
            f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={entry}">'
            f'<a href="{entry}">Open the offline site</a>', encoding="utf-8"
        )
    url_map = dict(mapping)
    url_map.update({url: path for url in redirects if (path := localizer.target(url))})
    routes = site / "_mirror"
    routes.mkdir(exist_ok=True)
    runtime = Path(__file__).with_name("local-requests.js").read_text(encoding="utf-8")
    (routes / "requests.js").write_text(
        "(() => { const routes = new Map(" + json.dumps(list(url_map.items())) + ");\n" +
        runtime + "\n})();\n", encoding="utf-8"
    )
    (site / "url-map.json").write_text(json.dumps(url_map, indent=2), encoding="utf-8")
    manifest["blocked_mixed_content"] = sorted(localizer.mixed_content)
    (site / "mirror-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (site / "START.txt").write_text(
        "Extract the ZIP. In the extracted folder run:\n"
        "  python -m http.server 8000 --bind 127.0.0.1\n"
        "Use python3 instead of python if needed. Open http://127.0.0.1:8000/.\n"
        "The archive contains local files and a map of captured URLs in url-map.json.\n",
        encoding="utf-8"
    )
    with ZipFile(work / "candidate.zip", "w", ZIP_DEFLATED) as candidate:
        for item in sorted(site.rglob("*")):
            if item.is_file():
                candidate.write(item, item.relative_to(site).as_posix())
    return manifest


if __name__ == "__main__":
    archive, config_path, work = map(Path, sys.argv[1:4])
    result = export(archive, work, json.loads(config_path.read_text(encoding="utf-8")))
    print(json.dumps(result))

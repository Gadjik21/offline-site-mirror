"""Discover public page URLs from the starting site's published sitemaps."""
import gzip
import io
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

from export_wacz import canonical

MAX_XML = 52_428_800


def same_site(url, domain):
    address = urlsplit(url)
    host = address.hostname or ""
    return (address.scheme in {"http", "https"} and address.username is None
            and (host == domain or host.endswith("." + domain)))


def discover(seed):
    address = urlsplit(seed)
    domain = address.hostname.removeprefix("www.")
    origin = f"{address.scheme}://{address.netloc}"

    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not same_site(newurl, domain):
                raise urllib.error.URLError("Sitemap redirects outside the site")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(Redirect())

    def fetch(url, limit):
        with opener.open(url, timeout=12) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Sitemap response exceeds the XML size limit")
        return data

    sources, skipped, warnings, pages = [], set(), [], set()
    roots = set()
    try:
        robots = fetch(origin + "/robots.txt", 512_000).decode("utf-8-sig", errors="replace")
        for line in robots.splitlines():
            field, sep, value = line.partition(":")
            if field.strip().lower() == "sitemap" and sep and value.strip():
                roots.add(urljoin(origin, value.strip()))
    except (OSError, ValueError):
        pass
    if not roots:
        roots.add(origin + "/sitemap.xml")
    pending = sorted(roots, reverse=True)
    seen = set()
    while pending:
        url = pending.pop()
        if url in seen:
            continue
        seen.add(url)
        if not same_site(url, domain):
            skipped.add(url)
            continue
        try:
            raw = fetch(url, MAX_XML)
            if raw.startswith(b"\x1f\x8b"):
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                    raw = stream.read(MAX_XML + 1)
                if len(raw) > MAX_XML:
                    raise ValueError("Decompressed sitemap exceeds the XML size limit")
            root = ElementTree.fromstring(raw)
            kind = root.tag.rsplit("}", 1)[-1]
            if kind not in {"urlset", "sitemapindex"}:
                raise ValueError("Unexpected sitemap element")
        except (OSError, ValueError, ElementTree.ParseError) as error:
            warnings.append({"url": url, "error": str(error)})
            continue
        sources.append(url)
        for entry in root:
            item = next((child.text for child in entry if child.tag.rsplit("}", 1)[-1] == "loc"), None)
            if not item:
                continue
            target = urljoin(url, item.strip())
            if not same_site(target, domain):
                skipped.add(target)
            elif kind == "sitemapindex":
                pending.append(target)
            else:
                try:
                    pages.add(canonical(target))
                except ValueError:
                    warnings.append({"url": target, "error": "Invalid page URL"})
    return {"sitemap_sources": sources, "sitemap_urls": sorted(pages),
            "sitemap_outside_site": sorted(skipped), "sitemap_warnings": warnings}

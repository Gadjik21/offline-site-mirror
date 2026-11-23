# Offline Site Mirror

Capture an allowed public site with Browsertrix and turn the recorded pages into a regular ZIP. After extraction, the copy runs from a static HTTP server on your computer. No archive viewer is required.

## Quick start

Install Python 3.9+ and start Docker with Linux containers. On Windows and macOS use Docker Desktop. On Linux use Docker Engine and check that your account can run Docker. The first run downloads the pinned Browsertrix image if needed.

- Windows: open mirror.cmd or run py -3 mirror.py.
- macOS and Linux: run python3 mirror.py.

Enter a URL or domain when prompted. To pass an address directly:

    python3 mirror.py https://quotes.toscrape.com/ --output site.zip

On Windows, use py -3 in place of python3. By default, Browsertrix saves the starting page and the pages it links to directly on the same hostname, including its www/bare variant. It records their resources, including CDN assets, and respects robots.txt. Repeated HTTP 429 responses from a host stop the crawl and preserve any partial result.

Common options:

- --full walks internal links recursively and reads the public sitemap when available. In this mode related hosts advertised by the sitemap are also eligible.

- --expected FILE verifies coverage against an independent list of HTTP(S) page URLs, one per line.
- --include PATTERN and --exclude PATTERN narrow page discovery to matching links on the starting host. --exclude can be repeated. Use --full --no-sitemap to follow links recursively without seeding the sitemap.
- --workers N defaults to 4. --limit N, --time-limit SECONDS and --quota SIZE bound a crawl. These limits apply only when supplied.

## Open the archive

Extract site.zip, open a terminal in the extracted folder and run:

    python3 -m http.server 8000 --bind 127.0.0.1

On Windows use py -3 -m http.server 8000 --bind 127.0.0.1. Open http://127.0.0.1:8000/. START.txt provides the same instructions, and url-map.json maps captured URLs to files.

## Verification and scope

A second Docker container extracts the ZIP and opens each exported page in Chromium with external networking disabled. The report records loaded images, fonts and stylesheets, local links, failed resources, external requests and stage timings. Add --expected to verify that known pages are all present. The report separates discovered pages, sitemap URLs that were missed, pages that opened, pages that fully passed offline, and internal links that still lead online.

A failed run returns a nonzero status. See site.failure.qa.json and the Browsertrix log. The tool publishes site.zip and site.qa.json only after successful offline verification. If an incomplete ZIP was produced, it is written as site.partial.zip for diagnosis.

Static pages and recorded read-only JavaScript requests can work offline. Interactions that require the original server, access-controlled content, uncaptured video, and some cross-origin or dynamically generated resources may lose functionality. Links outside the captured scope retain their original URL. Run copies locally and only mirror pages you are permitted to archive.

## Offline test results

Tested on Windows with Browsertrix 1.15.4. Chromium opened the extracted ZIP with external networking disabled. The page counts refer to the expected URLs in the test lists.

| Test site | Expected pages opened offline | Additional checks |
| --- | ---: | --- |
| Books to Scrape, Default category | 160 / 160 | 152 / 152 covers, pagination, and book detail links |
| Quotes to Scrape, listings and authors | 60 / 60 | Author links, pagination, and stylesheets |

To repeat both runs from the repository root in PowerShell, use the checked-in URL lists as the crawl scope and the coverage check.

```powershell
foreach ($site in 'books', 'quotes') {
    $expected = "tests/$site-expected.txt"
    $pages = Get-Content $expected
    $scope = ($pages | ForEach-Object { [regex]::Escape($_.TrimEnd('/')) + '/?' }) -join '|'
    py -3 mirror.py $pages[0] --full --no-sitemap --expected $expected --include "(?:$scope)$" --output "$site.zip"
}
```

## Local regression

From the source directory on macOS or Linux, run:

    docker run --rm --network none --shm-size 1g --mount "type=bind,source=$PWD,target=/mirror-tool,readonly" --entrypoint python3 webrecorder/browsertrix-crawler:1.15.4 /mirror-tool/tests/fixture.py /tmp

Repeat the test with --shallow after /tmp to verify that the one-hop crawl omits deeper pages and sites outside the scope. The full fixture captures a small site with two query variants, local navigation, a recorded fetch, and a gallery. It then verifies the ZIP with the origin stopped and checks that removing a required image fails verification.

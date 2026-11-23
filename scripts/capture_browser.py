"""Run Browsertrix in its own container and export the resulting WACZ."""
import ipaddress
import json
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from export_wacz import export
from sitemap import discover


def run_crawler(command, cwd, log, timeout=None):
    """Stop the crawl when one host repeatedly asks it to slow down."""
    deadline = time.monotonic() + timeout if timeout else None
    limited = {}
    with subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT) as process:
        with open(log.name, encoding="utf-8") as tail:
            reason = None
            while process.poll() is None:
                now = time.monotonic()
                for line in tail:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(item, dict) or item.get("context") != "pageStatus":
                        continue
                    details = item.get("details")
                    if not isinstance(details, dict) or str(details.get("status")) != "429":
                        continue
                    source = details.get("page")
                    if not source:
                        continue
                    host = urlsplit(source).hostname
                    recent = limited.setdefault(host, {})
                    recent[source] = now
                    limited[host] = {url: at for url, at in recent.items() if now - at <= 30}
                    if len(limited[host]) >= 4:
                        reason = f"The source host {host} repeatedly returned HTTP 429."
                        break
                if deadline is not None and now >= deadline:
                    reason = reason or "The capture exceeded its shutdown deadline."
                if reason:
                    try:
                        process.send_signal(signal.SIGINT)
                        process.wait(timeout=120)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break
                time.sleep(0.5)
            return process.wait(), reason


def collect(work):
    config = json.loads((work / "profile.json").read_text(encoding="utf-8"))
    result = {"engine": "Browsertrix", "workers": config["workers"], "seed": config["seed"]}
    with tempfile.TemporaryDirectory(prefix="browsertrix-") as tmp:
        scratch = Path(tmp)
        seed = config["seed"]
        sitemap = discover(seed) if config.get("discover_sitemap") else {"sitemap_urls": []}
        result.update(sitemap)
        sitemap_urls = sitemap["sitemap_urls"]
        config["sitemap_urls"] = sitemap_urls
        seeds = list(dict.fromkeys([seed, *sitemap_urls]))
        seed_file = scratch / "seeds.txt"
        seed_file.write_text("\n".join(seeds) + "\n", encoding="utf-8")
        authorities = {urlsplit(url).netloc for url in seeds}
        if config.get("depth") == 1:
            address = urlsplit(seed)
            host = address.hostname
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if "." in host:
                    alias = host[4:] if host.startswith("www.") else "www." + host
                    authorities.add(alias if address.port is None else f"{alias}:{address.port}")
        config["crawl_hosts"] = [urlsplit("https://" + authority).hostname for authority in authorities]
        host_scope = rf"^https?://(?:{'|'.join(map(re.escape, sorted(authorities)))})(?:/|$)"
        include = config.get("include")
        scope = rf"(?={host_scope})(?:{include})" if include else host_scope
        command = [
            "crawl", "--seedFile", str(seed_file), "--scopeType", "custom", "--include", scope,
            "--useRobots", "--workers", str(config["workers"]), "--collection", "mirror",
            "--generateWACZ", "--behaviors", "autoscroll", "autofetch",
            "--pageLoadTimeout", "40", "--behaviorTimeout", "15", "--postLoadDelay", "1",
            "--saveState", "never",
        ]
        for key, flag in (("depth", "--depth"), ("limit", "--pageLimit"),
                          ("time_limit", "--timeLimit"), ("quota", "--sizeLimit")):
            if config.get(key) is not None:
                command.extend([flag, str(config[key])])
        exclude = [r"^https?://[^/]+/(?:[^/?]+/)*(?:logout|log-out|signout|sign-out)(?:[/?#]|$)",
                   *config["exclude"]]
        command += ["--exclude", "(?:" + ")|(?:".join(exclude) + ")"]
        started = time.monotonic()
        with (work / "browsertrix.log").open("w+", encoding="utf-8") as log:
            timeout = config["time_limit"] + 120 if config.get("time_limit") else None
            result["crawler_exit"], stop_reason = run_crawler(command, scratch, log, timeout)
            if stop_reason:
                result["crawl_stopped_reason"] = stop_reason
            log.seek(0)
            capture_events = []
            for line in log:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("message") == "Crawl statistics":
                    result["crawl_statistics"] = item.get("details")
                elif item.get("logLevel") in {"warn", "error"}:
                    capture_events.append({"message": item.get("message"), "details": item.get("details")})
            result["crawl_diagnostics"] = capture_events[-30:]
        result["capture_seconds"] = round(time.monotonic() - started, 3)

        archive = scratch / "collections" / "mirror" / "mirror.wacz"
        if not archive.is_file():
            result["error"] = "Browsertrix did not write an archive."
            return result
        try:
            started = time.monotonic()
            result.update(export(archive, scratch, config))
            result["export_seconds"] = round(time.monotonic() - started, 3)
        except (OSError, ValueError, KeyError) as error:
            result["error"] = f"Cannot export the capture: {error}"
            return result
        stats = result.get("crawl_statistics") or {}
        result["crawl_limit_reached"] = bool((stats.get("limit") or {}).get("hit"))
        result["crawl_frontier_remaining"] = max(0, stats.get("total", 0) - stats.get("crawled", 0))
        if (scratch / "candidate.zip").is_file():
            shutil.copyfile(scratch / "candidate.zip", work / "candidate.zip")
        if stop_reason:
            result["error"] = stop_reason
        elif result["expected_missing"]:
            result["error"] = f"Missing {len(result['expected_missing'])} expected page(s): {result['expected_missing'][0]}"
        elif result["seed_missing"]:
            result["error"] = "The starting page did not return a captured HTML response."
        elif result["capture_missing"]:
            result["error"] = f"Browsertrix did not capture HTML for {len(result['capture_missing'])} indexed page(s)."
        elif result["sitemap_missing"]:
            result["error"] = f"Missing {len(result['sitemap_missing'])} sitemap page(s)."
        elif result["crawler_exit"] != 0:
            result["error"] = f"Browsertrix exited with status {result['crawler_exit']}."
        elif result["crawl_limit_reached"] or result["crawl_frontier_remaining"]:
            result["error"] = "The crawl stopped before exhausting its page queue."
        return result


if __name__ == "__main__":
    workspace = Path(sys.argv[1])
    try:
        output = collect(workspace)
    except (OSError, ValueError, KeyError) as error:
        output = {"error": str(error)}
    (workspace / "capture.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
    if output.get("error") and not (workspace / "candidate.zip").is_file():
        sys.exit(1)

"""Capture a public website with Browsertrix and verify a static ZIP offline."""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


IMAGE = "webrecorder/browsertrix-crawler:1.15.4"
ROOT = Path(__file__).resolve().parent


def address(value):
    value = value.strip().lstrip("\ufeff").strip()
    if value and "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.username is not None or parsed.fragment or any(c.isspace() for c in value)):
            raise ValueError
        port = parsed.port
        hostname = parsed.hostname.encode("idna").decode("ascii")
        if ":" in hostname:
            hostname = "[" + hostname + "]"
        authority = hostname if port is None else f"{hostname}:{port}"
    except ValueError:
        raise ValueError("Enter an HTTP(S) address without credentials or a fragment.") from None
    return urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))


def size(value):
    match = re.fullmatch(r"([1-9][0-9]*)([kmg]?)", value, re.I)
    if not match:
        raise argparse.ArgumentTypeError("Use a size such as 250m.")
    return int(match[1]) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[match[2].lower()]


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Use a positive integer.")
    return number


def json_file(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def run(config, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    report_file = output.with_suffix(".qa.json")
    failure_file = output.with_suffix(".failure.qa.json")
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=".offline-mirror-", dir=output.parent) as tmp:
        work = Path(tmp)
        (work / "profile.json").write_text(json.dumps(config), encoding="utf-8")
        stage = "capture"
        timings = {}

        def docker(network, script):
            extra = []
            if network == "bridge" and urlsplit(config["seed"]).hostname == "host.docker.internal":
                if sys.platform.startswith("linux"):
                    extra = ["--add-host", "host.docker.internal:host-gateway"]
            subprocess.run([
                "docker", "run", "--rm", "--network", network, "--shm-size", "1g",
                "--mount", f"type=bind,source={ROOT},target=/mirror-tool,readonly",
                "--mount", f"type=bind,source={work},target=/run-data", *extra,
                "--entrypoint", "python3", IMAGE, f"/mirror-tool/scripts/{script}", "/run-data",
            ], check=True)

        try:
            print(f"Capturing {config['seed']} with Browsertrix ({config['workers']} workers)", flush=True)
            docker("bridge", "capture_browser.py")
            stage = "offline verification"
            print("Opening extracted ZIP in Chromium with external networking disabled", flush=True)
            started_qa = time.monotonic()
            docker("none", "verify.py")
            timings["verify_seconds"] = round(time.monotonic() - started_qa, 3)
            result = json_file(work / "qa.json")
            capture = json_file(work / "capture.json")
            if capture.get("error"):
                raise ValueError(capture["error"])
            if not result.get("passed"):
                raise ValueError(result.get("error", "The offline ZIP did not pass verification."))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            capture = json_file(work / "capture.json")
            details = json_file(work / "qa.json")
            failure = {**capture, **details, **timings, "passed": False, "stage": stage,
                       "error": details.get("error") or capture.get("error") or str(error),
                       "total_seconds": round(time.monotonic() - start, 3)}
            if (work / "browsertrix.log").is_file():
                log = output.with_suffix(".browsertrix.log")
                shutil.copyfile(work / "browsertrix.log", log)
                failure["crawl_log"] = log.name
            partial = work / "candidate.zip"
            if partial.is_file():
                partial_path = output.with_suffix(".partial.zip")
                staged_partial = work / "publish-partial.zip"
                shutil.copyfile(partial, staged_partial)
                staged_partial.replace(partial_path)
                failure["partial_archive"] = partial_path.name
            failure_file.write_text(json.dumps(failure, indent=2) + "\n", encoding="utf-8")
            print(f"Mirror failed during {stage}: {failure['error']}", file=sys.stderr)
            print(f"Failure report: {failure_file}", file=sys.stderr)
            return 1

        result = {**json_file(work / "capture.json"), **result, **timings,
                  "total_seconds": round(time.monotonic() - start, 3)}
        candidate = work / "candidate.zip"
        # Publish using caller-owned files, including under a rootful Linux Docker daemon.
        staged_zip = work / "publish.zip"
        staged_report = work / "publish.json"
        shutil.copyfile(candidate, staged_zip)
        staged_report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        staged_zip.replace(output)
        staged_report.replace(report_file)
        failure_file.unlink(missing_ok=True)
    print(f"Verified ZIP: {output}")
    print(f"Offline QA report: {report_file}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", help="HTTP(S) URL or domain to mirror")
    parser.add_argument("--include", help="Browsertrix regex for in-scope page URLs")
    parser.add_argument("--exclude", action="append", default=[], help="Skip matching page URLs; repeatable")
    parser.add_argument("--expected", type=Path, help="Expected page URLs, one per line")
    parser.add_argument("--quota", type=size, help="Optional capture size limit, for example 250m")
    parser.add_argument("--workers", type=positive, default=4)
    parser.add_argument("--limit", type=positive, help="Optional page limit")
    parser.add_argument("--time-limit", type=positive, help="Optional capture time limit in seconds")
    parser.add_argument("--full", action="store_true", help="Follow links recursively and read the site's sitemap")
    parser.add_argument("--no-sitemap", action="store_true", help="Limit a full crawl to link discovery")
    parser.add_argument("--output", type=Path, default=Path("site.zip"))
    args = parser.parse_args()
    try:
        seed = address(args.url if args.url is not None else input("Какой сайт скопировать? / Website URL: "))
        expected = []
        if args.expected:
            for line in args.expected.read_text(encoding="utf-8-sig").splitlines():
                if line.strip():
                    expected.append(address(line))
        if any(urlsplit(url).hostname != urlsplit(seed).hostname for url in expected):
            parser.error("--expected page URLs must use the starting hostname.")
    except (EOFError, ValueError, OSError) as error:
        parser.error(str(error) or "A website URL is required.")
    except KeyboardInterrupt:
        return 130
    if args.output.suffix.lower() != ".zip":
        parser.error("--output must end in .zip")
    config = {"seed": seed, "include": args.include, "exclude": args.exclude,
              "expected": expected, "quota": args.quota, "workers": args.workers,
              "limit": args.limit, "time_limit": args.time_limit,
              "depth": None if args.full else 1,
              "discover_sitemap": args.full and not (args.no_sitemap or args.include or args.exclude)}
    try:
        return run(config, args.output.resolve())
    except OSError as error:
        print(f"Mirror failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

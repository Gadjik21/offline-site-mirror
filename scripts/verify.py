"""Verify the extracted ZIP in a container with no external networking."""
import json
import subprocess
import sys
import tempfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from zipfile import BadZipFile, ZipFile


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def verify(work):
    ipv4 = Path("/proc/net/route").read_text().splitlines()[1:]
    ipv6 = Path("/proc/net/ipv6_route").read_text().splitlines()
    routes = {row.split()[0] for row in ipv4 if row.strip()}
    routes.update(row.split()[-1] for row in ipv6 if row.strip())
    if routes - {"lo"}:
        raise ValueError(f"Expected no non-loopback routes; found {sorted(routes)}")
    candidate = work / "candidate.zip"
    with tempfile.TemporaryDirectory(prefix="mirror-qa-") as tmp:
        extracted = Path(tmp)
        with ZipFile(candidate) as archive:
            if bad := archive.testzip():
                raise ValueError(f"Invalid ZIP member: {bad}")
            archive.extractall(extracted)
        handler = partial(QuietHandler, directory=str(extracted))
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            Thread(target=server.serve_forever, daemon=True).start()
            try:
                origin = f"http://127.0.0.1:{server.server_port}"
                result = subprocess.run([
                    "node", "/mirror-tool/scripts/browser-qa.cjs", origin,
                    str(extracted / "mirror-manifest.json"),
                ], check=True, text=True, stdout=subprocess.PIPE)
            finally:
                server.shutdown()
    report = json.loads(result.stdout)
    report["routed_interfaces"] = sorted(routes)
    report["archive_bytes"] = candidate.stat().st_size
    return report


if __name__ == "__main__":
    workspace = Path(sys.argv[1])
    try:
        report = verify(workspace)
    except (ValueError, OSError, BadZipFile, subprocess.SubprocessError) as error:
        report = {"passed": False, "error": str(error)}
    (workspace / "qa.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

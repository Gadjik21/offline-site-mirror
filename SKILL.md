---
name: offline-site-mirror
description: Capture and verify a regular offline site ZIP with Browsertrix and Chromium.
---

# Offline Site Mirror

Run `python3 mirror.py URL --output site.zip`. On Windows use `py -3 mirror.py` or `mirror.cmd`. Read README.md for scope options and the report format.

Use `--expected FILE` when the user needs independently verified coverage. A passing report verifies the exported pages and resources that Chromium exercised without external networking. Check actual interactions when a task requires them. Report source throttling, missing pages, and runtime failures separately.

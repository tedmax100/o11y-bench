#!/usr/bin/env python3
"""Generate the storyboard's images with Gemini, into the paths the renderer expects.

`story-to-handdrawn-video` was built around an agent runtime that has its own
image tool (Codex Image2). Claude Code has no such tool, and the importer only
checks that the files exist at `output_master` — it does not care which model
drew them. So this reads the same job manifest and fills those paths.

The order in the manifest matters: the character reference is job zero, and every
scene lists it under `references`, which is what keeps one protagonist looking
like the same protagonist across eighteen frames.

    python3 gen_images.py --only character_reference     # look before spending
    python3 gen_images.py --all
    python3 gen_images.py --all --model gemini-2.5-flash-image   # cheaper
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _post(model: str, key: str, parts: list[dict], timeout: float) -> bytes:
    body = json.dumps(
        {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "1:1"}},
        }
    ).encode()
    req = urllib.request.Request(
        ENDPOINT.format(model=model),
        data=body,
        headers={"content-type": "application/json", "x-goog-api-key": key},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.load(r)
    for cand in payload.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    # A refusal comes back as text, and it is worth showing verbatim rather than
    # reporting "no image": it usually names the constraint that tripped.
    texts = [
        p.get("text", "")
        for c in payload.get("candidates", [])
        for p in c.get("content", {}).get("parts", [])
    ]
    raise RuntimeError("no image in response. model said: " + " ".join(t for t in texts if t)[:400])


def generate(job: dict, key: str, model: str, timeout: float, character: str = "") -> Path:
    prompt = Path(job["prompt_file"]).read_text()
    # The bundled prompts say "draw ONLY the protagonists described below" and then
    # describe nobody: they were written for a runtime that already holds the story
    # in its conversation. Sending the file alone leaves the model to invent a cast,
    # which is how a story about an agent got two generic villagers.
    if character:
        prompt += f"\nProtagonist description (authoritative): {character}\n"
    parts: list[dict] = []
    for ref in job.get("references") or []:
        p = Path(ref)
        if not p.exists():
            raise FileNotFoundError(f"reference not generated yet: {p.name}")
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(p.read_bytes()).decode()}})
    parts.append({"text": prompt})

    out = Path(job["output_master"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(_post(model, key, parts, timeout))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", default="/home/nathan/Project/o11y-bench/demo-services/"
                                      "story-to-handdrawn-video/codex-image-jobs.json")
    ap.add_argument("--model", default="gemini-3-pro-image")
    ap.add_argument("--only", action="append", default=[], help="job id; repeatable")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true", help="redraw jobs whose file already exists")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--key", default="", help="defaults to the cluster secret")
    ap.add_argument("--character", default="", help="who the recurring protagonist is; "
                                                    "the bundled prompts never say")
    args = ap.parse_args()

    key = args.key
    if not key:
        import subprocess

        key = base64.b64decode(
            subprocess.check_output(
                ["kubectl", "-n", "demo", "get", "secret", "aiops-agent-secrets",
                 "-o", "jsonpath={.data.google-api-key}"]
            )
        ).decode().strip()

    jobs = json.loads(Path(args.jobs).read_text())["jobs"]
    if args.only:
        jobs = [j for j in jobs if j["id"] in args.only]
    elif not args.all:
        ap.error("pass --all or --only <id>")

    failed = []
    for job in jobs:
        out = Path(job["output_master"])
        if out.exists() and not args.force:
            print(f"  skip {job['id']} (already drawn)")
            continue
        for attempt in (1, 2, 3):
            try:
                path = generate(job, key, args.model, args.timeout, args.character)
                print(f"  ok   {job['id']} → {path.name} ({path.stat().st_size // 1024} KB)")
                break
            except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as e:
                detail = e.read().decode()[:200] if isinstance(e, urllib.error.HTTPError) else str(e)[:200]
                print(f"  retry {job['id']} ({attempt}/3): {detail}")
                time.sleep(4 * attempt)
        else:
            failed.append(job["id"])
    print(f"\ndone. failed: {failed or 'none'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

"""What: writes and checks the image manifests that carry the tested image digests from CI to the VM (D-072).
Use when: the images workflow (.github/workflows/images.yml, subcommand `images`, after `docker buildx bake --push`), the release workflow (subcommand `release`)
  and scripts/deploy/deploy.sh (subcommand `env`, before any image is pulled). See scripts/deploy/DEPLOY.md section 3.
Inputs: `images --bake-metadata FILE --revision SHA --repo REPO --name NAME [--run-url URL] --out FILE`;
  `release --images FILE --tag TAG --revision SHA --out FILE`; `env --manifest FILE --tag TAG [--repo REPO]`.
Output: `images` and `release` write JSON files; `env` prints KEY=VALUE lines (AGENT_IMAGE, CORE_IMAGE, KNOWLEDGE_IMAGE,
  WEB_IMAGE, RELEASE_REVISION) for deploy.sh. Exit 1 with one `release_manifest: ...` line on stderr on any invalid input.
Side effects: writes only the --out file. Never calls docker, git or the network.
Role: the one place a manifest is formed and validated, so CI, the release step and the deploy agree on one format (schema 1)."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_REPO = "ghcr.io/aarvingeorge/customer-support-voice-agent"
# image name -> the Compose service CI builds it from (compose.yaml build section); the migrate, seed and ingest jobs
# use the same Dockerfile and SERVICE argument, so they share these four images (scripts/deploy/compose.release.yaml).
IMAGES = {"agent": "agent_a", "core": "core", "knowledge": "knowledge", "web": "web"}
ENV_NAMES = {"agent": "AGENT_IMAGE", "core": "CORE_IMAGE", "knowledge": "KNOWLEDGE_IMAGE", "web": "WEB_IMAGE"}
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
RELEASE_TAG = re.compile(r"^r-[0-9]{2}(\.[0-9]+)?$")
REPO = re.compile(r"^[a-z0-9.:-]+(/[a-z0-9._-]+)+$")


class ManifestError(Exception):
    """What: an invalid manifest or input. Use when: raised by every check here. Inputs: a message. Output: none.
    Side effects: none. Role: main() turns it into exit 1 and one stderr line."""


def _need(cond: bool, msg: str) -> None:
    """What: raise ManifestError(msg) unless cond. Use when: every validation. Inputs: condition, message. Output: none.
    Side effects: none. Role: keeps the checks one line each."""
    if not cond:
        raise ManifestError(msg)


def _load(path: str) -> dict[str, Any]:
    """What: read a JSON object from a file. Use when: reading bake metadata or a manifest. Inputs: path. Output: dict.
    Side effects: reads a file. Role: one error message for a missing or malformed file."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise ManifestError(f"cannot read {path}: {e}") from e
    _need(isinstance(data, dict), f"{path} is not a JSON object")
    return data


def build_images(meta: dict[str, Any], revision: str, repo: str, name: str, run_url: str) -> dict[str, Any]:
    """What: turn `docker buildx bake --metadata-file` output into an images manifest. Use when: CI after the push.
    Inputs: bake metadata, the commit sha, the image repo prefix, the pushed tag name (the sha), the run URL.
    Output: dict with schema, kind "images", revision, repo, run_url and per image digest, ref (repo/name@digest) and tag.
    Side effects: none. Role: the digests the gates' run pushed are the only ones a release may name."""
    _need(bool(REVISION.match(revision)), f"revision must be a 40-character commit sha, got {revision!r}")
    _need(bool(REPO.match(repo)), f"repo must be a lowercase registry path, got {repo!r}")
    images: dict[str, dict[str, str]] = {}
    for image, target in IMAGES.items():
        entry = meta.get(target)
        _need(isinstance(entry, dict), f"bake metadata has no target {target} (image {image})")
        assert isinstance(entry, dict)
        digest = str(entry.get("containerimage.digest", ""))
        _need(bool(DIGEST.match(digest)), f"target {target}: no sha256 digest in the bake metadata (was it pushed?)")
        tag = f"{repo}/{image}:{name}"
        pushed = str(entry.get("image.name", "")).split(",")
        _need(tag in pushed, f"target {target}: pushed as {pushed}, expected {tag}")
        images[image] = {"digest": digest, "ref": f"{repo}/{image}@{digest}", "tag": tag}
    return {"schema": 1, "kind": "images", "revision": revision, "repo": repo, "run_url": run_url, "images": images}


def check_images(m: dict[str, Any], repo: str | None = None) -> None:
    """What: validate an images or release manifest's image list. Use when: before a release is written and before a deploy.
    Inputs: manifest dict, the expected repo (None: the manifest's own). Output: none (raises ManifestError).
    Side effects: none. Role: every image is present and pinned by digest under the expected repo, nothing else is accepted."""
    _need(m.get("schema") == 1, f"unknown manifest schema {m.get('schema')!r} (expected 1)")
    _need(bool(REVISION.match(str(m.get("revision", "")))), "manifest revision is not a 40-character commit sha")
    want_repo = repo or str(m.get("repo", ""))
    _need(m.get("repo") == want_repo, f"manifest repo {m.get('repo')!r} is not {want_repo!r}")
    imgs = m.get("images")
    _need(isinstance(imgs, dict) and set(imgs) == set(IMAGES), f"manifest must list exactly the images {sorted(IMAGES)}")
    assert isinstance(imgs, dict)
    for image in IMAGES:
        e = imgs[image]
        _need(isinstance(e, dict), f"image {image}: entry is not an object")
        digest = str(e.get("digest", ""))
        _need(bool(DIGEST.match(digest)), f"image {image}: digest is not sha256:<64 hex>")
        _need(e.get("ref") == f"{want_repo}/{image}@{digest}", f"image {image}: ref must be {want_repo}/{image}@<digest> (a tag is mutable)")


def build_release(images: dict[str, Any], tag: str, revision: str, now: datetime | None = None) -> dict[str, Any]:
    """What: a release manifest from an images manifest. Use when: the release workflow. Inputs: images manifest, release tag
    (r-NN or r-NN.N, D-072), the sha the tag points at, the time (default now, UTC). Output: dict with kind "release", tag and
    released_at added. Side effects: none. Role: binds a release tag to the digests the gates' run pushed for that sha."""
    _need(images.get("kind") == "images", "input is not an images manifest")
    check_images(images)
    _need(bool(RELEASE_TAG.match(tag)), f"release tag must look like r-01 or r-01.1 (D-072), got {tag!r}")
    _need(images["revision"] == revision, f"the images were built from {images['revision']}, not from the tag's commit {revision}")
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {**images, "kind": "release", "tag": tag, "released_at": stamp}


def release_env(m: dict[str, Any], tag: str, repo: str) -> list[str]:
    """What: validate a release manifest for a deploy and give the Compose image variables. Use when: deploy.sh before a pull.
    Inputs: manifest dict, the tag being deployed, the expected repo. Output: KEY=VALUE lines. Side effects: none.
    Role: the deploy refuses a manifest for another tag, an unpinned image or a foreign registry before touching anything."""
    _need(m.get("kind") == "release", "not a release manifest (kind must be release)")
    _need(m.get("tag") == tag, f"manifest is for tag {m.get('tag')!r}, not {tag!r}")
    check_images(m, repo)
    lines = [f"{ENV_NAMES[i]}={m['images'][i]['ref']}" for i in IMAGES]
    return [*lines, f"RELEASE_REVISION={m['revision']}"]


def _write(path: str, data: dict[str, Any]) -> None:
    """What: write JSON with a trailing newline. Use when: images and release. Inputs: path, dict. Output: none.
    Side effects: writes the file. Role: stable, reviewable output."""
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    """What: the command line (images, release, env). Use when: CI, the release workflow, deploy.sh. Inputs: argv.
    Output: exit code 0, or 1 with one stderr line. Side effects: see the module descriptor. Role: entry point."""
    p = argparse.ArgumentParser(prog="release_manifest")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("images")
    a.add_argument("--bake-metadata", required=True)
    a.add_argument("--revision", required=True)
    a.add_argument("--repo", required=True)
    a.add_argument("--name", required=True, help="the tag pushed (the commit sha)")
    a.add_argument("--run-url", default="")
    a.add_argument("--out", required=True)
    r = sub.add_parser("release")
    r.add_argument("--images", required=True)
    r.add_argument("--tag", required=True)
    r.add_argument("--revision", required=True)
    r.add_argument("--out", required=True)
    e = sub.add_parser("env")
    e.add_argument("--manifest", required=True)
    e.add_argument("--tag", required=True)
    e.add_argument("--repo", default=DEFAULT_REPO)
    args = p.parse_args(argv)
    try:
        if args.cmd == "images":
            _write(args.out, build_images(_load(args.bake_metadata), args.revision, args.repo, args.name, args.run_url))
        elif args.cmd == "release":
            _write(args.out, build_release(_load(args.images), args.tag, args.revision))
        else:
            print("\n".join(release_env(_load(args.manifest), args.tag, args.repo)))
    except ManifestError as err:
        print(f"release_manifest: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

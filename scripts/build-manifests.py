#!/usr/bin/env python3
"""
Render this repo the way the clusters' kustomize-controller does, so CI can
validate what would actually be applied.

  ./scripts/build-manifests.py OUT_DIR

Each Flux Kustomization in profiles/ is built with `flux build kustomization
--dry-run --strict-substitute`, which generates the kustomization.yaml Flux
would and runs Flux's own post-build substitution. Dry-run skips substituteFrom,
so the values the clusters get from flux-values and flux-secrets come from
placeholder-values.yaml instead. Strict mode fails on a variable that has
neither a placeholder nor a default, which catches a misspelled name that Flux
would otherwise substitute with an empty string.

Every kustomize tree under profiles/ and bootstrap/ is built as well, since
clusters point Flux at those directly.

One rendered file per build is written to OUT_DIR. Requires the flux CLI and
PyYAML.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
PLACEHOLDERS = Path(__file__).resolve().parent / "placeholder-values.yaml"
TREES = ("profiles", "bootstrap")


def flux_kustomizations():
    """Yield (name, path, Kustomization) for each Flux Kustomization in profiles/."""
    for f in sorted((REPO / "profiles").rglob("*.yaml")):
        for doc in yaml.safe_load_all(f.read_text()):
            if (
                doc
                and doc.get("kind") == "Kustomization"
                and doc["apiVersion"].startswith("kustomize.toolkit.fluxcd.io/")
            ):
                yield doc["metadata"]["name"], doc["spec"]["path"], doc


def kustomize_trees():
    """Yield (name, path, Kustomization) for each kustomize tree under TREES.

    The Kustomization is a stand-in so flux build can render the tree; these
    directories are plain kustomize and use no substitution.
    """
    for tree in TREES:
        for k in sorted((REPO / tree).rglob("kustomization.yaml")):
            rel = k.parent.relative_to(REPO).as_posix()
            name = rel.replace("/", "-")
            yield name, f"./{rel}", {
                "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
                "kind": "Kustomization",
                "metadata": {"name": name, "namespace": "flux-system"},
                "spec": {
                    "interval": "1h",
                    "path": f"./{rel}",
                    "prune": True,
                    "sourceRef": {"kind": "GitRepository", "name": "flux-core"},
                },
            }


def with_placeholders(kustomization, placeholders):
    """Return a copy whose inline substitute map also carries the placeholders."""
    spec = dict(kustomization["spec"])
    post_build = dict(spec.get("postBuild") or {})
    post_build.pop("substituteFrom", None)
    # Inline values are what the clusters use, so they win over placeholders.
    post_build["substitute"] = {**placeholders, **post_build.get("substitute", {})}
    spec["postBuild"] = post_build
    return {**kustomization, "spec": spec}


def build(name, path, kustomization, out_dir):
    """Render one Kustomization into out_dir. Returns flux's error output, or None."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as tmp:
        yaml.safe_dump(kustomization, tmp)
        tmp.flush()
        result = subprocess.run(
            [
                "flux", "build", "kustomization", name,
                "--path", str(REPO / path),
                "--kustomization-file", tmp.name,
                "--dry-run",
                "--strict-substitute",
            ],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        return result.stderr.strip() or f"flux exited {result.returncode}"
    (out_dir / f"{name}.yaml").write_text(result.stdout)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("out_dir", type=Path, help="directory for rendered manifests")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    placeholders = yaml.safe_load(PLACEHOLDERS.read_text())
    builds = [
        (name, path, with_placeholders(k, placeholders))
        for name, path, k in flux_kustomizations()
    ]
    builds += list(kustomize_trees())

    failed = 0
    for name, path, kustomization in builds:
        error = build(name, path, kustomization, args.out_dir)
        if error is None:
            print(f"ok    {name} ({path})")
            continue
        failed += 1
        print(f"FAIL  {name} ({path})\n{error}\n")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            # Annotations need newlines escaped to stay one message.
            message = error.replace("%", "%25").replace("\n", "%0A")
            print(f"::error title=flux build {name}::{message}")

    print(f"\n{len(builds) - failed}/{len(builds)} built")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

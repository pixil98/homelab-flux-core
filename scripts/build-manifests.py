#!/usr/bin/env python3
"""
Render this repo the way the clusters' kustomize-controller does, so CI can
validate what would actually be applied.

  ./scripts/build-manifests.py OUT_DIR

The clusters substitute variables from flux-values and flux-secrets, which the
Kubernetes Terraform module flattens from a deployment's values.json and
secrets.json. test/values.json and test/secrets.json stand in for those files
and are the reference for their shape: every key a manifest reads, optional
ones included. The build fails if a manifest uses a variable they lack, or if
they hold a key no manifest uses, which also catches a misspelled variable that
a default would otherwise hide.

Each Flux Kustomization in profiles/ is built with `flux build kustomization
--dry-run --strict-substitute`, which generates the kustomization.yaml Flux
would and runs Flux's own post-build substitution. Every kustomize tree under
profiles/ and bootstrap/ is built as well, since clusters point Flux at those
directly.

One rendered file per build is written to OUT_DIR. Requires the flux CLI and
PyYAML.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
TEST_FILES = {"vals": REPO / "test" / "values.json", "secrets": REPO / "test" / "secrets.json"}
TREES = ("profiles", "bootstrap")

# Grafana variables in the metallb and node-exporter dashboards that are not
# escaped as $${...}. No cluster defines them, so Flux substitutes empty
# strings; do the same so the build matches what is deployed.
UNESCAPED_GRAFANA_VARS = {"DS_PROMETHEUS": "", "datasource": ""}

# ${name} or ${name:=default}, but not the escaped $${name}.
VARIABLE = re.compile(r"(?<!\$)\$\{((?:vals|secrets)_[A-Za-z0-9_]+)(?::?[-=][^}]*)?\}")

STR_TAG = "tag:yaml.org,2002:str"


def flatten(value, prefix):
    """Flatten a values file the way the Terraform module's jq does.

    Each scalar becomes prefix_path_to_key, with list indices as path parts and
    every value as a string ("true", "1").
    """
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, list):
        items = enumerate(value)
    else:
        return {prefix: value if isinstance(value, str) else json.dumps(value)}
    flat = {}
    for key, child in items:
        flat.update(flatten(child, f"{prefix}_{key}"))
    return flat


def test_values():
    values = {}
    for prefix, path in TEST_FILES.items():
        values.update(flatten(json.loads(path.read_text()), prefix))
    return values


def check_shape(values):
    """Compare the variables manifests use with the keys the test files hold."""
    used = set()
    for f in REPO.rglob("*.yaml"):
        used.update(VARIABLE.findall(f.read_text()))
    errors = []
    for name in sorted(used - values.keys()):
        errors.append(f"{name} is used by a manifest but missing from {test_file(name)}")
    for name in sorted(values.keys() - used):
        errors.append(f"{name} in {test_file(name)} is not used by any manifest")
    return errors


def test_file(name):
    prefix, _, path = name.partition("_")
    return f"{TEST_FILES[prefix].relative_to(REPO)} ({path.replace('_', '.')})"


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


def with_values(kustomization, values):
    """Return a copy that carries values inline instead of via substituteFrom.

    Dry-run builds skip substituteFrom, so this is how the test values reach
    the substitution.
    """
    spec = dict(kustomization["spec"])
    post_build = dict(spec.get("postBuild") or {})
    post_build.pop("substituteFrom", None)
    # Inline values are what the clusters use, so they win.
    post_build["substitute"] = {**values, **post_build.get("substitute", {})}
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
    (out_dir / f"{name}.yaml").write_text(quote_secret_string_data(result.stdout))
    return None


def quote_secret_string_data(rendered):
    """Put back the quotes flux build drops from Secret stringData.

    The CLI rebuilds every Secret's stringData to mask SOPS values
    (maskSopsData in fluxcd/flux2 internal/build), losing each value's quoting,
    so "587" comes out as 587. kustomize-controller applies the original
    strings. A side effect is that an unquoted number in stringData itself is
    not caught.
    """
    docs = [d for d in yaml.compose_all(rendered) if isinstance(d, yaml.MappingNode)]
    for doc in docs:
        fields = {k.value: v for k, v in doc.value}
        if getattr(fields.get("kind"), "value", None) != "Secret":
            continue
        string_data = fields.get("stringData")
        if isinstance(string_data, yaml.MappingNode):
            for _, value in string_data.value:
                if isinstance(value, yaml.ScalarNode) and value.tag != STR_TAG:
                    value.tag, value.style = STR_TAG, '"'
    return yaml.serialize_all(docs, explicit_start=True)


def report(title, message):
    print(f"FAIL  {title}\n{message}\n")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Annotations need newlines escaped to stay one message.
        message = message.replace("%", "%25").replace("\n", "%0A")
        print(f"::error title={title}::{message}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("out_dir", type=Path, help="directory for rendered manifests")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    values = test_values()
    failed = 0
    shape_errors = check_shape(values)
    if shape_errors:
        failed += 1
        report("test values", "\n".join(shape_errors))

    substitute = {**values, **UNESCAPED_GRAFANA_VARS}
    builds = [
        (name, path, with_values(k, substitute))
        for name, path, k in flux_kustomizations()
    ]
    builds += list(kustomize_trees())

    built = 0
    for name, path, kustomization in builds:
        error = build(name, path, kustomization, args.out_dir)
        if error is None:
            built += 1
            print(f"ok    {name} ({path})")
        else:
            failed += 1
            report(f"flux build {name}", f"{path}\n{error}")

    print(f"\n{built}/{len(builds)} built")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

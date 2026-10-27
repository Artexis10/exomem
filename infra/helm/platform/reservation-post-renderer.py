#!/usr/bin/env python3
"""Replace the upstream LogicalVolume CRD only for a rendered guarded provider."""

import sys
from pathlib import Path

import yaml

source = sys.stdin.read()
documents = list(yaml.safe_load_all(source))
configs = [document for document in documents if isinstance(document, dict)
           and document.get("kind") == "ConfigMap"
           and document.get("metadata", {}).get("name") == "exomem-local-lvmd-config"]
guarded = any(any(device.get("reservation-policy") is not None
                  for device in yaml.safe_load(config["data"]["lvmd.yaml"]).get("device-classes", []))
              for config in configs)
if not guarded:
    sys.stdout.write(source)
else:
    generated = yaml.safe_load((Path(__file__).parent / "files/topolvm/logicalvolumes.yaml").read_text())
    matches = [index for index, document in enumerate(documents) if isinstance(document, dict)
               and document.get("kind") == "CustomResourceDefinition"
               and document.get("metadata", {}).get("name") == "logicalvolumes.topolvm.io"]
    # A missing or duplicate schema is a malformed install, not a capacity observation.
    if len(matches) != 1:
        raise SystemExit("guarded provider requires exactly one LogicalVolume CRD")
    previous = documents[matches[0]]
    generated["metadata"]["annotations"] = {
        **previous.get("metadata", {}).get("annotations", {}), **generated["metadata"].get("annotations", {})}
    documents[matches[0]] = generated
    yaml.safe_dump_all(documents, sys.stdout, sort_keys=False)

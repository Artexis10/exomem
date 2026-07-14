# Hosted encrypted secret artifacts

Only SOPS/age ciphertext belongs in this directory. The versioned destination
matrix in `../contracts/secret-destinations-v1.json` fixes which named secret
may reach which Vercel variable or Kubernetes Secret. Generate artifacts only
through `../scripts/secret_handoff.py`; never hand-write a Secret manifest.

The age private key is not stored on the node or in this repository. Dynamic
cell credentials are deliberately absent: the provisioner moves those from its
encrypted database directly into one tenant namespace.

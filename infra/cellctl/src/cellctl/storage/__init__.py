"""Object-storage (B2) and volume-listing (Hetzner) interfaces for cellctl.

cellctl's own code only ever talks to these two external services through
the Protocols in `interface.py`; backup and restore data transfer itself is
done by `restic` inside a Job pod, not by cellctl.
"""

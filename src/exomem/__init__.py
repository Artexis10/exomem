"""exomem — local knowledge substrate for owned markdown/Obsidian vaults.

Formerly published under the working name kb-mcp; ``import kb_mcp`` remains a
supported alias (see the kb_mcp shim package) and legacy ``KB_MCP_*`` env vars
are honored via promotion below.
"""

# Promotion MUST run before any module-level or call-time env read in this
# package can observe a miss — keep it the first statement of the package.
from .env_compat import promote_legacy as _promote_legacy
from .env_compat import refuse_program_entry_conflicts as _refuse_program_entry_conflicts

_promote_legacy()
_refuse_program_entry_conflicts()

__version__ = "0.210.1"  # x-release-please-version

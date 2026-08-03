"""Shared in-process product-command invocation for CLI-family surfaces.

The CLI (`__main__._core_op_main`) and the terminal UI (`exomem tui`) reach the
unified command registry through this one seam so their semantics cannot drift:
registry lookup, `Param` coercion, vault resolution (including the pre-init
allowances for `browse_memory` and `adopt_vault` scan-only), conditional
`source_schema` injection, and the writer-lease dispatcher.

The `capabilities.active_surface` + owner-principal binding lives INSIDE
`invoke_prepared`, not at call sites: ContextVars do not propagate into worker
threads, and an unbound principal fails silently closed at the governance
egress boundary (results scrub to empty — indistinguishable from "no knowledge
found"). Owning the binding here makes an unbound invocation structurally
impossible for every surface built on the seam.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import cli_ops
from .kbdir import kb_prefix


def _expose_tier2(override: bool | None) -> bool:
    if override is not None:
        return override
    return not os.environ.get("EXOMEM_DISABLE_TIER2")


def product_command(op: str, *, expose_tier2: bool | None = None):
    """Resolve one registry `Command` exposed on the CLI-family surface."""
    from . import commands as commands_module

    tier2 = _expose_tier2(expose_tier2)
    for command in commands_module.product_commands_for("cli", expose_tier2=tier2):
        if command.name == op:
            return command
    raise cli_ops.OpError("UNKNOWN_OP", f"unknown product operation {op!r}")


def cli_surface_descriptor(*, expose_tier2: bool | None = None):
    """The trusted adapter descriptor bound by CLI-family surfaces."""
    from . import capabilities
    from . import commands as commands_module

    tier2 = _expose_tier2(expose_tier2)
    registered = commands_module.product_commands_for("cli", expose_tier2=tier2)
    return capabilities.ActiveSurfaceDescriptor(
        surface="cli",
        profile="product",
        tier2_enabled=tier2,
        product_commands=tuple(command.name for command in registered),
        exported_aliases=commands_module.simple_action_names(),
    )


def allows_uninitialized_vault(op: str, kwargs: dict) -> bool:
    """First-run read allowances: browse/scan may run before the KB exists."""
    if op == "browse_memory":
        return True
    if op == "adopt_vault":
        return (kwargs.get("mode") or "scan-only") == "scan-only"
    return False


def resolve_vault_for(op: str, kwargs: dict, vault_root: Path | str | None) -> Path:
    """Resolve the vault root, honoring the pre-init scan allowances.

    ``vault_root=None`` reproduces the CLI behavior exactly (env-var resolution
    with the uninitialized-dir escape for the allowed ops). An explicit root is
    validated the same way, so a surface built on the seam cannot accidentally
    run a write against a folder that is not a vault.
    """
    from . import vault as vault_module

    if vault_root is not None:
        root = Path(vault_root)
        if vault_module._is_vault(root):
            return root
        if allows_uninitialized_vault(op, kwargs) and root.is_dir():
            return root
        raise RuntimeError(
            f"{str(root)!r} does not look like a vault "
            f"(no {kb_prefix()}_Schema/SKILL.md found)"
        )
    try:
        return vault_module.resolve_vault()
    except RuntimeError:
        if not allows_uninitialized_vault(op, kwargs):
            raise
        override = os.environ.get("EXOMEM_VAULT_PATH")
        if not override:
            raise
        path = Path(override)
        if not path.is_dir():
            raise
        return path


def invoke_prepared(
    cmd,
    kwargs: dict[str, Any],
    *,
    vault_root: Path | str | None = None,
    expose_tier2: bool | None = None,
    idempotency_key: str | None = None,
):
    """Invoke one already-coerced registry command under the shared bindings."""
    from . import capabilities
    from . import schema as schema_module
    from .governance import principal as principal_module
    from .writer_lease import invoke_command

    # Domain-invalid mixed selectors must reach their stable public error
    # before the lease/egress coverage classifier. This is input validation,
    # not a branch registration.
    if cmd.name == "process_media" and kwargs.get("operation", "process") not in {
        "process",
        "status",
        "retry",
    }:
        raise cli_ops.OpError(
            "INVALID_MEDIA_OPERATION",
            "process_media operation must be process, status, or retry",
        )

    root = resolve_vault_for(cmd.name, kwargs, vault_root)
    if cmd.needs_schema:
        injected = (root, schema_module.load_source_schema(root))
    else:
        injected = (root,)

    descriptor = cli_surface_descriptor(expose_tier2=expose_tier2)
    # CLI-family surfaces run in the vault owner's own process: the canonical
    # audience is `owner` (design D5), bound explicitly rather than left to the
    # unbound-contextvar default so the surface label is accurate.
    with capabilities.active_surface(descriptor), principal_module.request_scope(
        principal_module.owner_principal(surface="cli")
    ):
        return invoke_command(cmd, *injected, idempotency_key=idempotency_key, **kwargs)


def invoke_product(
    op: str,
    raw: dict[str, Any] | None = None,
    *,
    vault_root: Path | str | None = None,
    expose_tier2: bool | None = None,
    idempotency_key: str | None = None,
):
    """Coerce raw arguments and invoke one product command end to end.

    Coercion runs REST-strict (`cli=False`): in-process callers pass native
    values, and only the argv-string CLI needs the bare-string JSON
    relaxation (it coerces for itself before `invoke_prepared`).
    """
    cmd = product_command(op, expose_tier2=expose_tier2)
    kwargs = cli_ops.coerce(
        cmd.params,
        dict(raw or {}),
        guarded_fields=cmd.guarded_fields,
        tool=cmd.name,
        cli=False,
    )
    return invoke_prepared(
        cmd,
        kwargs,
        vault_root=vault_root,
        expose_tier2=expose_tier2,
        idempotency_key=idempotency_key,
    )

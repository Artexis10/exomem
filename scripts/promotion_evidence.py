#!/usr/bin/env python3
"""Observe, sign and import promotion evidence for a hosted cohort candidate.

`scripts/sign-directory-evidence.py` is a different tool: it signs marketplace
*directory* evidence. This one produces the record `validatePromotionEvidence`
accepts, which is what `import-artifact` and then `promote-cohort` consume.

Like that script, this deliberately does not invent the facts it signs. The
seven operation flags describe a real client session and are read from a results
file the operator writes after running it; the six counts are queried from the
control plane and the run aborts unless each is exactly 1. A harness that
defaulted those to true/1 would turn the signed-evidence chain into decoration.

Everything here is timing-critical for the FIRST promotion only. The bootstrap
authority is capped server-side at 30 minutes, the rollout assignment inherits
that expiry, and `storeClientArtifact` requires the assignment still be active --
so observe, sign, both imports and promote all have to land inside it. Rehearse
against an existing tenant before opening a window.

Resolve these FOUR environment variables before starting, not during the window.
Two of them name nothing that is deployed under that name, and one is not
reachable from a laptop by default:

* `EXOMEM_HOSTED_PROMOTION_KEY_ID` / `EXOMEM_HOSTED_PROMOTION_SECRET` -- the
  operator signing pair, and the server compares the key id exactly.
* `SUBSTRATE_DATABASE_URL` -- substrate's own `DATABASE_URL`, the hosted Neon
  DSN from its deployment environment. It is NOT whatever `DATABASE_URL` happens
  to be exported locally; pointing this at another Postgres makes every count
  query return 0 and the run aborts late rather than early.
* `PROVISIONER_DATABASE_URL` -- the provisioner's `EXOMEM_PROVISIONER_DATABASE_URL`,
  which lives inside the cluster. Reaching it needs a port-forward and a working
  kubeconfig, so confirm you have both BEFORE the authority exists. Discovering
  it mid-window costs the window.

    observe  --platform claude --results results-claude.json
    sign     --platform claude
    import   --platform claude
    promote

Subsequent promotions do not need this urgency: once a cohort is live a real
client authorizes through the cohort branch, and an ordinary assignment may live
up to 7 days.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

TEST_IDENTITY = "hosted-client-plugins-v1"

OPERATIONS = (
    "native_install",
    "authorization",
    "tool_discovery",
    "content_recall",
    "citation",
    "durable_capture",
    "fresh_chat_recall",
)

COUNTS = (
    "identity_count",
    "tenant_count",
    "entitlement_count",
    "operation_count",
    "cell_count",
    "volume_count",
)

# Mirrors evidenceStrings in substrate client-artifacts.ts. Kept explicit so a
# drift shows up here as a field-set error rather than as an opaque 400 from
# import-artifact at the end of a spent window.
STRINGS = (
    "client_version",
    "clean_client_identity_hmac_sha256",
    "timestamp",
    "paired_run_hmac_sha256",
    "test_identity",
    "exomem_identity_hmac_sha256",
    "tenant_hmac_sha256",
    "entitlement_hmac_sha256",
    "provisioning_operation_hmac_sha256",
    "cell_hmac_sha256",
    "result_sha256",
    "package_artifact_sha256",
    "archive_sha256",
    "compatibility_sha256",
    "schema_contract_sha256",
    "command_surface_sha256",
    "endpoint",
    "plugin_version",
    "profile",
    "operator_key_id",
    "operator_signature",
    "oauth_client_config_sha256",
    "contract_candidate_id",
    "staged_client_release_id",
    "assignment_id",
    "assignment_generation",
)


def canonical(value: object) -> str:
    """Byte-identical to substrate's canonical(): recursive key sort, compact."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                f"{json.dumps(key, ensure_ascii=False)}:{canonical(value[key])}"
                for key in sorted(value)
            )
            + "}"
        )
    raise TypeError(f"cannot canonicalise {type(value)!r}")


def query(url: str, sql: str) -> list[str]:
    result = subprocess.run(
        ["psql", url, "-X", "-A", "-t", "-F", "\x1f", "-c", sql],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode != 0:
        raise SystemExit(f"query failed: {result.stderr.strip()[:400]}")
    return [line for line in result.stdout.strip().split("\n") if line]


def one(url: str, sql: str) -> str:
    rows = query(url, sql)
    if len(rows) != 1:
        raise SystemExit(f"expected exactly one row, got {len(rows)}: {sql[:120]}")
    return rows[0]


def attest(secret: str, label: str, *parts: str) -> str:
    """Privacy-safe attestation over real identifiers.

    The server format-checks these as 64 hex and binds them via operator_signature;
    it never recomputes them. They exist so an artifact references a real tenant,
    cell and run without persisting the raw identifiers, so they must be derived
    from observed values rather than invented.
    """
    message = "\0".join((label, *parts)).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def load_locks(repo: Path, platform: str) -> dict:
    generated = repo / "plugins" / "hosted" / "generated"
    package = json.loads((generated / f"{platform}.lock.json").read_text())
    archive = json.loads((generated / f"{platform}.zip.lock.json").read_text())
    return {"package": package, "archive": archive}


def observe(args: argparse.Namespace) -> int:
    state = args.state_dir
    secret = os.environ["EXOMEM_HOSTED_PROMOTION_SECRET"]
    key_id = os.environ["EXOMEM_HOSTED_PROMOTION_KEY_ID"]
    substrate = os.environ["SUBSTRATE_DATABASE_URL"]
    provisioner = os.environ["PROVISIONER_DATABASE_URL"]

    context = json.loads((state / "bootstrap-context.json").read_text())
    outcome = json.loads((state / args.outcome).read_text())
    tenant = outcome["tenantId"]
    assignment_id = outcome["assignmentId"]
    generation = int(outcome["generation"])

    if args.rehearse:
        # Exercises every query and gate, then refuses to emit. Rehearsal must not
        # be able to produce a signable record, or the dry run becomes a way to
        # manufacture evidence for a client session that never happened.
        results = dict.fromkeys(OPERATIONS, True)
    else:
        results = json.loads(Path(args.results).read_text())
        missing = [name for name in OPERATIONS if name not in results]
        if missing:
            raise SystemExit(f"results file is missing observed operations: {missing}")
        failed = [name for name in OPERATIONS if results[name] is not True]
        if failed:
            raise SystemExit(
                "these operations were not observed to succeed, so there is nothing "
                f"truthful to sign: {failed}"
            )

    owner = one(substrate, f"select owner_user_id from exomem_tenants where id='{tenant}'")
    cell_id = one(
        substrate,
        f"select id from exomem_cells where tenant_id='{tenant}' and lifecycle_state<>'deleted'",
    )
    entitlement = one(substrate, f"select id from exomem_entitlements where tenant_id='{tenant}'")
    operation = one(
        substrate,
        "select id from exomem_lifecycle_operations where tenant_id='"
        f"{tenant}' and operation_type='provision' and state='succeeded'",
    )

    counts = {
        "identity_count": int(one(substrate, f"select count(*) from users where id='{owner}'")),
        "tenant_count": int(
            one(
                substrate,
                f"select count(*) from exomem_tenants where owner_user_id='{owner}' and deleted_at is null",
            )
        ),
        "entitlement_count": int(
            one(substrate, f"select count(*) from exomem_entitlements where tenant_id='{tenant}'")
        ),
        "operation_count": int(
            one(
                substrate,
                "select count(*) from exomem_lifecycle_operations where tenant_id='"
                f"{tenant}' and operation_type='provision' and state='succeeded'",
            )
        ),
        "cell_count": int(
            one(
                substrate,
                f"select count(*) from exomem_cells where tenant_id='{tenant}' and lifecycle_state<>'deleted'",
            )
        ),
        "volume_count": int(
            one(
                provisioner,
                f"select count(*) from exomem_provisioner.resources where tenant_id='{tenant}' and kind='VOLUME'",
            )
        ),
    }
    wrong = {name: value for name, value in counts.items() if value != 1}
    if wrong:
        raise SystemExit(
            "promotion requires exactly one of each; a clean client run that "
            f"produced anything else is not admissible: {wrong}"
        )

    # Must be the PLATFORM SIBLING stage, never the bootstrap stage. The artifact's
    # oauth_client_config_sha256 is read from this row and has to match the sibling
    # client that actually ran the session; the bootstrap stage carries the throwaway
    # loopback client's digest and fails the stage match at import.
    stage_id = args.stage_id
    if not stage_id:
        # `reviewer_bootstrap.py run` writes the two sibling ids here precisely so
        # this does not have to be retyped. Reading them back is safer than any
        # hand-carried value: the bootstrap stage id is also on disk, in
        # bootstrap-context.json, and the two are indistinguishable by eye.
        recorded = args.state_dir / "sibling-stage-ids.json"
        if recorded.is_file():
            stage_id = json.loads(recorded.read_text()).get(args.platform)
        if not stage_id:
            raise SystemExit(
                f"--stage-id is required: pass the {args.platform} SIBLING stage, not the "
                f"bootstrap stage from bootstrap-context.json.\n"
                f"`run` records both siblings at {recorded}; if that file is missing, this "
                "state directory is not from a completed run."
            )
        print(f"  using recorded {args.platform} sibling stage {stage_id[:8]}")
    config_sha = one(
        substrate,
        f"select oauth_client_config_sha256 from exomem_staged_client_releases where id='{stage_id}'",
    )
    locks = load_locks(args.repo, args.platform)
    package, archive = locks["package"], locks["archive"]
    now = datetime.now(UTC)

    unsigned: dict = {
        "schema_version": 1,
        "platform": args.platform,
        "client_version": args.client_version,
        "clean_client_identity_hmac_sha256": attest(
            secret, "clean-client", args.platform, args.client_version, tenant
        ),
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "paired_run_hmac_sha256": attest(
            secret, "paired-run", tenant, assignment_id, str(generation)
        ),
        "test_identity": TEST_IDENTITY,
        "exomem_identity_hmac_sha256": attest(secret, "exomem-identity", owner),
        "tenant_hmac_sha256": attest(secret, "tenant", tenant),
        "entitlement_hmac_sha256": attest(secret, "entitlement", entitlement),
        "provisioning_operation_hmac_sha256": attest(secret, "operation", operation),
        "cell_hmac_sha256": attest(secret, "cell", cell_id),
        "oauth_client_config_sha256": config_sha,
        "contract_candidate_id": context["candidateId"],
        "staged_client_release_id": stage_id,
        "assignment_id": assignment_id,
        "assignment_generation": generation,
        **counts,
        "result_sha256": hashlib.sha256(
            canonical(
                {"counts": counts, "operations": {k: results[k] for k in OPERATIONS}}
            ).encode()
        ).hexdigest(),
        "package_artifact_sha256": package["artifact_sha256"],
        "archive_sha256": archive["archive_sha256"],
        **(
            {"registered_app_id_sha256": package["registered_app_id_sha256"]}
            if args.platform == "openai"
            else {}
        ),
        "compatibility_sha256": package["compatibility_sha256"],
        "schema_contract_sha256": package["schema_contract_sha256"],
        "command_surface_sha256": package["command_surface_sha256"],
        "endpoint": package["endpoint"],
        "plugin_version": package["plugin_version"],
        "profile": package["profile"],
        "operator_key_id": key_id,
        **{name: True for name in OPERATIONS},
    }

    expected = set(STRINGS) | set(COUNTS) | set(OPERATIONS) | {"schema_version", "platform"}
    if args.platform == "openai":
        expected.add("registered_app_id_sha256")
    present = set(unsigned) | {"operator_signature"}
    if present != expected:
        raise SystemExit(
            "evidence field set does not match the contract; "
            f"missing {sorted(expected - present)}, unexpected {sorted(present - expected)}"
        )

    if args.rehearse:
        print("  REHEARSAL - no evidence written")
        print(f"  counts        {counts}")
        print(f"  owner         {owner[:8]}  cell {cell_id[:8]}  entitlement {entitlement[:8]}")
        print(f"  operation     {operation[:8]}  assignment {assignment_id[:8]} gen {generation}")
        print(f"  stage         {stage_id[:8]}  config_sha {config_sha[:16]}...")
        print(f"  field set     matches the contract ({len(expected)} fields)")
        return 0

    path = state / f"evidence-{args.platform}.unsigned.json"
    path.write_text(json.dumps(unsigned, indent=1, sort_keys=True))
    path.chmod(0o600)
    print(f"  observed: counts all 1, {len(OPERATIONS)} operations confirmed from {args.results}")
    print(f"  wrote {path}")
    return 0


def sign(args: argparse.Namespace) -> int:
    secret = os.environ["EXOMEM_HOSTED_PROMOTION_SECRET"]
    state = args.state_dir
    unsigned = json.loads((state / f"evidence-{args.platform}.unsigned.json").read_text())
    signature = hmac.new(secret.encode(), canonical(unsigned).encode(), hashlib.sha256).hexdigest()
    evidence = {**unsigned, "operator_signature": signature}
    path = state / f"evidence-{args.platform}.json"
    path.write_text(json.dumps(evidence, indent=1, sort_keys=True))
    path.chmod(0o600)
    digest = hashlib.sha256(canonical(evidence).encode()).hexdigest()
    print(f"  signed {args.platform}; evidence digest {digest[:16]}...")
    print(f"  wrote {path}")
    return 0


def call(path: str, body: dict, label: str, state: Path) -> tuple[int, dict]:
    base = os.environ["EXOMEM_PUBLIC_BASE_URL"].rstrip("/")
    request = urllib.request.Request(f"{base}{path}", data=json.dumps(body).encode(), method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {os.environ['EXOMEM_ADMIN_TOKEN']}")
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        parsed = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        parsed = {"_raw": raw.decode(errors="replace")[:400]}
    record = state / f"{label}.response.json"
    record.write_text(json.dumps({"status": status, **parsed}, indent=1))
    record.chmod(0o600)
    return status, parsed


def import_artifact(args: argparse.Namespace) -> int:
    state = args.state_dir
    evidence = json.loads((state / f"evidence-{args.platform}.json").read_text())
    evidence_sha256 = hashlib.sha256(canonical(evidence).encode()).hexdigest()
    artifact = {
        "platform": args.platform,
        "state": "pending",
        "installUrl": args.install_url,
        "observedAt": evidence["timestamp"],
        "pluginVersion": evidence["plugin_version"],
        "packageSha256": evidence["package_artifact_sha256"],
        "archiveSha256": evidence["archive_sha256"],
        "compatibilitySha256": evidence["compatibility_sha256"],
        "contractSha256": evidence["schema_contract_sha256"],
        "clientIdentitySha256": evidence["clean_client_identity_hmac_sha256"],
        "pairedRunHmacSha256": evidence["paired_run_hmac_sha256"],
        "exomemIdentityHmacSha256": evidence["exomem_identity_hmac_sha256"],
        "tenantHmacSha256": evidence["tenant_hmac_sha256"],
        "evidenceSha256": evidence_sha256,
        "resultSha256": evidence["result_sha256"],
        "oauthClientConfigSha256": evidence["oauth_client_config_sha256"],
        "candidateId": evidence["contract_candidate_id"],
        "stagedClientReleaseId": evidence["staged_client_release_id"],
        "assignmentId": evidence["assignment_id"],
        "assignmentGeneration": evidence["assignment_generation"],
        "evidence": evidence,
    }
    status, payload = call(
        "/api/exomem/admin/contracts",
        {"action": "import-artifact", "artifact": artifact},
        f"import-{args.platform}",
        state,
    )
    if status != 200 or not payload.get("artifactId"):
        raise SystemExit(f"import-artifact failed: {status} {payload}")
    (state / f"artifact-{args.platform}.txt").write_text(payload["artifactId"])
    print(f"  {args.platform} artifact {payload['artifactId']}")
    return 0


def get(path: str) -> tuple[int, dict]:
    base = os.environ["EXOMEM_PUBLIC_BASE_URL"].rstrip("/")
    request = urllib.request.Request(f"{base}{path}", method="GET")
    request.add_header("Authorization", f"Bearer {os.environ['EXOMEM_ADMIN_TOKEN']}")
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def promote(args: argparse.Namespace) -> int:
    state = args.state_dir
    context = json.loads((state / "bootstrap-context.json").read_text())
    candidate_id = context["candidateId"]

    # Read the digest immediately before the swap. The routable set is computed
    # live from every routable cell, so it moves whenever any cell appears or is
    # destroyed -- including the reviewer tenant we delete afterwards. A digest
    # captured earlier in the window may already be wrong, and promotion is a
    # compare-and-swap that will (correctly) refuse it.
    status, contracts = get("/api/exomem/admin/contracts")
    if status != 200:
        raise SystemExit(f"contracts read failed: {status} {contracts}")
    # rolloutStatus, not agentContracts: the latter carries contract digests and has
    # no candidateId or routable fields at all.
    rollout = contracts.get("rolloutStatus") or []
    row = next((c for c in rollout if c.get("candidateId") == candidate_id), None)
    if not row:
        raise SystemExit(f"candidate {candidate_id} not present in contracts response")
    # routableObservationFresh is informational, NOT a precondition: the stored
    # observation only counts as fresh for 5 minutes after a cell activates, but
    # promoteExomemHostedCohort takes its own live probe via
    # preparePromotionRuntimeHealth and refreshes the authority inside the
    # transaction. Gating on the flag would refuse promotions that succeed.
    if not row.get("routableObservationFresh"):
        print("  note: stored observation is stale (>5 min); promotion re-probes live")
    digest = row["routableSetDigest"]
    live = contracts.get("liveCohortCandidateId")
    print(
        f"  candidate {candidate_id[:8]} routable={row.get('routableCellCount')} digest={digest[:16]}..."
    )
    print(f"  expected live cohort: {live!r}")

    missing = [
        name
        for name in (
            "artifact-claude.txt",
            "artifact-openai.txt",
            "evidence-claude.json",
            "evidence-openai.json",
        )
        if not (state / name).exists()
    ]
    if missing:
        if args.dry_run:
            print(f"  DRY RUN - digest path verified; still missing {missing}")
            return 0
        raise SystemExit(
            f"cannot promote without both artifacts and both evidence records: {missing}"
        )

    claude_artifact = (state / "artifact-claude.txt").read_text().strip()
    openai_artifact = (state / "artifact-openai.txt").read_text().strip()
    body = {
        "action": "promote-cohort",
        "candidateId": candidate_id,
        "claudeArtifactId": claude_artifact,
        "openaiArtifactId": openai_artifact,
        "expectedLiveCandidateId": live,
        "expectedRoutableCellDigest": digest,
        "claudeEvidence": json.loads((state / "evidence-claude.json").read_text()),
        "openaiEvidence": json.loads((state / "evidence-openai.json").read_text()),
    }
    if args.dry_run:
        print("  DRY RUN - not promoting")
        print(f"  would send artifacts {claude_artifact[:8]} / {openai_artifact[:8]}")
        return 0
    status, payload = call("/api/exomem/admin/contracts", body, "promote-cohort", state)
    if status != 200:
        raise SystemExit(f"promote-cohort failed: {status} {payload}")
    print(f"  promoted: {json.dumps(payload.get('result'))[:300]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("observe", "sign", "import", "promote"))
    parser.add_argument("--state-dir", type=Path, required=True)
    # Defaults to the checkout this script is in. An absolute default would name
    # one operator's machine, which `validate-public-artifacts.py` rejects in a
    # public repository and which is wrong for anyone else anyway.
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--platform", choices=("claude", "openai"))
    parser.add_argument("--results", help="JSON of the seven observed operations")
    parser.add_argument("--outcome", default="bootstrap-outcome-final.json")
    parser.add_argument(
        "--stage-id",
        help="platform SIBLING stage; read from the run's sibling-stage-ids.json when omitted. "
        "Never the bootstrap stage — that one only fails at import.",
    )
    parser.add_argument("--client-version", default="1.0.0")
    parser.add_argument("--install-url", default="https://substratesystems.io/exomem/setup")
    parser.add_argument(
        "--rehearse",
        action="store_true",
        help="run every query and gate against a live tenant, then refuse to emit",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="promote: resolve everything, send nothing"
    )
    args = parser.parse_args()

    if args.command in {"observe", "sign", "import"} and not args.platform:
        print("--platform is required", file=sys.stderr)
        return 2
    if args.command == "observe" and not args.results and not args.rehearse:
        print("--results is required for observe", file=sys.stderr)
        return 2

    return {
        "observe": observe,
        "sign": sign,
        "import": import_artifact,
        "promote": promote,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

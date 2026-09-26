"""Red-first unit tests for the D5 rendered manifests (task 3.2)."""

from __future__ import annotations

import base64

from cellctl.manifests import (
    CELL_PORT,
    STORAGE_CLASS,
    CellManifestSpec,
    namespace_name,
    render_cell_manifests,
    render_namespace,
    render_network_policies,
    render_pvc,
    render_secret,
    render_statefulset,
)


def _spec(**overrides) -> CellManifestSpec:
    defaults = dict(
        cell_id="aaaaaaaaaaaaaaaa",
        image="registry.example/exomem-cell@sha256:" + "0" * 64,
        replicas=1,
        read_only=False,
        bearer_current="bearer-current-value",
        backup_password="backup-password-value",
        b2_key_id="b2-key-id-value",
        b2_key_secret="b2-key-secret-value",
    )
    defaults.update(overrides)
    return CellManifestSpec(**defaults)


def _find(manifests: list[dict], kind: str, name: str | None = None) -> dict:
    for manifest in manifests:
        if manifest["kind"] != kind:
            continue
        if name is None or manifest["metadata"]["name"] == name:
            return manifest
    raise AssertionError(f"no {kind} named {name!r} in rendered manifests")


def test_namespace_name_matches_the_exo_cell_convention() -> None:
    assert namespace_name("aaaaaaaaaaaaaaaa") == "exo-cell-aaaaaaaaaaaaaaaa"


def test_namespace_carries_restricted_pod_security_labels() -> None:
    ns = render_namespace(_spec())
    labels = ns["metadata"]["labels"]
    for mode in ("enforce", "audit", "warn"):
        assert labels[f"pod-security.kubernetes.io/{mode}"] == "restricted"


def test_resource_quota_is_present_and_bounds_storage() -> None:
    manifests = render_cell_manifests(_spec(storage_gib=10))
    quota = _find(manifests, "ResourceQuota")
    assert quota["spec"]["hard"]["requests.storage"] == "10Gi"


def test_runtime_network_policy_admits_only_the_gateway_on_cell_port() -> None:
    policies = render_network_policies(_spec())
    runtime = next(p for p in policies if p["metadata"]["name"] == "runtime-ingress")
    rule = runtime["spec"]["ingress"]
    assert len(rule) == 1
    peer = rule[0]["from"][0]
    assert peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "exomem-cloud"
    assert peer["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "exomem-cloud-gateway"
    assert rule[0]["ports"] == [{"protocol": "TCP", "port": CELL_PORT}]


def test_runtime_pod_has_no_egress_at_all_not_even_dns() -> None:
    policies = render_network_policies(_spec())
    runtime = next(p for p in policies if p["metadata"]["name"] == "runtime-ingress")
    assert "Egress" in runtime["spec"]["policyTypes"]
    assert runtime["spec"]["egress"] == []


def test_default_deny_policy_covers_every_pod_ingress_and_egress() -> None:
    policies = render_network_policies(_spec())
    default_deny = next(p for p in policies if p["metadata"]["name"] == "default-deny")
    assert default_deny["spec"]["podSelector"] == {}
    assert set(default_deny["spec"]["policyTypes"]) == {"Ingress", "Egress"}


def test_job_pods_may_egress_only_to_443_and_dns() -> None:
    policies = render_network_policies(_spec())
    jobs = next(p for p in policies if p["metadata"]["name"] == "job-egress")
    egress_rules = jobs["spec"]["egress"]
    ports = {rule_port["port"] for rule in egress_rules for rule_port in rule["ports"]}
    assert ports == {443, 53}
    dns_rule = next(r for r in egress_rules if 53 in {p["port"] for p in r["ports"]})
    assert dns_rule["to"][0]["podSelector"]["matchLabels"]["k8s-app"] == "kube-dns"


def test_job_egress_443_is_scoped_by_ipblock_except_not_unrestricted() -> None:
    # L4: a bare `ports: [443]` rule with no `to:` reached any in-cluster
    # listener and the node's hostPort, not just object storage. An
    # ipBlock of 0.0.0.0/0 with `except` for the cluster/private/CGNAT
    # ranges (chart value cells.jobEgressExcept) keeps the "object storage
    # on the internet" scope while blocking in-cluster targets.
    policies = render_network_policies(
        _spec(job_egress_except=("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    )
    jobs = next(p for p in policies if p["metadata"]["name"] == "job-egress")
    egress_rules = jobs["spec"]["egress"]
    object_storage_rule = next(r for r in egress_rules if r["ports"][0]["port"] == 443)
    ip_block = object_storage_rule["to"][0]["ipBlock"]
    assert ip_block["cidr"] == "0.0.0.0/0"
    assert set(ip_block["except"]) == {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"}


def test_statefulset_pod_has_no_service_account_token() -> None:
    sts = render_statefulset(_spec())
    pod_spec = sts["spec"]["template"]["spec"]
    assert pod_spec["automountServiceAccountToken"] is False


def test_statefulset_container_is_read_only_root_with_dropped_capabilities() -> None:
    sts = render_statefulset(_spec())
    container = sts["spec"]["template"]["spec"]["containers"][0]
    security = container["securityContext"]
    assert security["readOnlyRootFilesystem"] is True
    assert security["capabilities"]["drop"] == ["ALL"]
    assert security["allowPrivilegeEscalation"] is False
    assert security["runAsNonRoot"] is True


def test_statefulset_mounts_a_writable_tmp_emptydir() -> None:
    sts = render_statefulset(_spec())
    pod_spec = sts["spec"]["template"]["spec"]
    tmp_volume = next(v for v in pod_spec["volumes"] if v["name"] == "tmp")
    assert "emptyDir" in tmp_volume
    container = pod_spec["containers"][0]
    tmp_mount = next(m for m in container["volumeMounts"] if m["mountPath"] == "/tmp")
    assert tmp_mount["name"] == "tmp"


def test_pod_security_context_sets_fsgroup_and_change_policy() -> None:
    sts = render_statefulset(_spec())
    pod_security = sts["spec"]["template"]["spec"]["securityContext"]
    assert pod_security["fsGroup"] == 10001
    assert pod_security["fsGroupChangePolicy"] == "OnRootMismatch"


def test_cell_init_init_container_is_present_and_uses_the_same_image() -> None:
    image = "registry.example/exomem-cell@sha256:" + "1" * 64
    sts = render_statefulset(_spec(image=image))
    pod_spec = sts["spec"]["template"]["spec"]
    assert len(pod_spec["initContainers"]) == 1
    init = pod_spec["initContainers"][0]
    assert init["name"] == "cell-init"
    assert init["image"] == image
    # D3: "exomem cell-init --vault /data/vault --json" is the exact
    # contract command, matching lane A's real entrypoint.
    assert init["command"] == ["exomem", "cell-init"]
    assert init["args"] == ["--vault", "/data/vault", "--json"]


def test_cell_init_runs_in_cloud_mode_like_the_server() -> None:
    # cell-init only creates the /data/host custody root and redacts its own
    # output when EXOMEM_CLOUD_CELL is set; without it, the server container
    # starts on a volume cell-init never prepared for cloud mode.
    sts = render_statefulset(_spec())
    init = sts["spec"]["template"]["spec"]["initContainers"][0]
    env = {e["name"]: e.get("value") for e in init["env"]}
    assert env["EXOMEM_CLOUD_CELL"] == "1"
    assert env["EXOMEM_CLOUD_CELL_ID"] == _spec().cell_id


def test_no_container_sets_governance_session_env_vars() -> None:
    # D3 (orchestrator ruling): cell-init no longer runs the governance
    # v3->v4 migration, it only does init + migrate-state -- so neither the
    # init container nor the server container ever needs a governance
    # session identity, and must not carry one.
    sts = render_statefulset(_spec())
    pod_spec = sts["spec"]["template"]["spec"]
    forbidden = {
        "EXOMEM_AUTH_SESSION_KEYRING_FILE",
        "EXOMEM_AUTH_SESSION_CONTROL_FILE",
        "EXOMEM_AUTH_SESSION_MEMBERSHIP_FILE",
        "EXOMEM_AUTH_SESSION_REPLICA_ID",
    }
    for container in [*pod_spec["initContainers"], *pod_spec["containers"]]:
        env_names = {e["name"] for e in container.get("env", [])}
        assert not (env_names & forbidden), (container["name"], env_names)


def test_probes_target_health_and_health_ready() -> None:
    sts = render_statefulset(_spec())
    container = sts["spec"]["template"]["spec"]["containers"][0]
    assert container["readinessProbe"]["httpGet"]["path"] == "/health/ready"
    assert container["livenessProbe"]["httpGet"]["path"] == "/health"


def test_pvc_uses_the_encrypted_storage_class() -> None:
    pvc = render_pvc(_spec(storage_gib=25))
    assert pvc["spec"]["storageClassName"] == STORAGE_CLASS
    assert pvc["spec"]["resources"]["requests"]["storage"] == "25Gi"


def test_secret_carries_current_and_previous_bearer_during_rotation() -> None:
    secret = render_secret(
        _spec(bearer_current="new-token", bearer_previous="old-token")
    )
    assert base64.b64decode(secret["data"]["cell-token"]).decode() == "new-token"
    assert base64.b64decode(secret["data"]["cell-token-previous"]).decode() == "old-token"


def test_secret_omits_previous_bearer_outside_a_rotation() -> None:
    secret = render_secret(_spec(bearer_previous=None))
    assert "cell-token-previous" not in secret["data"]


def test_read_only_sets_the_env_var_and_running_does_not() -> None:
    read_only_sts = render_statefulset(_spec(read_only=True))
    running_sts = render_statefulset(_spec(read_only=False))
    read_only_env = {
        e["name"]: e.get("value")
        for e in read_only_sts["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    running_env = {
        e["name"]: e.get("value")
        for e in running_sts["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert read_only_env["EXOMEM_CLOUD_READ_ONLY"] == "1"
    assert "EXOMEM_CLOUD_READ_ONLY" not in running_env


def test_hold_annotations_are_only_present_during_a_hold() -> None:
    held = render_statefulset(
        _spec(hold_kind="upgrade", hold_started_at="2026-01-01T00:00:00Z")
    )
    unheld = render_statefulset(_spec())
    assert held["metadata"]["annotations"]["exomem.io/hold"] == "upgrade"
    assert unheld["metadata"]["annotations"] == {}


def test_namespace_first_in_render_order_for_apply_safety() -> None:
    manifests = render_cell_manifests(_spec())
    assert manifests[0]["kind"] == "Namespace"


def test_every_namespaced_manifest_targets_the_cell_namespace() -> None:
    spec = _spec()
    manifests = render_cell_manifests(spec)
    expected = namespace_name(spec.cell_id)
    for manifest in manifests:
        if manifest["kind"] == "Namespace":
            continue
        assert manifest["metadata"]["namespace"] == expected


def test_statefulset_carries_the_row_generation_it_was_rendered_for() -> None:
    from cellctl.manifests import ROW_GENERATION_ANNOTATION

    statefulset = _find(render_cell_manifests(_spec(row_generation=7)), "StatefulSet")
    assert statefulset["metadata"]["annotations"][ROW_GENERATION_ANNOTATION] == "7"


def test_the_render_digest_is_also_a_pod_template_annotation_so_a_change_restarts_the_pod() -> None:
    from cellctl.manifests import RENDER_DIGEST_ANNOTATION

    statefulset = _find(render_cell_manifests(_spec(render_digest="d" * 64)), "StatefulSet")
    assert statefulset["spec"]["template"]["metadata"]["annotations"][RENDER_DIGEST_ANNOTATION] == "d" * 64


def _all_rendered_containers(spec: CellManifestSpec) -> list[dict]:
    from cellctl.manifests import render_backup_job, render_restore_job

    held = CellManifestSpec(**{**spec.__dict__, "hold_kind": "backup", "hold_started_at": "2026-01-01T00:00:00+00:00"})
    pod_specs = [
        render_statefulset(spec)["spec"]["template"]["spec"],
        render_backup_job(held, bucket_name="b", endpoint="https://s3.example")["spec"]["template"]["spec"],
        render_restore_job(held, bucket_name="b", endpoint="https://s3.example", snapshot_id="a" * 64)["spec"]["template"]["spec"],
    ]
    return [container for pod in pod_specs for container in [*pod.get("initContainers", []), *pod["containers"]]]


def test_no_rendered_container_sets_home_or_any_xdg_variable() -> None:
    # Cloud-mode state roots follow HOME and XDG_*, so the pod spec leaves
    # the passwd home in charge.
    spec = _spec(read_only=True, bearer_previous="previous", model_env={"EXOMEM_MODEL": "m"})
    containers = _all_rendered_containers(spec)
    assert {c["name"] for c in containers} == {"cell-init", "exomem", "backup", "restore"}
    for container in containers:
        names = {entry["name"] for entry in container.get("env", [])}
        assert "HOME" not in names and not any(name.startswith("XDG_") for name in names), (container["name"], names)


def test_model_env_may_not_set_home_or_an_xdg_variable() -> None:
    import pytest

    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        with pytest.raises(ValueError):
            render_statefulset(_spec(model_env={key: "/elsewhere"}))


# NEW-2: every variable src/exomem reads to relocate state, config or a
# writable directory, and every variable cellctl renders itself, which a
# duplicate model_env entry would silently shadow.
RELOCATING_KEYS = (
    "EXOMEM_STATE_ROOT",
    "EXOMEM_WRITER_LEASE_STATE_DIR",
    "EXOMEM_CONFIG_PATH",
    "EXOMEM_CALL_LEDGER_DIR",
    "EXOMEM_HOSTED_STATE_ROOT",
    "EXOMEM_VAULT_PATH",
    "EXOMEM_KB_DIRNAME",
    "EXOMEM_LOG_DIR",
    "EXOMEM_LEASE_COORDINATOR_DB",
    "EXOMEM_RANKING_CONFIG",
    "EXOMEM_HOOK_HOME",
    "EXOMEM_SERVICE_ENV",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "HF_HOME",
    "HF_HUB_CACHE",
    "TMPDIR",
    "EXOMEM_CLOUD_READ_ONLY",
    "EXOMEM_CLOUD_CELL_ID",
)


def test_model_env_may_not_relocate_state_config_or_a_writable_directory() -> None:
    import pytest

    for key in RELOCATING_KEYS:
        with pytest.raises(ValueError, match=key):
            render_statefulset(_spec(model_env={key: "/elsewhere"}))
    render_statefulset(_spec(model_env={"EXOMEM_EMBED_BACKEND": "onnx", "EXOMEM_WHISPER_MODEL": "base"}))


def test_the_previous_bearer_reference_is_optional_so_a_refused_secret_never_blocks_the_pod() -> None:
    # Inside a hold the StatefulSet is applied even when this pass's Secret
    # was refused. A required reference to a key the live Secret lacks would
    # leave the pod in CreateContainerConfigError, time the hold out and
    # blame the image.
    statefulset = render_statefulset(_spec(bearer_previous="previous"))
    env = statefulset["spec"]["template"]["spec"]["containers"][0]["env"]
    previous = next(entry for entry in env if entry["name"] == "EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS")
    assert previous["valueFrom"]["secretKeyRef"]["optional"] is True

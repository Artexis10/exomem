package main

import rego.v1

topolvm_node(overrides) := object.union({
  "kind": "DaemonSet",
  "metadata": {
    "name": "exomem-platform-topolvm-node",
    "namespace": "exomem-platform",
    "labels": {"helm.sh/chart": "topolvm-17.2.0"},
  },
  "spec": {"template": {"spec": {
    "containers": [{
      "name": "topolvm-node",
      "image": "ghcr.io/topolvm/topolvm-with-sidecar:0.41.1@sha256:70548dbe0c6addcccf79a557f29e95db2e6dc2cba2102988c91f30086004d0fc",
      "securityContext": {"privileged": true},
    }],
    "volumes": [{"name": "devices", "hostPath": {"path": "/dev"}}],
  }}},
}, overrides)

test_named_topolvm_node_may_be_privileged_with_its_paths if {
  count(deny) == 0 with input as topolvm_node({})
}

test_an_unnamed_privileged_pod_is_still_denied if {
  pod := {
    "kind": "Pod",
    "metadata": {"name": "rogue", "namespace": "exomem-platform"},
    "spec": {"containers": [{
      "name": "topolvm-node",
      "image": "ghcr.io/topolvm/topolvm-with-sidecar:0.41.1@sha256:70548dbe0c6addcccf79a557f29e95db2e6dc2cba2102988c91f30086004d0fc",
      "securityContext": {"privileged": true},
    }]},
  }
  "Pod/rogue uses a privileged container" in deny with input as pod
}

test_the_topolvm_name_in_another_namespace_is_denied if {
  renamed := topolvm_node({"metadata": {"namespace": "default"}})
  "DaemonSet/exomem-platform-topolvm-node uses a privileged container" in deny with input as renamed
}

test_a_named_daemonset_cannot_mount_another_host_path if {
  extra := topolvm_node({"spec": {"template": {"spec": {"volumes": [{"name": "root", "hostPath": {"path": "/"}}]}}}})
  "DaemonSet/exomem-platform-topolvm-node uses an unexpected TopoLVM hostPath" in deny with input as extra
}

test_the_named_daemonset_on_another_image_is_denied if {
  container := object.union(topolvm_node({}).spec.template.spec.containers[0], {
    "image": "ghcr.io/topolvm/topolvm-with-sidecar:0.41.1@sha256:0000000000000000000000000000000000000000000000000000000000000000",
  })
  other := topolvm_node({"spec": {"template": {"spec": {"containers": [container]}}}})
  "DaemonSet/exomem-platform-topolvm-node uses a privileged container" in deny with input as other
}

test_a_second_privileged_container_in_the_named_daemonset_is_denied if {
  sidecar := {"name": "sidecar", "image": "example.invalid/x@sha256:00", "securityContext": {"privileged": true}}
  two := topolvm_node({"spec": {"template": {"spec": {"containers": [
    topolvm_node({}).spec.template.spec.containers[0], sidecar,
  ]}}}})
  "DaemonSet/exomem-platform-topolvm-node uses a privileged container" in deny with input as two
}

package main

import rego.v1

pinned := "registry.example/app:1@sha256:0000000000000000000000000000000000000000000000000000000000000000"

hcloud_node(containers) := {
  "kind": "DaemonSet",
  "metadata": {
    "name": "exomem-platform-hcloud-csi-node",
    "namespace": "exomem-platform",
    "labels": {"helm.sh/chart": "hcloud-csi-2.21.1"},
  },
  "spec": {"template": {"spec": {"initContainers": null, "containers": containers}}},
}

# Charts render `initContainers: null`; the checks must still see every container.
test_privileged_container_beside_null_init_containers_is_denied if {
  doc := {"kind": "Deployment", "metadata": {"name": "x"}, "spec": {"template": {"spec": {
    "initContainers": null,
    "containers": [{"name": "c", "image": pinned, "securityContext": {"privileged": true}}],
  }}}}
  "Deployment/x uses a privileged container" in deny with input as doc
}

test_privileged_init_container_beside_null_containers_is_denied if {
  doc := {"kind": "Deployment", "metadata": {"name": "x"}, "spec": {"template": {"spec": {
    "initContainers": [{"name": "i", "image": pinned, "securityContext": {"privileged": true}}],
    "containers": null,
  }}}}
  "Deployment/x uses a privileged container" in deny with input as doc
}

test_named_hcloud_csi_driver_may_be_privileged if {
  doc := hcloud_node([{
    "name": "hcloud-csi-driver",
    "image": "docker.io/hetznercloud/hcloud-csi-driver:v2.21.1@sha256:79b979d2fc7b46fdddab19e619c65faa201d0d76080765f0ec4b1969e0abe33f",
    "securityContext": {"privileged": true},
  }])
  count(deny) == 0 with input as doc
}

test_another_container_of_the_hcloud_node_cannot_be_privileged if {
  doc := hcloud_node([{"name": "csi-node-driver-registrar", "image": pinned, "securityContext": {"privileged": true}}])
  "DaemonSet/exomem-platform-hcloud-csi-node uses a privileged container" in deny with input as doc
}

test_the_hcloud_driver_name_elsewhere_cannot_be_privileged if {
  doc := {"kind": "Deployment", "metadata": {"name": "x", "namespace": "exomem-platform"}, "spec": {"template": {"spec": {
    "containers": [{
      "name": "hcloud-csi-driver",
      "image": "docker.io/hetznercloud/hcloud-csi-driver:v2.21.1@sha256:79b979d2fc7b46fdddab19e619c65faa201d0d76080765f0ec4b1969e0abe33f",
      "securityContext": {"privileged": true},
    }],
  }}}}
  "Deployment/x uses a privileged container" in deny with input as doc
}

test_the_hcloud_node_driver_from_another_image_cannot_be_privileged if {
  doc := hcloud_node([{"name": "hcloud-csi-driver", "image": pinned, "securityContext": {"privileged": true}}])
  "DaemonSet/exomem-platform-hcloud-csi-node uses a privileged container" in deny with input as doc
}

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

test_an_unprivileged_container_on_another_image_in_the_named_daemonset_is_denied if {
  sidecar := {"name": "sidecar", "image": "example.invalid/x@sha256:00"}
  two := topolvm_node({"spec": {"template": {"spec": {"initContainers": [sidecar]}}}})
  "DaemonSet/exomem-platform-topolvm-node runs a container on an image other than the pinned TopoLVM image" in deny with input as two
}

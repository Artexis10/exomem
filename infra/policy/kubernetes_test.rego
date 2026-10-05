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

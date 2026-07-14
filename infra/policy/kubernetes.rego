package main

import rego.v1

workload if {
  input.kind in {"Pod", "Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
}

pod_spec := input.spec if input.kind == "Pod"
pod_spec := input.spec.template.spec if input.kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}
pod_spec := input.spec.jobTemplate.spec.template.spec if input.kind == "CronJob"

deny contains message if {
  workload
  some volume in pod_spec.volumes
  volume.hostPath
  not approved_hcloud_csi_node
  message := sprintf("%s/%s uses hostPath", [input.kind, input.metadata.name])
}

approved_hcloud_csi_node if {
  input.kind == "DaemonSet"
  input.metadata.name == "exomem-platform-hcloud-csi-node"
  input.metadata.namespace == "exomem-platform"
  input.metadata.labels["helm.sh/chart"] == "hcloud-csi-2.21.1"
}

approved_hcloud_csi_paths := {
  "/var/lib/kubelet",
  "/var/lib/kubelet/plugins/csi.hetzner.cloud/",
  "/var/lib/kubelet/plugins_registry/",
  "/dev",
}

deny contains message if {
  approved_hcloud_csi_node
  some volume in pod_spec.volumes
  volume.hostPath
  not volume.hostPath.path in approved_hcloud_csi_paths
  message := sprintf("%s/%s uses an unexpected CSI hostPath", [input.kind, input.metadata.name])
}

deny contains message if {
  workload
  some container in array.concat(object.get(pod_spec, "initContainers", []), pod_spec.containers)
  not contains(container.image, "@sha256:")
  message := sprintf("%s/%s uses a mutable image", [input.kind, input.metadata.name])
}

deny contains message if {
  workload
  some container in array.concat(object.get(pod_spec, "initContainers", []), pod_spec.containers)
  object.get(container.securityContext, "privileged", false)
  message := sprintf("%s/%s uses a privileged container", [input.kind, input.metadata.name])
}

deny contains message if {
  input.kind == "Service"
  input.spec.type in {"NodePort", "LoadBalancer"}
  object.get(input.metadata.labels, "app.kubernetes.io/part-of", "") == "exomem-hosted"
  message := sprintf("Service/%s exposes hosted infrastructure publicly", [input.metadata.name])
}

deny contains message if {
  input.kind == "Namespace"
  object.get(input.metadata.labels, "exomem.io/tenant-cell", "") == "true"
  object.get(input.metadata.labels, "pod-security.kubernetes.io/enforce", "") != "restricted"
  message := sprintf("Namespace/%s is not restricted", [input.metadata.name])
}

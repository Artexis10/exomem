# Ansible ownership

Owns the declared Ubuntu host configuration and pinned K3s installation after
Terraform creates the node. It does not create cloud resources, fetch or commit
cluster-admin kubeconfig, manage tenant knowledge, or render application
secrets. Normal administration uses a restricted kubeconfig; cluster-admin is
offline break glass.

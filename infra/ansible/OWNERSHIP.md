# Ansible ownership

Owns the declared Ubuntu host configuration and pinned K3s installation after
Terraform creates the node, or after the operator installs the OS on a server
Terraform does not create (dedicated hosts: private link, encrypted local cell
storage, Tang). It does not create cloud resources, fetch or commit
cluster-admin kubeconfig, manage tenant knowledge, or render application
secrets. Normal administration uses a restricted kubeconfig; cluster-admin is
offline break glass.

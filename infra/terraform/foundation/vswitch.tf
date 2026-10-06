# A dedicated Hetzner server joins the cluster over a Robot vSwitch coupled to
# the private network (openspec move-cloud-cells-to-local-storage D8). Off by
# default: nothing exists until var.vswitch names the vSwitch, and turning it
# on adds this one subnet without touching the network or any server.
resource "hcloud_network_subnet" "vswitch" {
  count        = var.vswitch == null ? 0 : 1
  network_id   = hcloud_network.alpha.id
  type         = "vswitch"
  network_zone = "eu-central"
  ip_range     = var.vswitch.subnet_cidr
  vswitch_id   = var.vswitch.id
}

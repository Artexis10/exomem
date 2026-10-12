<!-- authority:non-specification -->

# Exomem Cloud hardware options

Checked on 7 October 2026. These are published offers, not stock reservations or an order.
All prices exclude VAT. Hardware acquisition remains deferred until demand justifies it.

## Representative EU offers

| Offer | Machine RAM / GPU VRAM | Published charge | Storage and placement |
|---|---|---|---|
| Hetzner GEX45 | 64 GB / 24 GB, RTX PRO 4000 Blackwell SFF | €214/month including IPv4; €209 setup | i5-13500, 2 × 512 GB NVMe; Helsinki. [September announcement](https://www.hetzner.com/pressroom/hetzner-expands-its-gpu-portfolio-with-the-gex45/), [specifications](https://www.hetzner.com/dedicated-rootserver/gex45/). |
| Hetzner GEX63 | 128 GB / 96 GB, RTX PRO 6000 Blackwell Max-Q | €999/month including IPv4; €999 setup | Core Ultra 7 265, 2 × 1.92 TB NVMe; EU dedicated offering. [6 October announcement](https://www.hetzner.com/pressroom/hetzner-expands-gpu-portfolio-with-new-gex63-featuring-nvidia-rtx-pro-6000-blackwell-max-q/). |
| OVHcloud RISE-S | 64 GB / no GPU | €64.99/month; €64.99 setup on the Ireland storefront | Ryzen 7 9700X, 2 × 512 GB NVMe; 1 Gbit/s public network, no private bandwidth in this configuration. [Price table](https://www.ovhcloud.com/en-ie/bare-metal/prices/?range=rise), [terms](https://eco.ovhcloud.com/en-ie/rise/). |
| OVHcloud l4-90 | 90 GB / 24 GB, NVIDIA L4 | €0.75/hour or €540/month as separate billing choices | 22 vCores, 400 GB NVMe, 8 Gbit/s network. Exact EU zone and stock require configuration. [Prices](https://www.ovhcloud.com/en-ie/public-cloud/prices/), [L4 terms](https://www.ovhcloud.com/en-ie/public-cloud/gpu/l4/). |
| Scaleway L4-1-24G | 48 GB / 24 GB, NVIDIA L4 | Displayed €0.79/hour and approximately €574.87/month; GPU compute bills per minute | 8 vCPUs; separately billed Block Storage and IPv4. PAR1 appears in the price table. [Prices](https://www.scaleway.com/en/pricing/gpu/), [hardware](https://www.scaleway.com/en/l4-gpu-instance/), [billing](https://www.scaleway.com/en/docs/instances/faq/). |
| Scaleway L40S-1-48G | 96 GB / 48 GB, NVIDIA L40S | €1.47/hour; GPU compute bills per minute | 8 vCPUs; persistent Block Storage costs extra, and scratch NVMe is ephemeral. PAR2 appears in the price table. [June price change](https://www.scaleway.com/en/blog/a-transparent-update-on-scaleway-pricing/), [hardware](https://www.scaleway.com/en/l40s-gpu-instance/). |

Hetzner charges partial use hourly, rounds partial hours up, and caps compute at the monthly charge.
Its dedicated servers have no minimum contract term and permit immediate cancellation.
The dynamic offer pages did not expose numeric hourly rates during this check.
See the [billing rules](https://docs.hetzner.com/general/billing-and-account-management/billing-at-hetzner/billing-system-hetzner/).

OVHcloud advertises no commitment for L4 instances. A monthly instance cannot switch back to hourly billing.
RISE setup fees are waived for commitments of at least 12 months; the shortest RISE-S term remains unverified.
Neither GPU page establishes a setup charge, so the table does not assume zero.
See the [L4 terms](https://www.ovhcloud.com/en-ie/public-cloud/gpu/l4/) and [RISE terms](https://eco.ovhcloud.com/en-ie/rise/).

Scaleway's June notice quotes L4 at €0.792/hour; its current table rounds that display to €0.79.
Preserve the provider's monthly estimate rather than deriving an invoice from the rounded figure.
L40S at €1.47 × 730 hours is €1,073.10; this is arithmetic, not a monthly tariff.
GPU compute has no minimum commitment, but allocated storage and IPv4 remain billable while compute is off.
The GPU OS requires at least 125 GB of system storage. See the [GPU FAQ](https://www.scaleway.com/en/docs/instances/faq/).

Older price references need care. Hetzner's GEX131 launch price of €889 is stale.
The [15 June tariff](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/) lists €1,197.30/month excluding IPv4, plus €599 setup.
The current small GPU offer is GEX45; an old GEX44 comparison does not describe the current portfolio.

## What these machines could expose

These are engineering inferences, not measured capacity or throughput claims.

| Capability | Hardware contribution | Evidence still needed |
|---|---|---|
| More isolated cells with CPU embeddings | More RAM and CPU permit more resident cell state. Cells can use shared read-only model weights. | Occupancy, private memory, CPU contention, query latency, and headroom on the actual host. |
| Per-cell sensing and larger rerankers | More RAM and CPU let each cell run its own instruments on shared read-only model weights. GPU memory permits larger models, but no GPU process may receive two cells' text. | Model fit, per-cell activation memory, CPU contention between cells, failure isolation, and cost at measured concurrency. |
| Media transcription | Compatible GPU workers can run transcription off the request path. | Accuracy, job throughput, tenant isolation, and interruption recovery on representative media. |
| Faster local restore | Dedicated NVMe permits local restore and index rebuild without network block storage. | Recovery time, writes at risk, free space for restore, and node-loss acceptance. Scratch storage supplies no durable custody. |

An advertised GPU does not establish model fit or inference speed. System RAM and VRAM are separate budgets.
Extra RAM also does not establish cells per host: requests, warm peaks, filesystem use, and headroom all limit admission.
The accepted 512 MiB / 125m request pair still needs the occupancy qualification in [resource policy task 3.9](../openspec/changes/add-cloud-service-resource-policy/tasks.md).
No candidate machine has an accepted cells-per-host measurement.

## Embedding isolation and per-cell sensing

The [ONNX proposal](../openspec/changes/swap-embedding-runtime-to-onnx/proposal.md) rejects shared embedding inference for its runtime change.
That choice keeps embedding requests inside each cell and uses shared read-only model weights without pooling tenant text.
It remains the shipped embedding path.

[R4 and D9](../openspec/changes/add-sensed-epistemic-model/design.md) apply the same boundary to sensing: each cell senses with its own instruments on shared read-only model weights.
The dreamer stays deterministic in the cell, and each vault retains its own readings ledger.
Models act as instruments; they never author canon. Third-party APIs receive vault text only after the tenant opts in.
The [hosted inference measures](hosted-inference-boundary.md) govern latency, memory, failure isolation, and privacy evidence for each proposed job.

D9 rejects a shared inference process, so no process receives text from two cells.
A worker shared between cells, for any job, needs its own explicit contract and measured benefit.
This memo implements neither path and grants no content access or provisioning authority.

## Recommendation

Keep the current CPU embedding path and defer hardware until demand arrives.
Implement sensing as per-cell instruments on shared read-only model weights, as R4 and D9 specify.
Decide any GPU placement from workload evidence.
GEX45 is a useful monthly GPU reference; short GPU experiments can use the hourly offers only after a budget is agreed.
Do not select a provider from advertised RAM, VRAM, or price alone.

Provider stock, account quotas, selected zones, transfer charges, and the final invoice remain unverified.
RISE-S needs a private-link plan because this offer has no private bandwidth.
All acquisition, recurring charges, and provisioning require the owner's decision.

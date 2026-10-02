## Purpose

Defines the managed Cloud service's compute policy and measurable responsiveness within per-tenant and aggregate resource budgets, independently of workstation footprint modes.

## ADDED Requirements

### Requirement: Operator-owned Cloud service profile

Cloud SHALL support explicit `legacy` and `service-v1` deployment profiles. A missing selection SHALL preserve legacy behavior. Only operator-controlled Cloud deployment configuration SHALL select `service-v1`; tenant requests, engagement settings and generic model configuration MUST NOT change it. Invalid selections or selecting the service profile outside a valid Cloud deployment SHALL fail configuration with a value-free error. The system SHALL report profile and selection source separately from stored workstation mode and engagement preference; profile selection MUST NOT rewrite either stored preference.

#### Scenario: Operator canaries a service profile
- **WHEN** an operator upgrades one Cloud cell to a deployment explicitly selecting `service-v1`
- **THEN** that cell resolves the service policy while cells on legacy deployments retain their previous policy
- **AND** tenant configuration cannot switch or override the service policy

#### Scenario: Invalid deployment selection
- **WHEN** the selection is unknown or `service-v1` is selected outside valid Cloud deployment
- **THEN** startup reports a configuration error without echoing credentials or arbitrary supplied values

#### Scenario: Stored workstation mode changes in Cloud
- **WHEN** the stored workstation mode changes while the service profile is running
- **THEN** live mode application preserves the operator-selected core residency and service compute policy
- **AND** the stored selection is not silently rewritten to match that policy

### Requirement: Selective core residency and truthful degradation

A healthy running `service-v1` cell SHALL keep its actual serving core recall encoder resident and ready on CPU, without automatically enabling or preloading optional model groups or corpus caches. Core failures SHALL preserve lexical service and canonical writes with durable semantic debt while reporting semantic unavailability accurately. Idle reclamation SHALL remain available for safely reclaimable optional models and caches without unloading an in-flight resource. The profile MUST NOT change vector identity, engagement settings or mutation authority.

#### Scenario: Healthy cell remains warm after idle
- **WHEN** a healthy service-profile cell passes the idle reclamation window
- **THEN** its core encoder remains usable without a new load
- **AND** inactive optional models and corpus caches remain eligible for safe reclamation

#### Scenario: Core preload fails
- **WHEN** the serving encoder cannot become usable
- **THEN** semantic readiness is degraded or unavailable and lexical retrieval remains available
- **AND** successful canonical writes are not represented as semantically indexed unless their exact derived version has published

### Requirement: Cloud outcome and capacity gates

Promotion of `service-v1` SHALL require measured ordinary-write and query outcomes on the maintained large-vault fixture both with and without sustained bulk work. Twenty ordinary-note samples in each workload, each no more than 1 KiB UTF-8 and three chunks plus three semantic units, SHALL publish the exact committed version into both projections within 5 seconds at p95 and 10 seconds for every sample after commit; request-to-publication SHALL be within 10 seconds at p95 and 20 seconds for every sample. Eligible warm hybrid queries over the maintained 25-query fixture SHALL retain core vector participation with p95 latency at most 3 seconds and no sample over 10 seconds or newly degraded/refused. Outcome checks SHALL distinguish canonical commit from derived publication and measure post-idle and durable restart recovery separately.

The unchanged tenant CPU/memory limits SHALL bound acceptance. Cgroup peak memory SHALL remain at most 80% of the cell memory limit, with no workload-induced OOM or restart. New admissions SHALL use fresh simultaneous warm-cell and platform usage/reservation evidence, charge at least the greater of declared memory request and measured warm peak for each proposed cell, and preserve at least 20% node allocatable memory headroom. A missed gate SHALL block promotion rather than silently relax its threshold. Arbitrary bulk inputs and explicitly unavailable/migrating vector spaces SHALL remain visibly pending/degraded rather than be counted as passing ordinary samples.

#### Scenario: Ordinary save during an import
- **WHEN** a service-profile cell accepts the bounded ordinary-note workload while bulk import runs
- **THEN** exact committed chunks and semantic units publish within the stated freshness gates
- **AND** hybrid query participation and latency satisfy the stated gates without caller retry

#### Scenario: Warm capacity is insufficient
- **WHEN** cold-cell attachment capacity appears sufficient but fresh warmed usage/reservations would violate the node headroom gate
- **THEN** further friend admission and fleet promotion remain blocked
- **AND** the outcome reports insufficient resource capacity without altering tenant limits

### Requirement: Safe service-policy rollback

An operator SHALL be able to return a canary to a verified compatible legacy deployment while preserving post-upgrade canonical writes and outstanding durable index work. Rollback MUST NOT restore an older vault snapshot over newer committed data or relax tenant identity, authorization or publication fences.

#### Scenario: Canary misses its outcome budget
- **WHEN** a canary fails service acceptance after accepting new canonical writes
- **THEN** promotion stops and the operator can return to a compatible legacy deployment
- **AND** the new writes and their outstanding derived work remain recoverable

## 1. Reproduction

- [x] 1.1 Reproduce the rehearsal's symptom against a live cloud-cell server in temporary state: the first boot plus a write stays ready, and a restarted boot plus a write sticks at `retrieval_unavailable`. Show that it holds with embeddings cleanly disabled, so it is not the missing model.
- [x] 1.2 Reproduce it as a failing test against a real vault in temporary state: an inherited catalogue, managed warm-up, one governed write, and the health proof.

## 2. Repair

- [x] 2.1 Rebase an inherited, state-equal catalogue checkpoint into this process's lineage after warm-up proves it. The rebase runs under the publication barrier, only in the serving repair owner.
- [x] 2.2 Hand a scope that a bounded mutation applied but could not bless, because it has no stored or bridgeable origin, to the managed repair owner once the barrier is released.
- [x] 2.3 Keep the health probe side-effect free.

## 3. Verification

- [x] 3.1 Each half is pinned by a test that fails without it: the first write stays O(delta) with no repair, and a stranded scope converges through repair without a restart.
- [x] 3.2 Re-run the live reproduction, including the `read_only` to `running` sequence, and show readiness stays ready for 60 s after the recovery write.
- [x] 3.3 Scoped suites green for the lexical catalogue, warm-up, readiness, standby and handoff paths.

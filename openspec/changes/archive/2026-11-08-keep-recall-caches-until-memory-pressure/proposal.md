## Why

In normal mode the idle reaper releases the recall caches (find RAM caches, BM25 corpora, index matrices) fifteen minutes after their last use. Memory need not be short for this to happen. The first recall after any quiet spell then rebuilds them cold. On a personal service that costs tens of seconds to minutes per recall. One day's log showed the find caches released 22 times. Normal mode should keep what recall built until memory is actually short. Quiet mode keeps its promise to release on idle.

## What Changes

- In normal mode an idle CPU cache is released only when the host's available memory, or the service cgroup's headroom below `memory.high`, falls below a floor.
- The floor is the setting `cache_release_available_mib` (environment `EXOMEM_CACHE_RELEASE_AVAILABLE_MIB`), default 2048.
- When neither memory reading is available, the cache is released on idle as before.
- Quiet mode, performance mode and model singletons keep their current behaviour.

## Impact

- Code: `mode.py` (policy and a shared integer-setting reader), `runtime_resources.py` (cgroup headroom and the memory-short reading), `model_reaper.py` (the gate), `image_view.py` (uses the shared reader).
- Spec: `resource-governance`, requirement "Safe normal-mode residency".
- Resident memory in normal mode can stay higher while the machine has room. The floor bounds it.

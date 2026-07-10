# Tasks

## 1. Comparison harness

- [ ] 1.1 `scripts/compare_memory_servers.py`: corpus prep (synth_vault reuse), contender
      launch (exomem stdio / basic-memory uvx stdio, isolated config dirs, safe-read env),
      fixed query set, per-call timing, RSS sampling, first-index timing
- [ ] 1.2 Markdown report renderer (aggregate-only; fairness-contract section; host line)
- [ ] 1.3 Smoke path: `--tier fixture` runs against `tests/fixtures` in under a minute,
      model-free

## 2. RSS lane

- [ ] 2.1 `scripts/latency_curve.py --rss`: psutil-optional RSS after warm and after query
      pass, new columns in the emitted table

## 3. Startup benchmark

- [ ] 3.1 `scripts/startup_benchmark.py`: importtime parse (top offenders table), `--help`
      wall time, one-shot model-free product command wall time

## 4. Publication

- [ ] 4.1 `docs/comparison-basic-memory.md` with methodology, results, limitations,
      reproduction commands
- [ ] 4.2 Summary row + pointer in `docs/benchmarks.md`

## 1. Write advisories defer a cold corpus matrix

- [x] 1.1 Give `search_many` the `defer_cold_load` switch that `search` has, and pass it from both overlap sweeps in an activated managed runtime.
- [x] 1.2 Skip with the cold exception's own reason, and log `matrix_warming` as a quiet reason.
- [x] 1.3 Test: a managed save skips its overlap advisory while one background load runs, and a save after the load scores the page.

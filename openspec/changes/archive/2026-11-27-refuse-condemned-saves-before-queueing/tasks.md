## 1. Refusal before queueing

- [x] 1.1 Add `episode_memory.argument_refusal`, which applies the recap's own validation without reading state.
- [x] 1.2 Apply it, and the argument rules of commands that own them, in `commands.refusal_before_queueing`.
- [x] 1.3 Refuse a condemned save behind the queue without waiting for the queue.
- [x] 1.4 Tests: an oversized recap and a mis-shaped `record_memory` call behind the queue are refused and never queued.

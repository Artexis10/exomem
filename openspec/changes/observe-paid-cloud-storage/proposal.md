# Observe paid Cloud storage

The companion web service adds manual, provider-confirmed storage purchases.
The controller needs a durable observation that distinguishes requested bytes from usable claim capacity.
This change adds that observation to the existing cell reconciliation and preserves import, deletion and local growth behavior.
It does not change image pins, compute reservations, the storage class or rollout ownership.

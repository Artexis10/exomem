"""Template registry: importing this package registers every template."""

from __future__ import annotations

from membench.templates import t00_mini_smoke  # noqa: F401  (registration)
from membench.templates import t01_temporal_reversal  # noqa: F401  (registration)
from membench.templates import t02_partial_supersession  # noqa: F401  (registration)
from membench.templates import t03_event_vs_ingestion  # noqa: F401  (registration)
from membench.templates import t04_late_evidence  # noqa: F401  (registration)
from membench.templates import t05_future_plans  # noqa: F401  (registration)
from membench.templates import t06_expiring_fact  # noqa: F401  (registration)
from membench.templates import t07_authority_conflict  # noqa: F401  (registration)
from membench.templates import t08_equal_authority_dispute  # noqa: F401  (registration)
from membench.templates import t09_tentative_lifecycle  # noqa: F401  (registration)
from membench.templates import t10_retraction  # noqa: F401  (registration)
from membench.templates import t11_transitive_provenance  # noqa: F401  (registration)
from membench.templates import t12_absence_vs_unsupported  # noqa: F401  (registration)
from membench.templates import t13_entropy_dedup  # noqa: F401  (registration)
from membench.templates import t14_identity_graph  # noqa: F401  (registration)
from membench.templates import t15_numeric_multimodal  # noqa: F401  (registration)
from membench.templates import t16_governance_audiences  # noqa: F401  (registration)
from membench.templates import t17_procedural_chains  # noqa: F401  (registration)
from membench.templates import t18_quantitative  # noqa: F401  (registration)
from membench.templates import t19_negation_counterfactual  # noqa: F401  (registration)
from membench.templates import t20_cross_lingual  # noqa: F401  (registration)
from membench.templates import t21_preference_attribution  # noqa: F401  (registration)
from membench.templates import t22_source_reliability  # noqa: F401  (registration)
from membench.templates.base import Template, registry

__all__ = ["Template", "registry"]

from dataclasses import dataclass
from typing import Dict, Union
import torch
from mttl.models.containers.selectors import (
    PerTokenSelector,
    PerTokenSelectorConfig,
    register_multi_expert_selector,
)
from mttl.models.library.expert_library import ExpertLibrary
from mttl.models.library.library_transforms import ArrowTransform, ArrowConfig


def compute_arrow_embeddings(
    library,
    ab_only=True,
    tie_params=None,
    tie_op="concat",
    base_model_proto=False,
    recompute_prototypes=False,
):
    cfg = ArrowConfig(
        ab_only=ab_only,
        tie_params=tie_params or "default",
        tie_op=tie_op,
    )

    return ArrowTransform(cfg).transform(
        library,
        recompute=recompute_prototypes,
        add_base_proto=base_model_proto,
    )


@dataclass
class ArrowSelectorConfig(PerTokenSelectorConfig):
    router_temp: float = 1.0
    moe_top_k: int = -1
    proto_init: str = "arrow"
    input_norm_fn: str = "id"
    proto_norm_fn: str = "id"


@register_multi_expert_selector("arrow_router", ArrowSelectorConfig)
class ArrowSelector(PerTokenSelector):
    def _load_from_library(self):
        """Fetches prototypes from the library."""
        from mttl.models.library.library_transforms import ArrowConfig, ArrowTransform

        return ArrowTransform(ArrowConfig(name=self.config.selector_data_id)).fetch(
            self.config.library_id
        )

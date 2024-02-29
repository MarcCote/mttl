import pytest
import torch
import numpy as np
from pytorch_lightning import seed_everything
from mttl.models.expert_model import MultiExpertModel
from mttl.models.modifiers.lora import LoRAConfig
from mttl.models.modifiers.expert_containers.selectors import (
    PolySelector,
    PolySelectorConfig,
    TaskNameSelectorConfig,
    TaskNameSelector,
)
from mttl.models.expert_model import Expert


def test_expert_model():
    seed_everything(0)
    model = MultiExpertModel(model="EleutherAI/gpt-neo-125m", device_map="cpu")
    model.add_empty_expert("a", LoRAConfig(modify_layers=".*out_proj.*"))
    model.add_empty_expert("b", LoRAConfig(modify_layers=".*out_proj.*"))
    assert len(model.selectors) == 0

    # plug a poly selector
    model.set_selector("lora", PolySelectorConfig(task_names=["t1", "t2", "t3"]))
    assert len(model.selectors["lora"]) == 12
    assert isinstance(next(iter(model.selectors["lora"].values())), PolySelector)

    expert_a: Expert = model.get_expert_instance("a")
    assert len(expert_a.expert_weights) == 24
    assert expert_a.expert_config.modify_layers == ".*out_proj.*"
    expert_merged = model.get_merged_expert(task_name="t1")
    assert len(expert_merged.expert_weights) == 24
    assert np.allclose(
        sum([p.sum().item() for p in expert_merged.expert_weights.values()]),
        -0.407,
        atol=0.1,
    )

    # switch selector for lora to task name
    model.set_selector("lora", TaskNameSelectorConfig())

    # this should raise an error
    with pytest.raises(NotImplementedError):
        model.get_merged_expert()

    assert len(model.selectors["lora"]) == 12
    assert isinstance(next(iter(model.selectors["lora"].values())), TaskNameSelector)

    #


if __name__ == "__main__":
    pytest.main([__file__])

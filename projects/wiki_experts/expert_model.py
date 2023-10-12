import torch

from mttl.models.modifiers.routing import RoutingInfo
from transformers import AutoModelForCausalLM, LlamaForCausalLM

from mttl.models.utils import (
    EfficientCheckpointModule,
)

from mttl.models.utils import download_from_hub
from mttl.models.modifiers.experts import add_expert_to_transformer
from mttl.utils import get_checkpoint_path, logger
from expert_trainer import ExpertTrainer
from config import ExpertConfig


def push_expert_to_hub(
    ckpt_path,
    hf_user_id,
    auto_search=True,
    use_last=False,
    expert_name=None,
) -> None:
    from mttl.models.utils import convert_and_push_to_hub

    """Searches into local path for the checkpoint with lowest validation loss,
    then uploads that.

    if use_last is True, then uses the last checkpoint `last.ckpt` instead
    of the one with lowest validation loss.
    """
    from mttl.utils import get_checkpoint_path

    if auto_search:
        ckpt_path = get_checkpoint_path(ckpt_path, use_last=use_last)

    ckpt = torch.load(ckpt_path)

    if expert_name is None:
        for key in ['expert_name', 'finetune_task_name']:
            expert_name = ckpt['hyper_parameters'].get(key)
            if expert_name is not None:
                break

    dataset_name = ckpt['hyper_parameters']['dataset']
    # handle the case where dataset is from huggingface
    if "/" in dataset_name:
        dataset_name = dataset_name.partition("/")[-1]

    # model is definitely from HF
    model_name = ckpt['hyper_parameters']['model']
    model_name = model_name.partition("/")[-1]

    repo_id = f"{hf_user_id}/expert__{model_name}__{dataset_name}__{expert_name}"

    logger.info("Uploading checkpoint {} --> {}".format(ckpt_path, repo_id))
    convert_and_push_to_hub(ckpt_path, repo_id, auto_search=False, use_last=False)


class MultiExpertModel(ExpertTrainer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.experts = []

    def load_from_graph_string(self, s):
        from module_graph import ModuleGraph

        graph = ModuleGraph.from_string(s)
        for module_name, module_data in graph.create_modules(
            base_hparams=self.hparams
        ).items():
            self.model = add_expert_to_transformer(
                self.model,
                module_name,
                module_data.expert_config,
                module_data.expert_weights,
                action="route",
                is_default=module_name == "default",
            )
            self.experts.append(module_name)

    def load_expert(
        self,
        expert_path: str,
        expert_name: str = None,
        action: str = "merge",
        is_default: bool = False,
        load_only_layers: str = None,
    ):
        from module_graph import load_expert

        expert = load_expert(expert_path, expert_name=expert_name)
        if self.hparams.model != expert.expert_config.model:
            raise ValueError(
                "The expert has been trained on top of a different model!"
                " Detected: {} - Expected: {}".format(
                    expert.expert_config.model, self.hparams.model
                )
            )

        logger.info(
            f"Adding expert with name {expert_name}... with action ... {action}!"
        )

        self.model = add_expert_to_transformer(
            self.model,
            expert_name,
            expert.expert_config,
            expert.expert_weights,
            action=action,
            is_default=is_default,
            load_only_layers=load_only_layers,
        )
        if action != "merge":
            self.experts.append(expert_name)

    @property
    def generation_config(self):
        return self.model.generation_config

    def expert_choice(self, batch, **kwargs):
        input_ids = batch["input_ids"]
        mask = batch["input_ids"].ne(self.tokenizer.pad_token_id)

        # convert left to right padding here
        def roll_along(arr, shifts, dim):
            assert arr.ndim - 1 == shifts.ndim
            dim %= arr.ndim
            shape = (1,) * dim + (-1,) + (1,) * (arr.ndim - dim - 1)
            dim_indices = torch.arange(arr.shape[dim]).reshape(shape).to(arr.device)
            indices = (dim_indices - shifts.unsqueeze(dim)) % arr.shape[dim]
            return torch.gather(arr, dim, indices)

        input_ids = roll_along(input_ids, mask.sum(1), 1)
        mask = input_ids.ne(0)
        labels = torch.masked_fill(input_ids, ~mask, -100)

        scores = []
        for expert in self.experts:
            batch["task_names"] = [expert for _ in range(batch["input_ids"].shape[0])]
            self.model.task_id_container["routing_infos"] = RoutingInfo.from_batch(
                batch
            )
            outputs = self.model.forward(
                input_ids,
                attention_mask=mask,
            )
            # calculate loss, could also be done inside of the model
            bs = input_ids.size(0)
            logits = outputs.logits
            vocab_size = logits.size(-1)
            shift_logits = logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = torch.nn.CrossEntropyLoss(reduction="none")
            shift_logits = shift_logits.view(-1, vocab_size)
            shift_labels = shift_labels.view(-1)

            # Enable model parallelism
            shift_labels = shift_labels.to(shift_logits.device)
            loss = loss_fct(shift_logits, shift_labels)
            loss = loss.view((bs, -1)).sum(1)
            # mean only non-zero
            scores.append(loss.cpu())

        scores = torch.stack(scores, 0)
        expert_indices = scores.argmin(0)
        return [self.experts[i] for i in expert_indices]

    def generate(
        self,
        batch,
        **kwargs,
    ):
        if self.hparams.routing == "auto":
            logger.info(
                "Auto-routing... ground-truth tasks: {}".format(batch["task_names"])
            )
            batch["task_names"] = self.expert_choice(batch)
            logger.info("Auto-route tasks: {}".format(batch["task_names"]))
        elif self.hparams.routing == "first":
            batch["task_names"] = [
                self.experts[0] for _ in range(batch["input_ids"].shape[0])
            ]
        elif self.hparams.routing == "random":
            import numpy as np

            batch["task_names"] = np.random.choice(
                self.experts, batch["input_ids"].shape[0], replace=True
            ).tolist()

        if hasattr(self.model, "task_id_container"):
            self.model.task_id_container["routing_infos"] = RoutingInfo.from_batch(
                batch
            )

        generations = self.model.generate(inputs=batch["input_ids"], **kwargs)
        return generations

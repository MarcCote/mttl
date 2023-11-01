import os
import json
import torch
import itertools
import torch.nn.functional as F
import torch.distributed as dist
import torch.nn as nn
from statistics import mean
from typing import Any, List, Dict
from dataclasses import dataclass, field
from transformers import AutoModelForSeq2SeqLM
from mttl.models.modifiers import modify_transformer
from mttl.models.get_optimizer import get_optimizer
from mttl.models.get_scheduler import get_scheduler
from mttl.models.utils import EfficientCheckpointModule
from mttl.models.modifiers.routing import RoutingSelector, RoutingInfo as BaseRoutingInfo

@dataclass
class RoutingInfo(BaseRoutingInfo):
    inst_token_mask: torch.Tensor = None
    encoder_output: torch.Tensor = None
    # from clm
    inputs_cache_for_generation: Dict[object, torch.Tensor] = field(default_factory=dict)
    # signals if the model is in generation mode
    generation_mode: bool = False

class EncoderDecoder(EfficientCheckpointModule):
    """
    Encoder Decoder
    """

    def __init__(self, **kwargs):
        """
        :param config
        """
        super().__init__(**kwargs)

        self.save_hyperparameters(ignore=["tokenizer", "model_object"])
        self.config = config = self.hparams
        self.tokenizer = kwargs["tokenizer"]

        if kwargs.get("model_object") is None:
            self.model = AutoModelForSeq2SeqLM.from_pretrained(config.model, cache_dir=config.cache_dir)

            # free up local space after loading in memory
            if config.free_up_space:
                os.system(f"rm -rf {config.cache_dir}")

            self.model = modify_transformer(self.model, config)
        else:
            self.model = kwargs["model_object"]

        self.pad_token_id = self.tokenizer.pad_token_id

        if self.config.compute_strategy:
            self.use_deepspeed = self.config.compute_strategy.startswith("deepspeed")
            self.use_ddp = self.config.compute_strategy.startswith("ddp")
        else:
            self.use_deepspeed = False
            self.use_ddp = False

        self._last_global_step_saved = -1
        self.best_val_result = None
        self.test_results = []
        self.loss_plugins = nn.ModuleDict({})

        print(self.model.encoder.block[0])
        self._inference_outputs = []

        if 'smear' in config.model_modifier:
            task_id_container = self.model.task_id_container
            def enc_fwd(model, input, output):
                task_id_container["routing_infos"].encoder_output = output.last_hidden_state
                return output
            self.model.encoder.register_forward_hook(enc_fwd)
    
    def gather_auxiliary_losses(self):
        # get some losses from the model if it is a router
        aux_loss = []
        for name, module in self.model.named_modules():
            if isinstance(module, RoutingSelector) and hasattr(
                module, "auxiliary_loss"
            ):
                aux_loss_mod = getattr(module, "auxiliary_loss", None)
                if aux_loss_mod is not None:
                    aux_loss.append(aux_loss_mod)
        return aux_loss

    def add_loss_plugin(self, plugin):
        if self.loss_plugins is not None:
            self.loss_plugins[plugin.name] = plugin
        else:
            self.loss_plugins = nn.ModuleDict({plugin.name: plugin})

    def training_step(self, batch, batch_idx, split="train"):
        # propagate task information
        routing_infos = RoutingInfo.from_batch(batch)
        self.model.task_id_container["routing_infos"] = routing_infos 

        if self.config.mc_loss > 0 or self.config.unlikely_loss > 0:
            input_ids, choices_ids, labels = (
                batch["input_ids"],
                batch["answer_choices_ids"],
                batch["labels"],
            )
            bs, num_choices = choices_ids.shape[:2]

            flat_choices_ids = choices_ids.flatten(0, 1)
            attention_mask = batch['attention_mask'].float()
            routing_infos.inst_token_mask = attention_mask
            encoder_hidden_states = self.model.encoder(
                input_ids=input_ids, attention_mask=attention_mask
            )[0]
            encoder_hidden_states = encoder_hidden_states.repeat_interleave(
                num_choices, dim=0
            )
            attention_mask = attention_mask.repeat_interleave(num_choices, dim=0)
            decoder_input_ids = torch.cat(
                [torch.zeros_like(flat_choices_ids[:, :1]), flat_choices_ids[:, :-1]],
                dim=1,
            )
            decoder_attention_mask = (decoder_input_ids == decoder_input_ids).float()
            lm_target = (
                flat_choices_ids
                - 100 * (flat_choices_ids == self.tokenizer.pad_token_id).long()
            )

            model_output = self.model(
                attention_mask=attention_mask,
                encoder_outputs=[encoder_hidden_states],
                decoder_input_ids=decoder_input_ids,
                decoder_attention_mask=decoder_attention_mask,
            )
            choices_scores = (
                F.cross_entropy(
                    model_output.logits.flatten(0, 1),
                    lm_target.flatten(0, 1),
                    reduction="none",
                )
                .view(bs, num_choices, -1)
                .sum(dim=-1)
            )
            if self.config.length_norm > 0:
                choices_scores = choices_scores / torch.pow(
                    (choices_ids != self.tokenizer.pad_token_id).sum(dim=-1),
                    self.config.length_norm,
                )
            lm_loss = F.cross_entropy(
                model_output.logits.view(
                    bs, num_choices, *model_output.logits.size()[1:]
                )[range(bs), labels].flatten(0, 1),
                lm_target.view(bs, num_choices, -1)[range(bs), labels].flatten(0, 1),
            )

            # track accuracy of the method
            choices_scores = (
                F.cross_entropy(
                    model_output.logits.flatten(0, 1),
                    lm_target.flatten(0, 1),
                    reduction="none",
                )
                .view(bs, num_choices, -1)
                .sum(dim=-1)
            )
            if self.config.length_norm > 0:
                choices_scores = choices_scores / torch.pow(
                    (choices_ids != self.tokenizer.pad_token_id).sum(dim=-1),
                    self.config.length_norm,
                )
            pred_score, prediction = choices_scores.min(dim=1)
            acc = (prediction == labels).float().mean()

            self.log("tr_acc_ep", acc.item(), on_epoch=True)
            tensorboard_logs = {"lm_loss": lm_loss.item(), "tr_acc": acc.item()}
            if self.config.mc_loss > 0:
                mc_loss = F.cross_entropy(-choices_scores, labels)
                tensorboard_logs["mc_loss"] = mc_loss.item()
            else:
                mc_loss = 0.0

            if self.config.unlikely_loss > 0:
                cand_loglikely = -F.cross_entropy(
                    model_output.logits.flatten(0, 1),
                    lm_target.flatten(0, 1),
                    reduction="none",
                ).view(bs, num_choices, -1)
                cand_loglikely += (lm_target < 0).view(bs, num_choices, -1) * -100
                cand_loglikely[range(bs), labels] = -100
                unlikely_loss = (
                    -torch.log(1 - torch.exp(cand_loglikely) + 1e-2).sum()
                    / (cand_loglikely != -100).sum()
                )
                tensorboard_logs["unlikely_loss"] = unlikely_loss.item()
            else:
                unlikely_loss = 0.0
            
            loss = (
                lm_loss
                + mc_loss * self.config.mc_loss
                + unlikely_loss * self.config.unlikely_loss
            )
            tensorboard_logs["loss"] = loss.item()
        else:
            input_ids, target_ids = batch["input_ids"], batch["labels"]
            attention_mask = batch["attention_mask"].float()
            routing_infos.inst_token_mask = attention_mask

            decoder_input_ids = torch.cat(
                [torch.zeros_like(target_ids[:, :1]), target_ids[:, :-1]], dim=1
            )  # [bs, max_seq_len]
            
            # need to transform -100 into padding tokens
            decoder_input_ids[decoder_input_ids == -100] = self.tokenizer.pad_token_id

            decoder_attention_mask = (decoder_input_ids == decoder_input_ids).float()

            model_output = self.model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                decoder_input_ids=decoder_input_ids,
                decoder_attention_mask=decoder_attention_mask,
                labels=target_ids,
            )
            loss = model_output.loss
            tensorboard_logs = {"loss": loss.item()}

        # get some losses from the model if it is a router
        aux_loss = self.gather_auxiliary_losses()
        aux_loss = torch.stack(aux_loss).mean() if len(aux_loss) else torch.zeros(1).to(loss.device)
        tensorboard_logs['aux_loss'] = aux_loss.item()
        loss = loss + aux_loss

        # log learning rate as well
        for i, pg in enumerate(self.optimizers().param_groups):
            tensorboard_logs[f"lr_{i}"] = pg["lr"]

        if (
            self.config.save_every
            and self.global_step % self.config.save_every == 0
            and self.global_step > 0
        ) or self.global_step == 25_000:
            self.save_model()

        # reset task information
        self.model.task_id_container["routing_infos"] = None

        self.log_dict(
            {f"{split}/{k}": v for (k, v) in tensorboard_logs.items() if 'loss' in k}, sync_dist=True, prog_bar=True
        )
        self.log_dict(
            {f"{split}/{k}": v for (k, v) in tensorboard_logs.items() if 'loss' not in k}, sync_dist=True,
        )
        for plugin in self.loss_plugins.values():
            plugin_loss = plugin.compute_loss(self.model, batch)
            loss += plugin.factor * plugin_loss
            self.log(
                f"{split}/{plugin.name}_loss", plugin_loss, on_epoch=True, prog_bar=True
            )
        return loss

    def save_model(self, finish=False):
        if finish or self._last_global_step_saved != self.global_step:
            if finish:
                model_fname = os.path.join(self.config.output_dir, "finish.pt")
            else:
                model_fname = os.path.join(
                    self.config.output_dir, f"global_step{self.global_step}.pt"
                )

            self.trainer.save_checkpoint(model_fname)
            self._last_global_step_saved = self.global_step

    def on_train_end(self):
        self.save_model(finish=True)

    def predict(self, batch):
        """
        Predict the lbl for particular pet
        :param batch:
        :param pet:
        :return:
        """

        # propagate task information
        routing_infos = RoutingInfo.from_batch(batch)
        self.model.task_id_container["routing_infos"] = routing_infos 

        input_ids, choices_ids, labels = (
            batch["input_ids"],
            batch["answer_choices_ids"],
            batch["labels"],
        )

        split_option_at_inference = False

        if not split_option_at_inference:
            bs, num_choices = choices_ids.size()[:2]
            flat_choices_ids = choices_ids.flatten(0, 1)
            attention_mask = (
                input_ids != self.tokenizer.pad_token_id
            ).float()  # [bs, max_seq_len]
            routing_infos.inst_token_mask = attention_mask
            encoder_hidden_states = self.model.encoder(
                input_ids=input_ids, attention_mask=attention_mask
            )[0]
            encoder_hidden_states = (
                encoder_hidden_states.unsqueeze(dim=1)
                .repeat(1, num_choices, 1, 1)
                .flatten(0, 1)
            )
            attention_mask = (
                attention_mask.unsqueeze(dim=1).repeat(1, num_choices, 1).flatten(0, 1)
            )
            decoder_input_ids = torch.cat(
                [torch.zeros_like(flat_choices_ids[:, :1]), flat_choices_ids[:, :-1]],
                dim=1,
            )
            decoder_attention_mask = (decoder_input_ids == decoder_input_ids).float()
            lm_target = (
                flat_choices_ids
                - 100 * (flat_choices_ids == self.tokenizer.pad_token_id).long()
            )

            model_output = self.model(
                attention_mask=attention_mask,
                encoder_outputs=[encoder_hidden_states],
                decoder_input_ids=decoder_input_ids,
                decoder_attention_mask=decoder_attention_mask,
            )
            choices_scores = (
                F.cross_entropy(
                    model_output.logits.flatten(0, 1),
                    lm_target.flatten(0, 1),
                    reduction="none",
                )
                .view(bs, num_choices, -1)
                .sum(dim=-1)
            )
            if self.config.length_norm > 0:
                choices_scores = choices_scores / torch.pow(
                    (choices_ids != self.tokenizer.pad_token_id).sum(dim=-1),
                    self.config.length_norm,
                )
            pred_score, prediction = choices_scores.min(dim=1)

        else:
            bs, num_choices = choices_ids.size()[:2]
            midpoint = num_choices // 2
            #
            first_half_choice_ids = choices_ids[:, :midpoint, :]
            second_half_choice_ids = choices_ids[:, midpoint:, :]
            #
            all_choice_scores = []

            for half_choice_ids in [first_half_choice_ids, second_half_choice_ids]:
                half_num_choices = half_choice_ids.shape[1]

                flat_choices_ids = half_choice_ids.flatten(
                    0, 1
                )  # [bs * num_choices, choice_len]

                attention_mask = (
                    input_ids != self.tokenizer.pad_token_id
                ).float()  # [bs, max_seq_len]
                routing_infos.inst_token_mask = attention_mask
                encoder_hidden_states = self.model.encoder(
                    input_ids=input_ids, attention_mask=attention_mask
                )[0]
                encoder_hidden_states = (
                    encoder_hidden_states.unsqueeze(dim=1)
                    .repeat(1, half_num_choices, 1, 1)
                    .flatten(0, 1)
                )
                attention_mask = (
                    attention_mask.unsqueeze(dim=1)
                    .repeat(1, half_num_choices, 1)
                    .flatten(0, 1)
                )

                decoder_input_ids = torch.cat(
                    [
                        torch.zeros_like(flat_choices_ids[:, :1]),
                        flat_choices_ids[:, :-1],
                    ],
                    dim=1,
                )
                decoder_attention_mask = (
                    decoder_input_ids == decoder_input_ids
                ).float()
                lm_target = (
                    flat_choices_ids
                    - 100 * (flat_choices_ids == self.tokenizer.pad_token_id).long()
                )

                model_output = self.model(
                    attention_mask=attention_mask,
                    encoder_outputs=[encoder_hidden_states],
                    decoder_input_ids=decoder_input_ids,
                    decoder_attention_mask=decoder_attention_mask,
                )
                choices_scores = (
                    F.cross_entropy(
                        model_output.logits.flatten(0, 1),
                        lm_target.flatten(0, 1),
                        reduction="none",
                    )
                    .view(bs, half_num_choices, -1)
                    .sum(dim=-1)
                )
                if self.config.length_norm > 0:
                    choices_scores = choices_scores / torch.pow(
                        (half_choice_ids != self.tokenizer.pad_token_id).sum(dim=-1),
                        self.config.length_norm,
                    )

                all_choice_scores.append(choices_scores)

            choices_scores = torch.cat(all_choice_scores, dim=-1)
            pred_score, prediction = choices_scores.min(dim=1)

        score_gt = choices_scores[range(bs), labels]
        choices_scores[range(bs), labels] = choices_scores.max(dim=-1)[0]
        score_cand = choices_scores.min(dim=-1)[0]

        batch_output = {
            "prediction": prediction.tolist(),
            "label": labels.tolist(),
            "idx": batch["idx"].tolist(),
            "log.score_gt": score_gt.tolist(),
            "log.score_cand": score_cand.tolist(),
        }

        # reset task information
        self.model.task_id_container["routing_infos"] = None
        return batch_output

    def _inference_step(self, batch):
        # propagate task information
        self.model.task_id_container["routing_infos"] = RoutingInfo.from_batch(batch)
        batch_output = self.predict(batch)

        # reset task information
        self.model.task_id_container["routing_infos"] = None
        return batch_output

    def validation_step(self, batch, batch_idx):
        if "answer_choices_ids" in batch:
            out = self._inference_step(batch)
        else:
            out = self.training_step(batch, batch_idx, split="val"), batch["task_ids"]
        self._inference_outputs.append(out)
        return out

    def test_step(self, batch, batch_idx):
        output = self._inference_step(batch)
        self._inference_outputs.append(output)
        return output

    def inference_epoch_end(self, outputs, split="val"):
        # exchange outputs between processes
        if self.use_deepspeed or self.use_ddp:
            gathered_outputs = [[] for _ in range(dist.get_world_size())]
            dist.all_gather_object(gathered_outputs, outputs)
            if dist.get_rank() == 0:
                outputs = [
                    batch_output
                    for outputs in gathered_outputs
                    for batch_output in outputs
                ]

        if not (self.use_deepspeed or self.use_ddp) or dist.get_rank() == 0:
            # let rank 0 collect all outputs
            accumulated = {key: [] for key in outputs[0].keys()}
            for batch_output in outputs:
                for key, value in batch_output.items():
                    accumulated[key].extend(value)

            # multi-process may yield dupliated examples in the last batch
            valid_mask = []
            idx_set = set()
            for idx in accumulated["idx"]:
                valid_mask.append(idx not in idx_set)
                idx_set.add(idx)
            for key, values in accumulated.items():
                accumulated[key] = [v for v, m in zip(values, valid_mask) if m]

            # compute and log results
            metrics = self.trainer.datamodule.dataset_reader.compute_metric(accumulated)

            for key, value in accumulated.items():
                if key.startswith("log."):
                    metrics[key.replace("log.", "")] = mean(value)

            acc = metrics["accuracy"]
            if self.trainer.global_step == 0:
                metrics["acc_0shot"] = metrics["accuracy"]
            else:
                # need to log like this for checkpoint
                self.log(f"{split}_acc", acc, on_epoch=True)
                metrics["acc"] = metrics["accuracy"]

            metrics["metric_perf"] = metrics["accuracy"]
            metrics = {f"{split}/{k}": v for k, v in metrics.items()}

            self.log_dict(metrics, prog_bar=True, on_epoch=True)

            metrics["epoch"] = self.current_epoch
            metrics["split"] = split
            metrics["step"] = self.global_step
            metrics["metric"] = self.trainer.datamodule.dataset_reader.metric
            metrics["seed"] = self.hparams.seed

            result_str = json.dumps(metrics) + "\n"
            with open(
                os.path.join(self.config.output_dir, f"{split}_scores.jsonl"), "a+"
            ) as f:
                f.write(result_str)
            print("\n" + result_str)

            if split == "val":
                if self.best_val_result is None:
                    self.best_val_result = metrics
                else:
                    if self.best_val_result["val/accuracy"] < metrics["val/accuracy"]:
                        self.best_val_result = metrics
            elif split == "test":
                self.test_results.append(metrics)
        else:
            metrics = {}
        return metrics

    def on_validation_epoch_end(self):
        outputs = self._inference_outputs
        try:
            # differentiate between fine-tuning phase / zero-shot phase and
            # validation phase during training. this will raise because
            # training step does not return a dict
            if "prediction" in outputs[0]:
                outputs = self.inference_epoch_end(outputs, split="val")
        except:
            losses = torch.cat([out[0].sum(-1) for out in outputs], 0)
            task_ids = torch.cat([out[1] for out in outputs], 0)

            # compute the loss per task id
            with open(
                os.path.join(self.config.output_dir, "val_loss_by_task.txt"), "a+"
            ) as f:
                task_losses = {}
                for task_id in torch.unique(task_ids):
                    task_losses[task_id.item()] = (
                        losses[task_ids == task_id].mean().item()
                    )
                f.write(json.dumps(task_losses) + "\n")
            outputs = None

        self._inference_outputs.clear()
        try:
            self.model.clear_routing_losses()
            self.model.clear_routing_metrics()
        except:
            pass
        return outputs

    def on_test_epoch_end(self):
        outputs = self.inference_epoch_end(self._inference_outputs, split="test")
        self._inference_outputs.clear()
        try:
            self.model.clear_routing_losses()
            self.model.clear_routing_metrics()
        except:
            pass
        return outputs

    def configure_optimizers(self):
        config = self.config
        optimizer, self.trainable_param_names = get_optimizer(self.model, self.config)

        assert not any(k.startswith("model.") for k in self.trainable_param_names)

        # now put it back
        self.trainable_param_names = set(
            f"model.{k}" for k in self.trainable_param_names
        )

        try:
            global_bs = (
                config.train_batch_size
                * torch.distributed.get_world_size()
                * config.gradient_accumulation_steps
            )
        except:
            global_bs = config.train_batch_size * config.gradient_accumulation_steps

        if config.total_steps == -1:
            config.total_steps = (
                len(self.trainer.datamodule.train_dataset) // global_bs
            ) * self.trainer.max_epochs

        if config.warmup_steps == -1:
            config.warmup_steps = int(config.warmup_proportion * config.total_steps)

        scheduler = get_scheduler(optimizer, self.config)
        return {
            "optimizer": optimizer,
            "lr_scheduler": {
                "scheduler": scheduler,
                "interval": "step",
            },
        }
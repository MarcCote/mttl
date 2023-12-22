import os
import tqdm
import copy
import pytorch_lightning as pl
from pytorch_lightning import LightningModule, Trainer, callbacks as cb
from torch.optim import Optimizer
from projects.wiki_experts.src.config import ExpertConfig
from projects.wiki_experts.src.expert_trainer import ExpertTrainer

from mttl.utils import logger
from mttl.evaluators.rouge_evaluator import RougeEvaluator


DEBUG = False


class RougeLCallback(cb.Callback):
    def __init__(
        self,
        datamodule,
        output_dir,
        name="rougeL",
        eval_every_opt_step=1,
        checkpoint_oracle=False,
    ):
        self.name = name
        self.output_dir = output_dir
        self.datamodule = datamodule
        self.eval_every_opt_step = eval_every_opt_step
        # save best perf
        self._best_rouge = None
        # checkpointing
        self.do_checkpoint = checkpoint_oracle
        self._checkpoint_now = False
        self._prev_checkpoint = None
        self.evaluator = RougeEvaluator(datamodule)

    @property
    def last_model_path(self):
        return self._prev_checkpoint

    @property
    def best_model_path(self):
        return self._prev_checkpoint

    @property
    def last_chkpt(self):
        return self._prev_checkpoint

    @property
    def best_loss(self):
        return self._best_rouge

    @best_loss.setter
    def best_loss(self, value):
        if self._best_rouge is None:
            self._best_rouge = value
            self._checkpoint_now = True
        else:
            if value > self._best_rouge:
                self._checkpoint_now = True
                self._best_rouge = value

    def on_before_optimizer_step(
        self, trainer: Trainer, pl_module: LightningModule, optimizer: Optimizer
    ) -> None:
        if trainer.global_step % self.eval_every_opt_step == 0:
            metrics = self.test(pl_module)
            self.best_loss = copy.deepcopy(metrics)
            self.maybe_checkpoint_now(trainer)
            self.log_metrics(metrics, pl_module)
            # checksum of parameters
            # p_sum = np.sum([p.detach().cpu().sum() for p in pl_module.parameters()])
        return super().on_before_optimizer_step(trainer, pl_module, optimizer)

    def maybe_checkpoint_now(self, trainer: Trainer):
        if self.do_checkpoint and self._checkpoint_now:
            try:
                filename = (
                    self.output_dir + f"/{self.name}/" + f"{self.best_loss:.004f}.ckpt"
                )
                ckpt_path = os.path.join(filename)
                trainer.save_checkpoint(ckpt_path)
                if (
                    self._prev_checkpoint is not None
                    and ckpt_path != self._prev_checkpoint
                ):
                    os.remove(self._prev_checkpoint)
                self._prev_checkpoint = ckpt_path
            except Exception as e:
                logger.error("Error in checkpointing with RougeLCallback: " + str(e))
        self._checkpoint_now = False

    def test(self, pl_module: LightningModule):
        rougeL = self.evaluator.evaluate(pl_module, verbose=False)
        return rougeL

    def log_metrics(self, metrics, pl_module: pl.LightningModule, on_step=True):
        pl_module.log(
            f"downstream/{self.name}",
            metrics,
            on_step=on_step,
        )

    def remove_checkpoints(self):
        if self._prev_checkpoint is not None and os.path.exists(self._prev_checkpoint):
            os.remove(self._prev_checkpoint)


class ValLossCheckpointCallback(cb.Callback):
    def __init__(self, args: ExpertConfig) -> None:
        super().__init__()
        self.args = args
        self.best_val_loss = None
        self._prev_checkpoint = None
        self.criteria = "val_loss"

    @property
    def best_model_path(self):
        return self._prev_checkpoint

    def on_validation_epoch_end(
        self, trainer: Trainer, pl_module: ExpertTrainer
    ) -> None:
        if self.best_val_loss is None or pl_module.best_val_result < self.best_val_loss:
            self.best_val_loss = pl_module.best_val_result
            logger.info(f"New best val loss: {self.best_val_loss}")
            self.save_best_val_model(trainer, pl_module)

    def save_best_val_model(self, trainer: Trainer, pl_module: LightningModule):
        dir_name = self.args.output_dir
        if not os.path.exists(dir_name):
            os.makedirs(dir_name)
        filename = self.criteria + f"/{self.best_val_loss:.004f}.ckpt"
        ckpt_path = os.path.join(dir_name, filename)
        if (
            self._prev_checkpoint is not None
            and ckpt_path != self._prev_checkpoint
            and os.path.exists(self._prev_checkpoint)
        ):
            os.remove(self._prev_checkpoint)
        trainer.save_checkpoint(ckpt_path)
        self._prev_checkpoint = ckpt_path

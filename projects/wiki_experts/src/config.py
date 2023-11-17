from dataclasses import dataclass
from mttl.config import Config
import os


@dataclass
class ExpertInfo:
    """
    Stuff that we want to save about experts but will never be passed from command line
    """

    parent_node: str = None
    expert_name: str = None
    expert_task_name: str = None


class ExpertConfig(Config):
    def _set_defaults(self):
        super()._set_defaults()

        self.load_in_8bit = False
        self.wandb_project = None
        self.tensorboard = False
        self.hf_token_hub = None
        self.hf_repo_id = None

        self.expert_name = None
        self.routing = "subject"
        self.mmlu_test_split = "test"
        self.load_module = None
        self.module_graph = None
        self.micro_batch_size = None
        self.validation_portion = 0.03

        self.expand_val_set_w_downstream = False

        self.eval_mmlu_callbacks_every = 0
        self.eval_test_set_callback_every = 0
        self.eval_rougeL_callback_every = 0
        self.test_sets_callbacks = []

        self.use_custom_valid_callback = False  # if True use custom callback to early top on eval loss  instead of lightning callback

        self.data_dir = os.getenv("AMLT_DATA_DIR", "~/data/")
        self.output_dir = os.getenv("AMLT_OUTPUT_DIR", "tmp/instruction_learning/")

    def post_init(self):
        if self.micro_batch_size is None:
            self.micro_batch_size = self.train_batch_size

        # to reproduce setup in https://github.com/daanelson/alpaca-lora
        self.gradient_accumulation_steps = (
            self.train_batch_size // self.micro_batch_size
        )
        self.train_batch_size = self.micro_batch_size

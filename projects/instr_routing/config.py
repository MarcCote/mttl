from mttl.config import Config
import os


class RoutingConfig(Config):
    def _set_defaults(self):
        super()._set_defaults()

        self.micro_batch_size = 4
        self.load_in_8bit = False

        self.wandb_project = None
        self.tensorboard = False
        self.switch_to_average = 0

        # scale the output a bit
        self.lora_alpha = 16

        self.router_weight_decay = None  # weight decay for the routing parameters
        self.router_learning_rate = None  # learning rate of the routing parameters
        self.router_temperature = 1.0  # temperature of router for softmax
        self.router_teacher_temperature = (
            1.0  # temperature of router for teacher softmax
        )
        self.router_normalize_weights = (
            False  # l2 normalize cluster centroids before routing
        )
        self.router_teacher_center_momentum = 1.0  # centering momentum a-la DINO_v2, if 1.0 don't use centering
        self.router_shared_weights = True  # share weights between teacher and student

        self.fast_dev_run = False
        self.hf_token_hub = None
        self.validation_portion = 0.03

        self.eval_hellaswag = True
        self.eval_arc = True
        self.eval_truthfulqa = True
        self.eval_superni = True
        self.eval_mmlu = True
        self.eval_batches = -1
        self.gen_alpaca_eval = False

        self.data_dir = os.getenv("AMLT_DATA_DIR", "~/data/")
        self.output_dir = os.getenv("AMLT_OUTPUT_DIR", "tmp/instruction_learning/")

    def post_init(self):
        if self.eval_mmlu and "MMLU_DATA_DIR" not in os.environ:
            raise ValueError("MMLU_DATA_DIR not set in env but eval_mmlu = True.")

        if self.eval_superni and "NI_DATA_DIR" not in os.environ:
            raise ValueError("NI_DATA_DIR not set in env but eval_superni = True.")

        # to reproduce setup in https://github.com/daanelson/alpaca-lora
        self.gradient_accumulation_steps = (
            self.train_batch_size // self.micro_batch_size
        )
        self.train_batch_size = self.micro_batch_size

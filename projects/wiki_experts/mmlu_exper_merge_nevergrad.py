import os
import sys
import glob
import copy
import torch
import wandb
import numpy as np
import pandas as pd
import seaborn as sns
from functools import partial
from huggingface_hub import login
from collections import defaultdict
from pytorch_lightning import seed_everything
from lora_hub import RoutingOptimizer, mmlu_get_loss

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))

from string import Template
from src import mmlu_subject_configs
from mttl.evaluators import MMLUEvaluator
from mttl.utils import setup_logging, logger
from src.graph.module_graph import ModuleGraph

# register models
from projects.wiki_experts.src.expert_model import MultiExpertModel
from projects.wiki_experts.src.config import ExpertConfig
from mttl.datamodule.mmlu_data_module import MMLUDataModule
from mttl.vllm_engines.engines import LLMEngineMMLU, free_memory

base_dir = os.environ.get(
    "MODULES_DIR", "/home/v-oostapenko/dev/amlt/wiki_experts_train_per_cat_2_if/"
)
base_dir_tempalte = (
    lambda subject: f"{base_dir}/ll2_13b_expert_2_{subject}__qa-ostapeno_qa-openai_icl5_clen128_maxD-1_maxC8000_0_length_matched___5e-5_/{subject}/meta-llama_Llama-2-13b-hf-mmlu_test_oracle"
)

# MMLU_MODULES={
#     "formal_logic":
#     "machine_learning":
#     "global_facts":
#     "abstract_algebra":
#     "high_school_physics":
#     "college_biology":
#     "high_school_government_and_politics":
#     "prehistory":
#     "security_studies":
#     "sociology":
# }


def get_module_graph(module_graph):
    if isinstance(module_graph, dict):
        s = ""
        tasks_to_module = {}
        for subject, mapping in module_graph.items():
            tasks_to_module[subject] = mapping
            s += mapping
        return s, tasks_to_module
    else:
        if module_graph in ["SUB_10"]:
            tasks = getattr(mmlu_subject_configs, module_graph)
        else:
            return module_graph

    s = ""
    tasks_to_module = {}
    for i, subject in enumerate(tasks):
        subject_dit = base_dir_tempalte(subject)
        files = glob.glob(f"{subject_dit}/*.ckpt")
        if len(files) == 0:
            logger.warning(f"no ckpt files found for {subject}")
            continue
        best_idx = np.argmax([int(f.split("/")[-1].split(".")[0]) for f in files])
        file = files[best_idx]

        mapping = f"{subject} -> linear({file}:$weight_{subject});"
        tasks_to_module[subject] = file
        s += mapping
    return s, tasks_to_module


def parse_experts_to_load(experts_to_load):
    kwargs = []

    def find_experts(path):
        import glob

        for path in glob.glob(expert_path + "/**/csv_metrics/", recursive=True):
            yield "/".join(path.split("/")[:-2])

    if type(experts_to_load) != list:
        experts_to_load = [experts_to_load]

    for expert in experts_to_load:
        options = expert.split(":")
        expert_path = options[0]
        expert_path, _, expert_name = expert_path.partition("=")
        all_paths = list(find_experts(expert_path)) or [expert_path]

        if not expert_name:
            expert_name = None

        if len(options) >= 2:
            action = options[1]
        else:
            action = "route"

        if len(options) >= 3:
            load_only_layers = options[2]
        else:
            load_only_layers = None

        is_default = "*" in action
        action = action.replace("*", "")

        if len(all_paths) > 1:
            if is_default:
                raise ValueError(
                    "Cannot define more than one default expert! Are you using * in expert path?"
                )
            if expert_name:
                raise ValueError(
                    "Cannot declare a name when using a wildcard in the expert path!"
                )

        kwargs.append(
            {
                "expert_path": expert_path,
                "action": action,
                "is_default": is_default,
                "expert_name": expert_name,
                "load_only_layers": load_only_layers,
            }
        )
    return kwargs


def log_wandb(scores, prefix):
    if wandb.run is not None:
        for t, v in scores.items():
            wandb.log({f"{prefix}_on_{t}_test_mmlu": v["mean"]})


def init_wandb_logger(args):
    if args.wandb_project is None:
        args.wandb_project = os.environ.get("WANDB_PROJECT", "MMLU_ninja_merge")
    if args.wandb_project:
        run_name = os.getenv("AMLT_JOB_NAME", f"{args.model}")
        wandb.init(
            project=args.wandb_project,
            name=run_name,
            config=args,
        )


def _setup_logging(args):
    logger.info("Args: {}".format(args.to_json()))
    setup_logging(args.output_dir)
    init_wandb_logger(args)


def produce_transfer_matrix(args, subject_to_module, use_vllm=True):
    """
    Eval each module on each subject
    """
    transfer_table = {}
    for module_for_subject, module_dest in subject_to_module.items():
        result = {}
        for subject_eval_on, _ in subject_to_module.items():
            # select dataloader
            graph = f"{module_for_subject} -> linear({module_dest}:1.0)"
            config_copy = copy.deepcopy(args)
            config_copy.finetune_task_name = subject_eval_on
            mmlu = MMLUEvaluator(
                config_copy, split=config_copy.mmlu_test_split, use_vllm=use_vllm
            )
            module = MultiExpertModel(
                **vars(config_copy),
                tokenizer=mmlu.datamodule.tokenizer,
                device_map="cpu" if use_vllm else "auto",
            )
            module.load_from_graph_string(graph, action="merge")
            scores = mmlu.evaluate(module)

            result[subject_eval_on] = scores[subject_eval_on]["mean"]
            all = scores.pop("all")
            log_wandb(scores, f"transfer/{module_for_subject}")
            logger.info(
                f"Scores on of {module_for_subject} for {subject_eval_on}:", all["mean"]
            )
            transfer_table[module_for_subject] = result
    transfer_matrix = pd.DataFrame.from_dict(transfer_table)
    if wandb.run is not None:
        tbl = wandb.Table(data=transfer_matrix)
        wandb.log({"transfer_matrix": tbl})
        wandb.log(
            {
                "transfer_matrix": wandb.Image(
                    sns.heatmap(transfer_matrix, annot=True, linewidth=0.5).get_figure()
                )
            }
        )
    try:
        del module
        free_memory()
    except:
        pass
    return transfer_matrix


def run_eval(args: ExpertConfig):
    seed_everything(args.seed, workers=True)
    _setup_logging(args)
    if args.hf_token_hub:
        login(token=args.hf_token_hub)
    use_vllm = args.use_vllm
    _, module_2_dest = get_module_graph(args.module_graph)

    # 1. How good is the merging optimization procedure? Can we find a routing that is equivalent or better than oracle? (How does it compare to join training?)

    # Get oracle perf + cross-task transfer
    transfer_matrix: pd.DataFrame = produce_transfer_matrix(
        args, module_2_dest, use_vllm=use_vllm
    )
    print("Transfer matrix", transfer_matrix)

    # we use the test-sets of each of the modules in the population and see if we can find the right routing or perform better than the oracle
    # we directly use tes-set for search, i.e. its an oracle!

    dm = MMLUDataModule(args, for_generation=use_vllm, do_tokenize=not use_vllm)
    module = MultiExpertModel(
        **vars(args), tokenizer=dm.tokenizer, device_map="cpu" if use_vllm else "auto"
    )
    get_loss_function = partial(mmlu_get_loss, use_vllm=use_vllm)
    best_weights_matrix = {}
    for task in module_2_dest.keys():
        logger.info(f"Optimizing for {task} for {args.n_ng_iterations} iterations")
        config_copy = copy.deepcopy(args)
        config_copy.finetune_task_name = task
        dm = MMLUDataModule(
            config_copy, for_generation=use_vllm, do_tokenize=not use_vllm
        )

        optimizer = RoutingOptimizer(
            model=module,
            modules_2_dest=module_2_dest,
            dataloader=dm.test_dataloader(),
            get_loss=get_loss_function,
            budget=config_copy.n_ng_iterations,
        )
        best_weights, best_graph_string = optimizer.optimize()
        best_weights = best_weights.tolist()
        logger.info("Found best weights: {}".format(best_weights))
        logger.info("Found best graph: {}".format(best_graph_string))
        if wandb.run is not None:
            wandb.log(
                {
                    f"best_weight/{task}:{t}": v
                    for t, v in zip(module_2_dest.keys(), best_weights)
                }
            )
            wandb.log(
                {
                    f"best_weight_{task}": wandb.Image(
                        sns.barplot(
                            x=list(module_2_dest.keys()), y=best_weights
                        ).get_figure()
                    )
                }
            )
        best_weights_matrix[task] = {
            t: v for t, v in zip(module_2_dest.keys(), best_weights)
        }
        # test model with these weights
        graph = ModuleGraph.from_string(best_graph_string)
        model_copy = copy.deepcopy(module)
        model_copy.load_from_graph(graph, action="merge")
        mmlu = MMLUEvaluator(
            config_copy, split=config_copy.mmlu_test_split, use_vllm=True
        )
        scores = mmlu.evaluate(model_copy)
        scores.pop("all")
        log_wandb(
            scores, f"ng_optimal/{task}_optimal_graph/"
        )  # TODO: log this also as a table and potentially as a barchart
        logger.info(f"Scores on of {task} with graph {best_graph_string}:", scores)
        del model_copy
        free_memory()

    best_weights_matrix = pd.DataFrame.from_dict(best_weights_matrix)
    if wandb.run is not None:
        tbl_bw = wandb.Table(data=best_weights_matrix)
        wandb.log({"best_weights_matrix": tbl_bw})
        wandb.log(
            {
                "best_weights_matrix": wandb.Image(
                    sns.heatmap(
                        best_weights_matrix, annot=True, linewidth=0.5
                    ).get_figure()
                )
            }
        )

    # We can do:
    #   - in-distribution evaluation: test sets we consider are the test sets of the tasks we have experts for
    #   - out-of-distribution evaluation: new task

    # Questions:
    # 1. How good is the merging optimization procedure?
    # On a the in-domain val-set of one of the modules in the population, can it converge to the right routing? (run this for each of the 10 test sets)
    # Does it attain perofrmance like the in-domain module? Could it find this module? if not, did it find a better combination?
    # How does it compare to join-training?

    # 2. How well can we generalize to new task? the baseline here is using jointly pre-trained model vs. merging the experts
    # If I could now tain on the new task a bit, is it bette to use as innitialization the merged pexpert vs. jointl pre-trained?

    # Given the modules lets first eval all of them on each other's test sets -> get a tansfe matix
    #

    # Then for each of the subjects for which we have the module, we optimize the merging procedure and see if we can get the right routing.
    # Can we get beyong expert performance with the right routing? The right module is there in the population.


if __name__ == "__main__":
    args = ExpertConfig.parse()
    run_eval(args)

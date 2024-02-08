import os
import sys
import pytorch_lightning as pl
import glob

import copy
import shutil

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))

from mttl.models.modifiers.expert_containers.expert_library import (
    HFExpertLibrary,
    ExpertLibrary,
    LocalExpertLibrary,
    VirtualLocalLibrary,
    retry,
)

from mttl.callbacks import LiveCheckpointCallback

from mttl.models.monitors import get_monitors
from projects.wiki_experts.src.callbacks import DownstreamEvalCallback
from projects.wiki_experts.src.expert_model import (
    MoETrainer,
    MultiExpertModel,
    RoutedMultiExpertModel,
)
from mttl.models.modifiers.expert_containers.module_graph import (
    load_expert,
    Expert,
    ExpertInfo,
)
from mttl.models.modifiers.expert_containers.library_transforms import (
    WeightedLinearMerge,
    WeightedLinearMergeConfig,
)

from projects.wiki_experts.src.evolution.retrievers import (
    RandomRetriever,
    SVDEmbeddingRetriever,
)


import torch
from huggingface_hub import login
from pytorch_lightning import Trainer, seed_everything

from projects.wiki_experts.utils import get_datamodule
from mttl.callbacks import NanoMMLUCallback, RougeCallback
from mttl.utils import (
    get_checkpoint_path,
    get_pl_loggers,
    setup_logging,
    logger,
)
from mttl.models.modifiers.expert_containers.library_transforms import (
    SVDEmbeddingTransform,
    SVDEmbeddingTransformConfig,
)
from typing import Callable
from projects.wiki_experts.src.expert_trainer import ExpertTrainer
from projects.wiki_experts.src.config import ExpertConfig
from projects.wiki_experts.train_experts_main import create_transfer_matrix
from projects.wiki_experts.src.callbacks import RougeLCallback
from mttl.models.modifiers.base import ModifierConfig


FINETUNE_FUNCTIONS: dict[str, Callable] = {}


@retry(max_retries=5, wait_seconds=60)
def svd_transform_with_retry(svd_embedder, expert_lib, upload_to_hf=True, force=True):
    return svd_embedder.transform(expert_lib, upload_to_hf=upload_to_hf, force=force)


def register_finetune_func(name):
    def decorator(func):
        if name not in FINETUNE_FUNCTIONS:
            FINETUNE_FUNCTIONS[name] = func
        else:
            raise ValueError(f"Duplicate name {name} in finetune functions")
        return func

    return decorator


def get_task_expert(task, library):
    if task not in library.tasks:
        raise ValueError(f"Task {task} not found in repository.")

    task_experts = []
    for name, metadata in library.data.items():
        if metadata.expert_deleted:
            continue
        if metadata.expert_task_name == task:
            task_experts.append(name)

    assert len(task_experts) == 1, f"Found {len(task_experts)} experts for task {task}"
    return library[task_experts[0]]


def load_expert_from_checkpoint(checkpoint):
    ckpt = torch.load(checkpoint)
    if "expert_dumps" in ckpt:
        expert_dumps = ckpt["expert_dumps"]
        expert: Expert = Expert.fromdict(expert_dumps)
    else:
        expert: Expert = load_expert(checkpoint)
    return expert


def prepare_expert_lib(args: ExpertConfig, lib_location) -> LocalExpertLibrary:
    exclude_selection = (
        args.remove_experts.split(",") if args.remove_experts is not None else None
    )
    library = LocalExpertLibrary.create_from_remote(
        HFExpertLibrary(args.hf_lib_id, exclude_selection=exclude_selection),
        destination=lib_location,
    )
    return library


def create_mean_expert(args: ExpertConfig, library: ExpertLibrary = None) -> Expert:
    if library is None:
        library = args.hf_lib_id

    return WeightedLinearMerge(WeightedLinearMergeConfig()).transform(library)


def retrieve(args: ExpertConfig, task, k, retrieve_with="random"):
    if retrieve_with == "random":
        k = args.sk
        retriever = RandomRetriever(args, sk=k)

        lib_location = f"/tmp/{args.hf_lib_id}"
        os.makedirs(lib_location, exist_ok=True)
        library = prepare_expert_lib(args, lib_location)
        library: VirtualLocalLibrary = retriever.transform(
            library, current_task=args.finetune_task_name
        )
    elif retrieve_with == "svdemb":
        k = args.sk
        assert args.hf_repo_query is not None, "Please specify hf_repo_query"
        query_library = HFExpertLibrary(args.hf_repo_query)
        task = args.finetune_task_name
        query_expert: Expert = get_task_expert(task, query_library)

        retriever = SVDEmbeddingRetriever(args, sk=k)

        lib_location = f"/tmp/{args.hf_lib_id}"
        os.makedirs(lib_location, exist_ok=True)
        library = prepare_expert_lib(args, lib_location)
        if query_expert in library:
            library.remove_expert(query_expert.name)
        library.add_expert(query_expert)
        ###########################################################################
        # redo SVD with the query expert included
        if "neo" in args.hf_lib_id:
            sparsity_threshold = 0.5
        elif "phi" in args.hf_lib_id:
            sparsity_threshold = 0.5
        else:
            raise ValueError("'Neo' nor 'phi' in hf_lib_id,sparsity_threshold not set")

        logger.info(f"!!!!!!! Using sparsity threshold {sparsity_threshold}")
        svd_embedder = SVDEmbeddingTransform(
            SVDEmbeddingTransformConfig(sparsity_threshold=sparsity_threshold),
            random_state=42,
        )
        svd_transform_with_retry(svd_embedder, library, upload_to_hf=True, force=True)
        ###########################################################################
        library: VirtualLocalLibrary = retriever.transform(
            library, current_task=args.finetune_task_name, task_expert=query_expert
        )
    else:
        raise ValueError(f"Unknown retriever {retrieve_with}")
    return library


@register_finetune_func("nevergrad_randretr")
def finetune_with_nevergrad(args: ExpertConfig, dm):
    """
    LoraHub baselines
    """
    import wandb

    get_pl_loggers(args)
    if wandb.run is not None:
        # log args to wandb
        wandb.config.update(args)

    from projects.wiki_experts.src.evolution.nevergrad_opt import NGRoutingOptimizer
    from mttl.evaluators.rouge_evaluator import RougeEvaluator

    library = retrieve(args, args.finetune_task_name, args.sk, retrieve_with="random")
    assert (
        len(library) == args.sk
    ), f"Retrieved {len(library)} experts, expected {args.sk}"

    dm_for_gen = get_datamodule(args, for_generation=True)

    rouge_evaluator = RougeEvaluator(dm_for_gen)

    def get_loss(model):
        return -1.0 * rouge_evaluator.evaluate(model, split="train", verbose=False)

    module = RoutedMultiExpertModel(**vars(args), device_map="auto")

    optimizer = NGRoutingOptimizer(
        model=module,
        expert_lib=library,
        get_loss=get_loss,
        budget=args.n_ng_iterations,
        action="route",
        regularizer_factor=0.05,
    )

    best_weights, best_graph_string = optimizer.optimize()
    module.load_from_graph_string(best_graph_string, "route", expert_library=library)
    expert = module.replace_container_with_expert("new_task")
    expert = Expert(
        ExpertInfo(
            expert_name="nevergrad",
            expert_config=ModifierConfig.from_training_config(args),
            training_config=args,
        ),
        expert.expert_weights,
    )
    return expert


@register_finetune_func("nevergrad")
def finetune_with_nevergrad(args: ExpertConfig, dm):
    """
    LoraHub baselines
    """
    import wandb

    get_pl_loggers(args)
    if wandb.run is not None:
        # log args to wandb
        wandb.config.update(args)

    from projects.wiki_experts.src.evolution.nevergrad_opt import NGRoutingOptimizer
    from mttl.evaluators.rouge_evaluator import RougeEvaluator

    lib_location = f"/tmp/{args.hf_lib_id}"
    os.makedirs(lib_location, exist_ok=True)
    expert_lib = prepare_expert_lib(args, lib_location)

    dm_for_gen = get_datamodule(args, for_generation=True)

    rouge_evaluator = RougeEvaluator(dm_for_gen)

    def get_loss(model):
        return -1.0 * rouge_evaluator.evaluate(model, split="train", verbose=False)

    module = RoutedMultiExpertModel(**vars(args), device_map="auto")

    optimizer = NGRoutingOptimizer(
        model=module,
        expert_lib=expert_lib,
        get_loss=get_loss,
        budget=args.n_ng_iterations,
        action="route",
        regularizer_factor=0.05,
    )

    best_weights, best_graph_string = optimizer.optimize()
    module.load_from_graph_string(best_graph_string, "route", expert_library=expert_lib)
    expert = module.replace_container_with_expert("new_task")
    expert = Expert(
        ExpertInfo(
            expert_name="nevergrad",
            expert_config=ModifierConfig.from_training_config(args),
            training_config=args,
        ),
        expert.expert_weights,
    )
    return expert


@register_finetune_func("lib_mu")
def finetune_lib_mu(args: ExpertConfig, dm):
    """
    1. Averages the library to a single expert
    2. Fine-tunes this expert on the downstream task
    """
    mean_expert: Expert = create_mean_expert(args)
    if args.finetune_task_name:
        mean_expert.name = args.finetune_task_name

    module = MultiExpertModel(**vars(args)).to("cuda")
    module.add_expert_instance(mean_expert, is_default=True)

    return (train_module(args, module, dm),)


@register_finetune_func("lib_mu_randretr")
def finetune_lib_mu_with_rand_retrieval(args: ExpertConfig, dm):
    """
    1. Retrieves randomly args.sk experts from the library
    2. Averages the library to a single expert
    3. Fine-tunes this expert on the downstream task
    """
    library = retrieve(args, args.finetune_task_name, args.sk, retrieve_with="random")
    assert (
        len(library) == args.sk
    ), f"Retrieved {len(library)} experts, expected {args.sk}"

    mean_expert: Expert = create_mean_expert(args, library)

    module = MultiExpertModel(**vars(args)).to("cuda")
    module.add_expert_instance(mean_expert, is_default=True)

    return train_module(args, module, dm)


@register_finetune_func("lib_mu_svdretr")
def finetune_lib_mu_with_svd_retrieval(args: ExpertConfig, dm):
    """
    1. Retrieves randomly args.sk experts from the library using SVD embeddings
    2. Averages the library to a single expert
    3. Fine-tunes this expert on the downstream task
    """
    library = retrieve(args, args.finetune_task_name, args.sk, retrieve_with="svdemb")
    assert (
        len(library) == args.sk
    ), f"Retrieved {len(library)} experts, expected {args.sk}"

    mean_expert: Expert = create_mean_expert(args, library)

    module = MultiExpertModel(**vars(args)).to("cuda")
    module.add_expert_instance(mean_expert, is_default=True)

    return train_module(args, module, dm)


@register_finetune_func("polylib_full")
def finetune_polylib_full(args: ExpertConfig, dm):
    """
    Tunes selector and experts on downstream task.

    Returns the resulting expert.
    """

    args.trainable_param_names = (
        args.trainable_param_names
        + "|.*module_logits.*|.*selector.*"  # adds selector params to trainable params
    )
    # args.router_selector = "poly_router"
    assert args.router_selector is not None
    module = MoETrainer(**vars(args), device_map="auto")

    for n, p in module.named_parameters():
        if "selector" in n:
            assert p.requires_grad

    module.to("cuda")
    return train_module(args, module, dm)


@register_finetune_func("polylib_uniform")
def finetune_polylib_full(args: ExpertConfig, dm):
    args.router_selector = "uniform"
    module = MoETrainer(**vars(args), device_map="auto")

    # for n, p in module.named_parameters():
    #     if "selector" in n:
    #         assert p.requires_grad==False

    module.to("cuda")
    return train_module(args, module, dm)


@register_finetune_func("polylib_selector")
def finetune_polylib_sel(args: ExpertConfig, dm):
    """
    Only trains the selector on the downstream task.
    """

    args.trainable_param_names = "|.*module_logits.*|.*selector.*"
    assert args.router_selector is not None

    module = MoETrainer(**vars(args), device_map="auto")

    for n, p in module.named_parameters():
        if "selector" in n:
            assert p.requires_grad

    module.to("cuda")
    return train_module(args, module, dm)


@register_finetune_func("polylib_full_randretr")
def finetune_polylib_full_with_rand_retrieval(args: ExpertConfig, dm):
    """
    Like polylib_full, but here we perform random expert selection before training.
    """
    library = retrieve(args, args.finetune_task_name, args.sk, retrieve_with="random")
    assert (
        len(library) == args.sk
    ), f"Retrieved {len(library)} experts, expected {args.sk}"

    # assert args.router_selector == "poly_router"
    assert args.router_selector is not None
    module = MoETrainer(**vars(args), device_map="auto", expert_library=library)

    for n, p in module.named_parameters():
        if "selector" in n:
            assert p.requires_grad

    module.to("cuda")
    return train_module(args, module, dm)


@register_finetune_func("private")
def finetune_private(args: ExpertConfig, dm):
    """
    Just train an expert from scratch
    """

    module = ExpertTrainer(**vars(args)).to("cuda")
    return train_module(args, module, dm)


@register_finetune_func("polylib_full_svdretr")
def finetune_polylib_full_with_svd_retrieval(args: ExpertConfig, dm):
    """
    Like polylib_full, but here we perform expert selection with SVD embeddings before training.
    """
    library = retrieve(args, args.finetune_task_name, args.sk, retrieve_with="svdemb")
    assert (
        len(library) == args.sk
    ), f"Retrieved {len(library)} experts, expected {args.sk}"

    # args.router_selector = "poly_router"
    assert args.router_selector is not None
    module = MoETrainer(**vars(args), device_map="auto", expert_library=library)

    for n, p in module.named_parameters():
        if "selector" in n:
            assert p.requires_grad

    module.to("cuda")
    return train_module(args, module, dm)


@register_finetune_func("pretrain_poly")
def finetune_polylib_full_with_svd_retrieval(args: ExpertConfig, dm):
    """
    Loads (the old) Poly / MHR pretrained checkoint, and fine-tunes it on the downstream task.
    """
    assert args.checkpoint is not None, "Please specify a checkpoint"

    # Passing a checkpoint assumes the use of `ExpertTrainer`
    # e.g. for poly-μ and MHR-μ
    ckpt_path = get_checkpoint_path(args.checkpoint)
    expert = load_expert(ckpt_path)
    module = ExpertTrainer(**vars(expert.training_config))

    ckpt = torch.load(ckpt_path)
    result = module.load_state_dict(ckpt["state_dict"], strict=False)
    assert len(result.unexpected_keys) == 0, result.unexpected_keys

    # For Poly and MHR, apply potential averaging, or resizing
    if args.finetune_type and args.finetune_type == "MuZ":
        module.model.switch_selector_to_average()
    elif expert.training_config.model_modifier == "poly":
        module.model.resize_module_logits(1)

    module.to("cuda")
    checkpoint = train_module(args, module, dm)
    return load_expert_from_checkpoint(checkpoint)


@register_finetune_func("joint")
def finetune_joint(args: ExpertConfig, dm):
    """
    Finetunes a pretrained shared model
    """
    from projects.wiki_experts.src.evolution.transfer_matrix import resolve_hf_repo_id

    hf_repo_id, expert_name = resolve_hf_repo_id(args.hf_lib_id)
    library = HFExpertLibrary(hf_repo_id)
    expert: Expert = library[expert_name]
    pretrain_args = expert.training_config
    module = ExpertTrainer(**vars(pretrain_args))
    module.load_state_dict(expert.expert_weights)

    return train_module(args, module, dm)


def run_multitask(args: ExpertConfig):
    seed_everything(args.seed, workers=True)

    # get directory of the current file
    setup_logging(args.output_dir)
    logger.info("Args: {}".format(args.to_json()))

    if args.hf_token_hub:
        login(token=args.hf_token_hub)

    # select dataloader
    dm = get_datamodule(args)
    args.n_tasks = len(dm._task_names)

    if args.checkpoint is not None:
        # Passing a checkpoint assumes the use of `ExpertTrainer`
        # e.g. for poly-μ and MHR-μ
        ckpt_path = get_checkpoint_path(args.checkpoint)
        expert = load_expert(ckpt_path)
        module = ExpertTrainer(**vars(expert.training_config))

        ckpt = torch.load(ckpt_path)
        result = module.load_state_dict(ckpt["state_dict"], strict=False)
        assert len(result.unexpected_keys) == 0, result.unexpected_keys

        # For Poly and MHR, apply potential averaging, or resizing
        if args.finetune_type and args.finetune_type == "MuZ":
            module.model.switch_selector_to_average()
        elif expert.training_config.model_modifier == "poly":
            module.model.resize_module_logits(1)
        checkpoint = train_module(args, module, dm)
        if args.create_transfer_matrix:
            create_transfer_matrix(args, checkpoint)

    else:
        # fine-tuning with expert library
        assert args.finetune_regime in FINETUNE_FUNCTIONS
        expert = FINETUNE_FUNCTIONS[args.finetune_regime](args, dm)

        if args.create_transfer_matrix or "nevergrad" in args.finetune_regime:
            create_transfer_matrix(args, expert)
        shutil.rmtree(f"/tmp/{args.hf_lib_id}", ignore_errors=True)


@register_finetune_func("poly_from_scratch")
def finetune_polylib_full(args: ExpertConfig, dm):
    """
    Trains poly from scratch, fine- or coarsegrained
    """

    if (
        "module_logits" not in args.trainable_param_names
        and "selector" in args.trainable_param_names
    ):
        args.trainable_param_names += "|.*module_logits.*|.*selector.*"
    assert args.hf_lib_id is None
    args.router_selector = "poly_router"
    module = MoETrainer(**vars(args), device_map="auto")
    module.to("cuda")
    return train_module(args, module, dm)


def train_module(args: ExpertConfig, module: ExpertTrainer, dm):
    loggers = get_pl_loggers(args)
    callbacks = get_monitors(args)

    monitor = "val/loss"
    mode = "min"

    if "rouge" in args.es_metric:  # early stop on Rouge
        monitor = "val/rougeL"
        mode = "max"

    elif "downstream" in args.es_metric:  # early stop on downstream eval
        monitor = f"downstream/{args.finetune_task_name}"
        mode = "max"

    try:
        dm_for_gen = get_datamodule(args, for_generation=True)
        rouge_callback = RougeCallback(
            datamodule=dm_for_gen,
        )
        callbacks.append(rouge_callback)
    except:
        logger.warn("Deactivating rouge callback. Exception thrown.")
        if "rouge" in args.es_metric:
            raise ValueError(
                "Cannot stop on Rouge if no rouge callback is present! An exception was encountered while trying to load it."
            )

    checkpoint_callback = LiveCheckpointCallback(
        dirpath=args.output_dir,
        monitor=monitor,
        save_last=True,
        mode=mode,
    )
    callbacks.append(checkpoint_callback)

    val_check_interval = args.eval_every
    if val_check_interval == -1 or val_check_interval is None:
        val_check_interval = None
    else:
        val_check_interval = args.gradient_accumulation_steps * args.eval_every
        if val_check_interval > len(dm.train_dataloader()):
            val_check_interval = len(dm.train_dataloader())
        elif val_check_interval > args.total_steps and args.total_steps != -1:
            val_check_interval = args.total_steps

    eval_callback = None
    if args.pipeline_eval_tasks:
        if args.pipeline_eval_tasks == "all":
            args.pipeline_eval_tasks = "arc-challenge,arc-easy,boolq,hellaswag,humaneval,mbpp,openbookqa,piqa,bbh-fast,winogrande"

        eval_callback = DownstreamEvalCallback(args)
        callbacks.append(eval_callback)
    else:
        logger.warn(
            "Deactivating downstream eval callback as it is not enabled in the config. Please set `pipeline_eval_tasks`."
        )

    val_check_interval = args.eval_every
    if val_check_interval == -1 or val_check_interval is None:
        val_check_interval = None
    else:
        val_check_interval = args.gradient_accumulation_steps * args.eval_every
        if val_check_interval > len(dm.train_dataloader()):
            val_check_interval = len(dm.train_dataloader())
        elif val_check_interval > args.total_steps and args.total_steps != -1:
            val_check_interval = args.total_steps

    trainer = Trainer(
        devices=-1,
        accelerator="gpu",
        logger=loggers,
        num_sanity_val_steps=0,
        default_root_dir=args.output_dir,
        max_epochs=args.num_train_epochs,
        max_steps=args.total_steps + 1 if args.total_steps != -1 else -1,
        gradient_clip_val=args.max_grad_norm,
        strategy=args.compute_strategy if args.compute_strategy else "auto",
        callbacks=callbacks,
        enable_checkpointing=False,
        log_every_n_steps=args.gradient_accumulation_steps,
        accumulate_grad_batches=args.gradient_accumulation_steps,
        precision=(
            int(args.precision) if args.precision in ["16", "32"] else args.precision
        ),
        val_check_interval=val_check_interval,
    )

    # initial validation only for a bunch of datasets... ?
    trainer.validate(module, dm)

    if args.do_train:
        trainer.fit(module, dm)

        checkpoint = (
            checkpoint_callback.best_model_path or checkpoint_callback.last_model_path
        )
        module.load_state_dict(torch.load(checkpoint)["state_dict"])
    else:
        checkpoint = None

    trainer.test(module, dm)
    return checkpoint


if __name__ == "__main__":
    args = ExpertConfig.parse()
    run_multitask(args)

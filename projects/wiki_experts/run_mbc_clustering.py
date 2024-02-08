import os
import json
import shutil
from collections import OrderedDict
from mttl.models.modifiers.expert_containers.expert_library import (
    HFExpertLibrary,
    LocalExpertLibrary,
)
from mttl.models.modifiers.expert_containers.library_transforms import (
    TiesMerge,
    TiesMergeConfig,
    WeightedLinearMerge,
    WeightedLinearMergeConfig,
    DatasetCentroidComputer,
    PrototypeComputerConfig,
    MBClusteringTransformConfig,
    MBCWithCosSimTransform,
)
from mttl.utils import logger
from projects.wiki_experts.src.config import ExpertConfig


def main(args: ExpertConfig):
    library = HFExpertLibrary(args.hf_lib_id)

    # making local copy of remote lib
    destination = args.local_libs_path + args.hf_lib_id
    os.makedirs(destination, exist_ok=True)
    library = LocalExpertLibrary.create_from_remote(library, destination=destination)

    cfg = MBClusteringTransformConfig(
        k=args.k, random_state=42, sparsity_threshold=0.1, recompute_embeddings=False
    )
    transform = MBCWithCosSimTransform(cfg)
    clusters = transform.transform(library)

    output_json_file = (
        f"{os.path.dirname(os.path.realpath(__file__))}/task_sets/{args.hf_lib_id}/"
    )
    os.makedirs(output_json_file, exist_ok=True)
    filename = f"{args.k}MBC.json"
    cluster_dict = {}
    for c, l in clusters.items():
        print(f"Cluster {c} has {len(l)} elements")
        print(f"c{c}o{args.k} = {l}")
        cluster_dict[f"c{c}o{args.k}"] = l
    with open(output_json_file + f"/{filename}", "w") as f:
        json.dump(cluster_dict, f, indent=4)
    logger.info(f"Saved clusters to {output_json_file}/{filename}")


if __name__ == "__main__":
    args = ExpertConfig.parse()
    main(args)

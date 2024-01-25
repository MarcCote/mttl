# unit test for adapter_ranker
import os
import pytest
import asyncio
from mttl.models.modifiers.expert_containers.expert_library import HFExpertLibrary


def test_expert_lib(mocker):
    library = HFExpertLibrary("sordonia/test-library")

    assert len(library) == 2
    assert not library._sliced

    module_dump = library["abstract_algebra"]

    library._upload_metadata = mocker.MagicMock()
    library._upload_weights = mocker.MagicMock()
    library._update_readme = mocker.MagicMock()

    # expert already there
    with pytest.raises(ValueError):
        library.add_expert(module_dump, "abstract_algebra")

    assert module_dump.expert_info.model == "phi-2"
    assert len(module_dump.expert_weights) == 128
    assert module_dump.expert_info.parent_node is None
    assert module_dump.expert_info.expert_name == "abstract_algebra"

    library.add_expert(module_dump, "new_module")
    assert library._upload_metadata.call_count == 1
    assert library._upload_weights.call_count == 1
    assert library._update_readme.call_count == 1
    assert len(library) == 3

    library = HFExpertLibrary(
        "sordonia/test-library", model_name="EleutherAI/other-model"
    )
    assert len(library) == 0
    assert library._sliced

    library = HFExpertLibrary(
        "sordonia/test-library", exclude_selection=["abstract_algebra"]
    )

    assert len(library) == 1
    assert library._sliced

    with pytest.raises(ValueError):
        module_dump = library["abstract_algebra"]


def test_soft_delete(mocker):
    from mttl.models.modifiers.expert_containers.expert_library import HFExpertLibrary

    # read the stored embeddings
    library = HFExpertLibrary("sordonia/test-library", create=False)
    assert len(library.data) == 2

    key = list(library.keys())[0]

    library._upload_metadata = mocker.MagicMock()
    library._update_readme = mocker.MagicMock()
    library.remove_expert(key, soft_delete=True)
    assert len(library.data) == 1
    assert key not in library.data
    assert library._upload_metadata.call_count == 1
    assert library._update_readme.call_count == 1

    library.unremove_expert(key)
    assert len(library.data) == 2


def test_compute_embeddings():
    from mttl.models.modifiers.expert_containers.library_transforms import (
        SVDEmbeddingTransform,
        SVDEmbeddingTransformConfig,
    )

    embeddings, svd = SVDEmbeddingTransform(
        SVDEmbeddingTransformConfig(n_components=2)
    ).transform("sordonia/test-library", upload_to_hf=False)
    assert embeddings.shape[1] == 2


def test_read_embeddings():
    from mttl.models.modifiers.expert_containers.expert_library import HFExpertLibrary

    # read the stored embeddings
    embeddings = HFExpertLibrary("sordonia/test-library").get_auxiliary_data(
        "embeddings"
    )
    assert "abstract_algebra" in embeddings
    assert embeddings["abstract_algebra"]["svd"]["embedding"].shape[1] == 2


def test_add_auxiliary_data(mocker, tmp_path):
    from mttl.models.modifiers.expert_containers.expert_library import (
        HFExpertLibrary,
        LocalExpertLibrary,
    )

    # read the stored embeddings
    library = LocalExpertLibrary.create_from_remote(
        HFExpertLibrary("sordonia/test-library"), tmp_path
    )

    library.add_auxiliary_data(
        data_type="test",
        expert_name="abstract_algebra",
        config={"name": "test_expert"},
        data={"test": 1},
    )
    assert (
        library.get_auxiliary_data("test", expert_name="abstract_algebra")[
            "test_expert"
        ]["test"]["test"]
        == 1
    )

token = os.getenv("BLOB_STORAGE_TOKEN")

def test_BlobStorageEngine():
    from mttl.models.modifiers.expert_containers.expert_library import (
        BlobStorageEngine,
    )

    container_name = "mhr"
    blob_file_name = "blob_data.txt"

    engine = BlobStorageEngine()
    engine.login(token)

    repo_files = engine.list_repo_files(container_name)
    blob_data_path = engine.hf_hub_download(container_name, blob_file_name)
    assert blob_data_path == f"/tmp/{container_name}/{blob_file_name}"
    with open(blob_data_path, "rb") as f:
        assert f.read() == b"Blob data"
    assert [f["name"] for f in repo_files] == ['blob_data.txt', 'blob_data.txt_data']


def test_async_download_blob_to_file():
    from mttl.models.modifiers.expert_containers.expert_library import (
        BlobStorageEngine,
    )
    container_name = "mhr"
    blob_file_name = "blob_data.txt"
    engine = BlobStorageEngine()
    engine.login(token)
    blob_data_path = asyncio.run(engine.async_download_blob_to_file(container_name, blob_file_name))
    assert blob_data_path == f"/tmp/{container_name}/{blob_file_name}"
    with open(blob_data_path, "rb") as f:
        assert f.read() == b"Blob data"


def test_snapshot_download():
    from mttl.models.modifiers.expert_containers.expert_library import (
        BlobStorageEngine,
    )
    container_name = "mhr"
    engine = BlobStorageEngine()
    engine.login(token)
    blob_data_paths = engine.snapshot_download(container_name)
    assert blob_data_paths == [f"/tmp/{container_name}/blob_data.txt", f"/tmp/{container_name}/blob_data.txt_data"]


def test_push_data_blob_storage(tmp_path):
    sas_url = token
    from azure.storage.blob import BlobServiceClient
    blob_service_client = BlobServiceClient(sas_url)
    # requires list permission
    # list(blob_service_client.list_containers())

    container_name = "mhr"
    container_client = blob_service_client.get_container_client(container_name)
    list(container_client.list_blobs())

    blob_file_name = "blob_data.txt"
    local_data_path = f"{tmp_path}/{blob_file_name}"
    # Write a temporary file to upload
    with open(local_data_path, "wb") as my_blob:
        my_blob.write(b"Blob data")

    with open(local_data_path, "rb") as my_blob:
        container_client.upload_blob(blob_file_name, my_blob, overwrite=True)

# Copyright 2022 Cerebras Systems.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""PyTorch HDF5 Dataset."""

import json
import math
import os
import random
from pathlib import Path
from typing import List, Optional, Union

import h5py
import numpy as np
import torch

from cerebras.modelzoo.common.input_utils import get_streaming_batch_size
from cerebras.modelzoo.config import BaseConfig
from cerebras.modelzoo.data.common.input_utils import (
    cluster_config,
    shard_list_of_chunks_contiguous,
)


class HDF5IterableDatasetConfig(BaseConfig):
    data_dir: Union[str, List[str]] = ...
    "Path to dataset HDF5 files"

    batch_size: int = ...
    "Batch size."

    shuffle: bool = False
    "Flag to enable data shuffling."

    shuffle_seed: Optional[int] = None
    "Shuffle seed."

    num_workers: int = 0
    "How many subprocesses to use for data loading."

    drop_last: bool = True
    """If True and the dataset size is not divisible
    by the batch size, the last incomplete batch will be dropped."""

    use_vsl: bool = False
    """
    Flag to enable variable sequence length training.
    It requires the dataset to have two extra features: the
    `attention_span` of keys and the `position_ids` of tokens.
    """

    features_list: List[str] = ["input_ids", "attention_mask", "labels"]
    "List of features to include in the batch"


class HDF5IterableDataset(torch.utils.data.IterableDataset):
    """
    A HDF5 dataset processor. Loads data from HDF5 files.

    Args:
        config: The configuration object for the dataset
    """

    def __init__(self, config: HDF5IterableDatasetConfig):
        if isinstance(config, dict):
            config = HDF5IterableDatasetConfig(**config)

        super().__init__()

        self.data_dir = config.data_dir
        self.batch_size = get_streaming_batch_size(config.batch_size)

        if config.batch_size % self.batch_size != 0:
            raise ValueError(
                f"\"{self.__class__.__name__}\" requires the global batch size "
                f"{config.batch_size} to be divisible by num_csx."
            )

        self.shuffle = config.shuffle
        self.shuffle_seed = config.shuffle_seed

        self.num_workers = config.num_workers
        self.drop_last = config.drop_last
        self.use_vsl = config.use_vsl

        self.features_list = config.features_list
        self.num_feature_groups = 1
        # Load feature names from data_params.json, if present and
        # has the correct format (generated by HF > HDF5 converter script)
        if not isinstance(self.data_dir, list) and os.path.exists(
            os.path.join(self.data_dir, "data_params.json")
        ):
            try:
                with open(
                    os.path.join(self.data_dir, "data_params.json"), 'r'
                ) as _fin:
                    data_params = json.load(_fin)
                    if "features" in data_params:
                        self.features_list = data_params["features"]
                    elif "data_0_features" in data_params:
                        self.features_list = [data_params["data_0_features"]]
                        i = 1
                        while f"data_{i}_features" in data_params:
                            self.features_list.append(
                                data_params[f"data_{i}_features"]
                            )
                            i += 1
                        self.num_feature_groups = i
            except:
                pass

        if self.use_vsl:
            self.features_list = [
                "input_ids",
                "attention_mask",
                "labels",
                "attention_span",
                "position_ids",
            ]

        if self.batch_size <= 0:
            raise ValueError(
                f"Batch size should be a positive number, but got value {self.batch_size}."
            )

        if not isinstance(self.data_dir, list):
            self.data_dir = [self.data_dir]

        self.files = []
        for directory in self.data_dir:
            p = Path(directory)
            if not p.is_dir():
                raise FileNotFoundError(
                    f"The path {directory} does not exist or is not a directory."
                )
            self.files.extend(p.glob('*.h5'))

        self.files = sorted(self.files)
        if not self.files:
            raise RuntimeError("No .h5 dataset files found.")

        cluster_spec, worker_spec = cluster_config()
        self.num_tasks = cluster_spec.num_tasks()
        self.task_id = worker_spec.rank

        # initialize state with 0 samples seen and shard_index = task_id
        self.set_state(0, self.task_id)

    def set_state(self, samples_seen, shard_index):
        """
        This method sets the state of the dataloader's samples_seen variable that controls
        how many samples are to be skipped for determinisitic restart.
        This is called by the load_state_dict method of the RestartableDataLoader.

        Args:
            samples_seen (int): number of samples streamed by the dataloader
            shard_index (int): the index of the shard of data that this worker
                is responsible for streaming
        """
        self._samples_seen = samples_seen
        self.shard_index = shard_index

        # Shard H5 files between the tasks and resolve the paths
        files_in_this_task = [
            str(file.resolve())
            for file in self.files[shard_index :: self.num_tasks]
        ]

        self.files_in_this_task = []
        self.num_examples_in_this_task = 0
        for file_path in files_in_this_task:
            with h5py.File(file_path, mode='r') as h5_file:
                num_examples_in_file = h5_file.attrs["n_examples"]
                self.files_in_this_task.append(
                    (file_path, num_examples_in_file)
                )
                self.num_examples_in_this_task += num_examples_in_file

        if self.shuffle:
            random.seed(self.shuffle_seed)
            random.shuffle(self.files_in_this_task)

    @property
    def samples_seen(self):
        return self._samples_seen % self.__len__()

    def _load_buffer(self, data_partitions):
        # partition id should default to 0 if not reading iter from file
        self.prev_worker_iter_index = self.samples_seen // self.batch_size
        restart_iter_partition_id = 0
        restart_iter_start_idx = 0  # start_idx should default to 0
        if self.prev_worker_iter_index > 0:
            # check total number of iterations/steps in the data partitions
            # This is required to determine the epoch of the restart iter
            worker_num_iters = self.num_examples_in_this_task // self.batch_size
            self.prev_worker_iter_index %= worker_num_iters
            iters_until_current_partition = 0
            prev_partition_offset_start_idx = 0
            current_partition_offset_start_idx = 0
            for partition_idx, partition_specs in enumerate(data_partitions):
                start_idx = partition_specs[1]
                num_examples = partition_specs[2]

                if partition_idx > 0:
                    num_examples_prev_partition = (
                        data_partitions[partition_idx - 1][2]
                        - prev_partition_offset_start_idx
                    )
                    if (
                        num_examples_prev_partition
                        - (num_examples_prev_partition // self.batch_size)
                        * self.batch_size
                    ) > 0:
                        current_partition_offset_start_idx = self.batch_size - (
                            num_examples_prev_partition
                            - (num_examples_prev_partition // self.batch_size)
                            * self.batch_size
                        )
                    else:
                        current_partition_offset_start_idx = 0
                    prev_partition_offset_start_idx = (
                        current_partition_offset_start_idx
                    )
                    num_examples_curr_partition = (
                        num_examples - current_partition_offset_start_idx
                    )
                else:
                    num_examples_curr_partition = num_examples
                    current_partition_offset_start_idx = 0

                iters_until_current_partition += np.ceil(
                    num_examples_curr_partition / self.batch_size
                )
                if (
                    self.prev_worker_iter_index
                    <= iters_until_current_partition - 1
                ):
                    restart_iter_partition_id = partition_idx
                    restart_iter_start_idx = int(
                        self.batch_size
                        * (
                            self.prev_worker_iter_index
                            - (
                                iters_until_current_partition
                                - np.ceil(
                                    num_examples_curr_partition
                                    / self.batch_size
                                )
                            )
                        )
                    )

                    restart_iter_start_idx += current_partition_offset_start_idx

                    break

        for partition_idx, partition_specs in enumerate(
            data_partitions[restart_iter_partition_id:]
        ):
            file_path = partition_specs[0]
            start_idx_org = partition_specs[1]
            num_examples = partition_specs[2]
            if self.prev_worker_iter_index > 0:
                if restart_iter_partition_id >= 0 and partition_idx == 0:
                    start_idx = restart_iter_start_idx
                else:
                    start_idx = start_idx_org
            else:
                start_idx = start_idx_org
            with h5py.File(file_path, mode='r') as h5_file:
                if self.use_vsl and h5_file["data"].shape[1] != 5:
                    raise ValueError(
                        f"Expected all dataset H5 files to have 5 features for "
                        f"variable sequence length training, but got "
                        f"{h5_file['data'].shape[1]} features in {file_path}."
                    )
                for idx in range(
                    start_idx, start_idx_org + num_examples, self.batch_size
                ):
                    load_len = min(
                        self.batch_size, start_idx_org + num_examples - idx
                    )
                    if self.num_feature_groups == 1:
                        load_data = h5_file["data"][idx : idx + load_len]
                        for i in range(load_len):
                            yield load_data[i]
                    else:
                        load_data = [None] * self.num_feature_groups
                        for i in range(self.num_feature_groups):
                            load_data[i] = h5_file[f"data_{i}"][
                                idx : idx + load_len
                            ]
                        for i in range(load_len):
                            yield tuple(
                                [
                                    load_data[j][i]
                                    for j in range(self.num_feature_groups)
                                ]
                            )

        l = self.__len__()
        self._samples_seen = l * math.ceil((self._samples_seen + 1) / l)

    def __iter__(self):
        """
        Iterating over the data to construct input features.
        """
        worker_info = torch.utils.data.get_worker_info()

        if worker_info is not None:
            worker_id = worker_info.id
            num_workers = worker_info.num_workers
        else:
            # Single-process
            worker_id = 0
            num_workers = 1

        data_partitions = shard_list_of_chunks_contiguous(
            self.files_in_this_task, worker_id, num_workers
        )

        for example in self._load_buffer(data_partitions):
            if self.num_feature_groups == 1:
                yield {
                    feature: np.array(example[i], np.int32)
                    for i, feature in enumerate(self.features_list)
                }
            else:
                sample = {}
                for j in range(self.num_feature_groups):
                    sample.update(
                        {
                            feature: np.array(example[j][i], np.int32)
                            for i, feature in enumerate(self.features_list[j])
                        }
                    )
                yield sample

    def __len__(self):
        """
        Returns the len of dataset on the task process.
        """
        return self.num_examples_in_this_task

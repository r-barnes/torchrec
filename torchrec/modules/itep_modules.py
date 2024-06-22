#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

import logging
from typing import Dict, List, Optional, Tuple

import torch
from torch import nn
from torchrec.distributed.embedding_types import ShardedEmbeddingTable
from torchrec.modules.embedding_modules import reorder_inverse_indices
from torchrec.sparse.jagged_tensor import _pin_and_move, _to_offsets, KeyedJaggedTensor

try:
    torch.ops.load_library(
        "//deeplearning/fbgemm/fbgemm_gpu:intraining_embedding_pruning"
    )
except OSError:
    pass

logger: logging.Logger = logging.getLogger(__name__)


class GenericITEPModule(nn.Module):
    """
    A generic module for applying In-Training Embedding Pruning (ITEP).
    This module can be hooked into the forward() of `EmbeddingBagCollection`.
    It will prune the embedding tables during training by applying a remapping transform
    to the embedding lookup indices.

    Args:
        table_name_to_unpruned_hash_sizes (Dict[str, int]): Map of table name to
            unpruned hash size.
        lookups (Optional[List[nn.Module]]): List of lookups in the EBC. Defaults to
            `None`.
        enable_pruning (Optional[bool]): Enable pruning or not. Defaults to `True`.
        pruning_interval (Optional[int]): Pruning interval. Defaults to `1001`.

    NOTE:
        The `lookups` argument is optional and is used in the sharded case. If not
        provided, the module will skip initialization for the dummy module.
        The `table_name_to_unpruned_hash_sizes` argument must not be empty. It is a map
        of table names to their unpruned hash sizes.

    Example::
        itep_module = GenericITEPModule(
            table_name_to_unpruned_hash_sizes={"table1": 1000, "table2": 2000},
            lookups=ShardedEmbeddingBagCollection._lookups,
            enable_pruning=True,
            pruning_interval=1001
        )
    """

    def __init__(
        self,
        table_name_to_unpruned_hash_sizes: Dict[str, int],
        lookups: Optional[List[nn.Module]] = None,
        enable_pruning: bool = True,
        pruning_interval: int = 1001,  # Default pruning interval 1001 iterations
    ) -> None:

        super(GenericITEPModule, self).__init__()

        # Construct in-training embedding pruning args
        self.enable_pruning: bool = enable_pruning
        self.pruning_interval: int = pruning_interval
        self.lookups: Optional[List[nn.Module]] = lookups
        self.table_name_to_unpruned_hash_sizes: Dict[str, int] = (
            table_name_to_unpruned_hash_sizes
        )

        # Map each feature to a physical address_lookup/row_util buffer
        self.feature_table_map: Dict[str, int] = {}
        self.table_name_to_idx: Dict[str, int] = {}
        self.buffer_offsets_list: List[int] = []
        self.idx_to_table_name: Dict[int, str] = {}
        # Prevent multi-pruning, after moving iteration counter to outside.
        self.last_pruned_iter = -1

        if self.lookups is not None:
            self.init_itep_state()
        else:
            logger.info(
                "ITEP init: no lookups provided. Skipping init for dummy module."
            )

    def print_itep_eviction_stats(
        self,
        pruned_indices_offsets: torch.Tensor,
        pruned_indices_total_length: torch.Tensor,
        cur_iter: int,
    ) -> None:
        table_name_to_eviction_ratio = {}

        num_buffers = len(self.buffer_offsets_list) - 1
        for buffer_idx in range(num_buffers):
            pruned_start = pruned_indices_offsets[buffer_idx]
            pruned_end = pruned_indices_offsets[buffer_idx + 1]
            pruned_length = pruned_end - pruned_start

            if pruned_length > 0:
                start = self.buffer_offsets_list[buffer_idx]
                end = self.buffer_offsets_list[buffer_idx + 1]
                buffer_length = end - start
                assert buffer_length > 0
                eviction_ratio = pruned_length.item() / buffer_length
                table_name_to_eviction_ratio[self.idx_to_table_name[buffer_idx]] = (
                    eviction_ratio
                )

        # Sort the mapping by eviction ratio in descending order
        sorted_mapping = dict(
            sorted(
                table_name_to_eviction_ratio.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        )
        # Print the sorted mapping
        logger.info(f"ITEP: table name to eviction ratio {sorted_mapping}")

        # Calculate percentage of indiced updated/evicted during ITEP iter
        pruned_indices_ratio = (
            pruned_indices_total_length / self.buffer_offsets_list[-1]
            if self.buffer_offsets_list[-1] > 0
            else 0
        )
        logger.info(
            f"Performed ITEP in iter {cur_iter}, evicted {pruned_indices_total_length} ({pruned_indices_ratio:%}) indices."
        )

    def get_table_hash_sizes(self, table: ShardedEmbeddingTable) -> Tuple[int, int]:
        unpruned_hash_size = table.num_embeddings

        if table.name in self.table_name_to_unpruned_hash_sizes:
            unpruned_hash_size = self.table_name_to_unpruned_hash_sizes[table.name]
        else:
            # Tables are not pruned by ITEP if table.name not in table_name_to_unpruned_hash_sizes
            unpruned_hash_size = table.num_embeddings
            logger.info(
                f"ITEP: table {table.name} not pruned, because table name is not present in table_name_to_unpruned_hash_sizes."
            )

        return (table.num_embeddings, unpruned_hash_size)

    def create_itep_buffers(
        self,
        buffer_size: int,
        buffer_offsets: List[int],
        table_names: List[str],
        emb_sizes: List[int],
    ) -> None:
        """
        Registers ITEP specific buffers in a way that can be accessed by
        `torch.ops.fbgemm.init_address_lookup` and can be individually checkpointed.
        """
        # Buffers do not enter backward pass
        with torch.no_grad():
            # Don't use register_buffer for buffer_offsets and emb_sizes because they
            # may change as the sharding plan changes between preemption/resumption
            self.buffer_offsets = torch.tensor(
                buffer_offsets, dtype=torch.int64, device=self.current_device
            )
            self.emb_sizes = torch.tensor(
                emb_sizes, dtype=torch.int64, device=self.current_device
            )

            self.address_lookup = torch.zeros(
                buffer_size, dtype=torch.int64, device=self.current_device
            )
            self.row_util = torch.zeros(
                buffer_size, dtype=torch.float32, device=self.current_device
            )

            # Register buffers
            for idx, table_name in enumerate(table_names):
                self.register_buffer(
                    f"{table_name}_itp_address_lookup",
                    self.address_lookup[buffer_offsets[idx] : buffer_offsets[idx + 1]],
                )
                self.register_buffer(
                    f"{table_name}_itp_row_util",
                    self.row_util[buffer_offsets[idx] : buffer_offsets[idx + 1]],
                )

    def init_itep_state(self) -> None:
        idx = 0
        buffer_size = 0
        # Record address_lookup/row_util buffer lengths and offsets for each feature
        buffer_offsets: List[int] = [0]  # number of buffers + 1
        table_names: List[str] = []  # number of buffers + 1
        emb_sizes: List[int] = []  # Store embedding table sizes
        self.current_device = None

        # Iterate over all tables
        # pyre-ignore
        for lookup in self.lookups:
            for emb in lookup._emb_modules:

                emb_tables: List[ShardedEmbeddingTable] = emb._config.embedding_tables
                for table in emb_tables:

                    (
                        pruned_hash_size,
                        unpruned_hash_size,
                    ) = self.get_table_hash_sizes(table)

                    # Skip tables that are not pruned, aka pruned_hash_size == unpruned_hash_size.
                    if pruned_hash_size == unpruned_hash_size:
                        continue

                    logger.info(
                        f"ITEP: Pruning enabled for table {table.name} with features {table.feature_names}, pruned_hash_size {pruned_hash_size} vs unpruned_hash_size {unpruned_hash_size}"
                    )

                    # buffer size for address_lookup and row_util
                    buffer_size += unpruned_hash_size
                    buffer_offsets.append(buffer_size)
                    table_names.append(table.name)
                    emb_sizes.append(pruned_hash_size)

                    # Create feature to table mappings
                    for feature_name in table.feature_names:
                        self.feature_table_map[feature_name] = idx

                    # Create table_name to buffer idx mappings
                    self.table_name_to_idx[table.name] = idx
                    self.idx_to_table_name[idx] = table.name
                    idx += 1

                    # Check that all features have the same device
                    if (
                        table.local_metadata is not None
                        and table.local_metadata.placement is not None
                    ):
                        if self.current_device is None:
                            self.current_device = (
                                table.local_metadata.placement.device()
                            )
                        else:
                            assert (
                                self.current_device
                                == table.local_metadata.placement.device()
                            ), f"Device of table {table}: {table.local_metadata.placement.device()} does not match existing device: {self.current_device}"

        if self.current_device is None:
            self.current_device = torch.device("cuda")

        self.buffer_offsets_list = buffer_offsets

        # Create buffers for address_lookup and row_util
        self.create_itep_buffers(
            buffer_size=buffer_size,
            buffer_offsets=buffer_offsets,
            table_names=table_names,
            emb_sizes=emb_sizes,
        )

        logger.info(
            f"ITEP: done init_state with feature_table_map {self.feature_table_map} and buffer_offsets {self.buffer_offsets_list}"
        )

        # initialize address_lookup
        torch.ops.fbgemm.init_address_lookup(
            self.address_lookup,
            self.buffer_offsets,
            self.emb_sizes,
        )

    def reset_weight_momentum(
        self,
        pruned_indices: torch.Tensor,
        pruned_indices_offsets: torch.Tensor,
    ) -> None:
        if self.lookups is not None:
            # pyre-ignore
            for lookup in self.lookups:
                for emb in lookup._emb_modules:
                    emb_tables: List[ShardedEmbeddingTable] = (
                        emb._config.embedding_tables
                    )

                    logical_idx = 0
                    logical_table_ids = []
                    buffer_ids = []
                    for table in emb_tables:
                        name = table.name
                        if name in self.table_name_to_idx:
                            buffer_idx = self.table_name_to_idx[name]
                            start = pruned_indices_offsets[buffer_idx]
                            end = pruned_indices_offsets[buffer_idx + 1]
                            length = end - start
                            if length > 0:
                                logical_table_ids.append(logical_idx)
                                buffer_ids.append(buffer_idx)
                        logical_idx += table.num_features()

                    if len(logical_table_ids) > 0:
                        emb.emb_module.reset_embedding_weight_momentum(
                            pruned_indices,
                            pruned_indices_offsets,
                            torch.tensor(
                                logical_table_ids,
                                dtype=torch.int32,
                                requires_grad=False,
                            ),
                            torch.tensor(
                                buffer_ids, dtype=torch.int32, requires_grad=False
                            ),
                        )

    # Flush UVM cache after ITEP eviction to remove stale states
    def flush_uvm_cache(self) -> None:
        if self.lookups is not None:
            # pyre-ignore
            for lookup in self.lookups:
                for emb in lookup._emb_modules:
                    emb.emb_module.flush()
                    emb.emb_module.reset_cache_states()

    def get_remap_info(self, features: KeyedJaggedTensor) -> List[torch.Tensor]:
        keys = features.keys()
        length_per_key = features.length_per_key()
        offset_per_key = features.offset_per_key()

        buffer_idx = []
        feature_lengths = []
        feature_offsets = []
        for i in range(len(keys)):
            key = keys[i]
            if key not in self.feature_table_map:
                continue
            buffer_idx.append(self.feature_table_map[key])
            feature_lengths.append(length_per_key[i])
            feature_offsets.append(offset_per_key[i])

        return [
            torch.tensor(buffer_idx, dtype=torch.int32, device=torch.device("cpu")),
            torch.tensor(
                feature_lengths, dtype=torch.int64, device=torch.device("cpu")
            ),
            torch.tensor(
                feature_offsets, dtype=torch.int64, device=torch.device("cpu")
            ),
        ]

    def get_full_values_list(self, features: KeyedJaggedTensor) -> List[torch.Tensor]:
        inverse_indices = features.inverse_indices()
        batch_size = inverse_indices[1].numel() // len(inverse_indices[0])
        keys = features.keys()
        if not all(key in self.feature_table_map for key in keys):
            keys = [key for key in keys if key in self.feature_table_map]
            key_indices = [features._key_indices()[key] for key in keys]
            features = features.permute(key_indices)
        indices = (
            inverse_indices[1]
            if keys == inverse_indices[0]
            else reorder_inverse_indices(inverse_indices, keys)
        )
        spk_tensor = _pin_and_move(
            torch.tensor(features.stride_per_key()), features.device()
        )
        offset_indices = (
            indices + _to_offsets(spk_tensor)[:-1].unsqueeze(-1)
        ).flatten()
        full_values, full_lengths = torch.ops.fbgemm.keyed_jagged_index_select_dim1(
            features.values(),
            features.lengths(),
            features.offsets(),
            offset_indices,
            features.lengths().numel(),
        )
        full_lpk = torch.sum(full_lengths.view(-1, batch_size), dim=1).tolist()
        return list(torch.split(full_values, full_lpk))

    def forward(
        self,
        sparse_features: KeyedJaggedTensor,
        cur_iter: int,
    ) -> KeyedJaggedTensor:
        """
        Args:
            sparse_features (KeyedJaggedTensor]): input embedding lookup indices to be
                remapped.
            cur_iter (int): iteration counter.

        Returns:
            KeyedJaggedTensor: remapped KJT

        NOTE:
            We use the same forward method for sharded and non-sharded case.
        """

        if not self.enable_pruning or self.lookups is None:
            return sparse_features

        num_buffers = self.buffer_offsets.size(dim=0) - 1
        if num_buffers <= 0:
            return sparse_features

        start_pruning: bool = (
            (cur_iter < 10 and (cur_iter + 1) % 3 == 0)
            or (cur_iter < 100 and (cur_iter + 1) % 30 == 0)
            or (cur_iter < 1000 and (cur_iter + 1) % 300 == 0)
            or ((cur_iter + 1) % self.pruning_interval == 0)
        )
        if start_pruning and self.training and self.last_pruned_iter != cur_iter:
            # Pruning function outputs the indices that need weight/momentum reset
            # The indices order is by physical buffer
            (
                pruned_indices,
                pruned_indices_offsets,
                pruned_indices_total_length,
            ) = torch.ops.fbgemm.prune_embedding_tables(
                cur_iter,
                self.pruning_interval,
                self.address_lookup,
                self.row_util,
                self.buffer_offsets,
                self.emb_sizes,
            )
            # After pruning, reset weight and momentum of pruned indices
            if pruned_indices_total_length > 0 and cur_iter > self.pruning_interval:
                self.reset_weight_momentum(pruned_indices, pruned_indices_offsets)

            if pruned_indices_total_length > 0:
                # Flush UVM cache after every ITEP eviction (every pruning_interval iterations)
                self.flush_uvm_cache()
                logger.info(
                    f"ITEP: trying to flush UVM after ITEP eviction, {cur_iter=}"
                )

            self.last_pruned_iter = cur_iter

            # Print eviction stats
            self.print_itep_eviction_stats(
                pruned_indices_offsets, pruned_indices_total_length, cur_iter
            )

        (
            buffer_idx,
            feature_lengths,
            feature_offsets,
        ) = self.get_remap_info(sparse_features)

        update_utils: bool = (
            (cur_iter < 10)
            or (cur_iter < 100 and (cur_iter + 1) % 19 == 0)
            or ((cur_iter + 1) % 39 == 0)
        )
        full_values_list = None
        if update_utils and sparse_features.variable_stride_per_key():
            if sparse_features.inverse_indices_or_none() is not None:
                # full util update mode require reconstructing original input indicies from VBE input
                full_values_list = self.get_full_values_list(sparse_features)
            else:
                logger.info(
                    "Switching to deduped util updating mode due to features missing inverse indices. "
                    f"features {list(sparse_features.keys())=} with variable stride: {sparse_features.variable_stride_per_key()}"
                )

        remapped_values = torch.ops.fbgemm.remap_indices_update_utils(
            cur_iter,
            buffer_idx,
            feature_lengths,
            feature_offsets,
            sparse_features.values(),
            self.address_lookup,
            self.row_util,
            self.buffer_offsets,
            full_values_list=full_values_list,
        )

        sparse_features._values = remapped_values

        return sparse_features

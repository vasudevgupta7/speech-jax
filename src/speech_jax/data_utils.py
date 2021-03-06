from typing import Callable, Dict, Optional, Tuple, Union

import datasets
import jax.numpy as jnp
import tensorflow as tf

from speech_jax.tf_dataset import TFDatasetReader


class IterableDataLoader:
    def __init__(
        self,
        dataset: Union[datasets.IterableDataset, TFDatasetReader],
        batch_size: int = 1,
        collate_fn: Optional[Callable] = None,
    ):
        self.dataset = dataset
        self.batch_size = batch_size
        self.collate_fn = collate_fn

    def __iter__(
        self,
    ) -> Union[Tuple[jnp.DeviceArray], Dict[str, jnp.DeviceArray], jnp.DeviceArray]:
        batch = []
        for i, sample in enumerate(self.dataset):
            batch.append(sample)

            if (i + 1) % self.batch_size == 0:
                if self.collate_fn is not None:
                    batch = self.collate_fn(batch)

                yield batch
                batch = []

    def shuffle(self, seed: int):
        self.dataset.set_epoch(seed)

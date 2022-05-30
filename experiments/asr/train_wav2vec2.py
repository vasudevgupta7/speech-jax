import dataclasses
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Tuple

import flax
import jax
import jax.numpy as jnp
import numpy as np
import optax
from flax.training import train_state
from transformers import (FlaxWav2Vec2ForCTC, Wav2Vec2CTCTokenizer,
                          Wav2Vec2FeatureExtractor)

from speech_jax import training
from speech_jax.training import TrainingStepOutput, ValidationStepOutput
from speech_jax.tx_utils import create_tx

from transformers.models.wav2vec2.modeling_flax_wav2vec2 import _compute_mask_indices

# masked_indices = _compute_mask_indices((2, 32), 0.05, 10)
# print(masked_indices)
# exit()

# TODO:
# hf-flax spec augmentation is not that great
# let's implement by ourselves

class TrainState(train_state.TrainState):
    loss_fn: Callable = flax.struct.field(pytree_node=False)
    get_feat_extract_output_lengths: Callable = flax.struct.field(pytree_node=False)


def training_step(
    state: train_state.TrainState,
    drp_rng: jnp.DeviceArray,
    batch: Dict[str, jnp.DeviceArray],
) -> TrainingStepOutput:
    new_drp_rng, drp_rng = jax.random.split(drp_rng, num=2)

    def loss_fn(params):
        labels = batch.pop("labels")
        label_paddings = batch.pop("label_paddings")

        outputs = state.apply_fn(
            **batch,
            params=params,
            dropout_rng=drp_rng,
            train=True,
            freeze_feature_encoder=True
        )
        seqlen = outputs.logits.shape[1]

        input_lengths = jnp.sum(batch["attention_mask"], axis=1)
        input_lengths = state.get_feat_extract_output_lengths(input_lengths)
        logit_paddings = input_lengths[..., None] <= jnp.arange(seqlen)

        # taking mean is fine as long as batches are equally distributed
        return state.loss_fn(
            outputs.logits, logit_paddings, labels, label_paddings
        ).mean()

    grad_fn = jax.value_and_grad(loss_fn)
    loss, grads = grad_fn(state.params)

    loss = jax.lax.pmean(loss, axis_name="batch")
    grads = jax.lax.pmean(grads, axis_name="batch")

    new_state = state.apply_gradients(grads=grads)

    return TrainingStepOutput(
        state=new_state,
        dropout_rng=new_drp_rng,
        loss=loss,
    )


def validation_step(state: train_state.TrainState, batch: Dict[str, jnp.DeviceArray]) -> ValidationStepOutput:
    labels = batch.pop("labels")
    label_paddings = batch.pop("label_paddings")

    input_lengths = jnp.sum(batch["attention_mask"], axis=1)
    input_lengths = state.get_feat_extract_output_lengths(input_lengths)
    
    outputs = state.apply_fn(**batch, params=state.params, train=False)

    seqlen = outputs.logits.shape[1]
    logit_paddings = input_lengths[..., None] <= jnp.arange(seqlen)

    loss = state.loss_fn(outputs.logits, logit_paddings, labels, label_paddings).mean()
    loss = jax.lax.pmean(loss, axis_name="batch")

    return ValidationStepOutput(loss=loss)


@dataclasses.dataclass
class SpecAugmentConfig:
    shape: Tuple[int, int]
    mask_time_prob: float = 0.05
    mask_time_span: int = 10
    min_masks: int = 0

@dataclasses.dataclass
class DataCollator:
    feature_extractor: Wav2Vec2FeatureExtractor
    tokenizer: Wav2Vec2CTCTokenizer
    audio_maxlen: Optional[int] = None
    text_maxlen: Optional[int] = None
    spec_augment_config: Optional[SpecAugmentConfig] = None
    get_feat_extract_output_lengths: Callable = None

    def __call__(self, batch: List[Dict[str, Any]]):
        audio = [sample["audio"]["array"] for sample in batch]
        text = [sample["text"] for sample in batch]

        # TODO: explore other padding options in JAX (special dynamic padding?)
        audio = self.feature_extractor(
            audio,
            padding="max_length",
            max_length=self.audio_maxlen,
            truncation=True,
            return_tensors="np",
            sampling_rate=16000,
        )
        targets = self.tokenizer(
            text,
            max_length=self.text_maxlen,
            truncation=True,
            padding="max_length",
            return_tensors="np",
        )
        labels = targets["input_ids"]
        label_paddings = (targets["attention_mask"] == 0).astype(np.int32)

        outputs = {
            "input_values": audio["input_values"],
            "attention_mask": audio["attention_mask"],
            "labels": labels,
            "label_paddings": label_paddings,
        }

        if self.spec_augment_config is not None:
            # batch_size, audio_seqlen
            input_lengths = np.sum(audio["attention_mask"], axis=1)
            # -> batch_size
            assert self.get_feat_extract_output_lengths is not None
            input_lengths = self.get_feat_extract_output_lengths(input_lengths)
            # -> batch_size
            seqlen = self.get_feat_extract_output_lengths(self.audio_maxlen)
            attention_mask = input_lengths[:, None] > np.arange(seqlen)
            # print(input_lengths)
            # print(attention_mask)

            outputs["mask_time_indices"] = _compute_mask_indices(
                self.spec_augment_config.shape,
                self.spec_augment_config.mask_time_prob,
                self.spec_augment_config.mask_time_span,
                attention_mask=attention_mask,
                min_masks=self.spec_augment_config.min_masks,
            )

        #     print(outputs["mask_time_indices"])

        # exit()

        return outputs


@dataclasses.dataclass
class TrainerConfig(training.TrainerConfig):
    lr: float
    weight_decay: float

print(jax.devices())

model_id = "facebook/wav2vec2-large-lv60"
model = FlaxWav2Vec2ForCTC.from_pretrained(model_id)

trainer_config = TrainerConfig(
    max_epochs=30,
    lr=5e-5,
    weight_decay=1e-4,
    train_batch_size_per_device=1,
    eval_batch_size_per_device=1, # TODO this is not supported
    wandb_project_name="speech-JAX",
    epochs_save_dir="epochs-960h",
    logging_steps=8,
)

feature_extractor = Wav2Vec2FeatureExtractor.from_pretrained(model_id)
tokenizer = Wav2Vec2CTCTokenizer.from_pretrained(model_id)

audio_maxlen, text_maxlen = 246000, 256
train_batch_size = trainer_config.train_batch_size_per_device * jax.device_count()
spec_augment_config = SpecAugmentConfig(
    (train_batch_size, model._get_feat_extract_output_lengths(audio_maxlen)),
    mask_time_prob=0.05,
    mask_time_span=7,
    min_masks=15,
)
print(train_batch_size, model._get_feat_extract_output_lengths(audio_maxlen))

collate_fn = DataCollator(
    feature_extractor, tokenizer, audio_maxlen=audio_maxlen, text_maxlen=text_maxlen,
    spec_augment_config=spec_augment_config,
    get_feat_extract_output_lengths=model._get_feat_extract_output_lengths,
)

def hf_save_fn(save_dir, params, model_save_fn, feature_extractor_save_fn, tokenizer_save_fn, push_to_hub=False):
    model_save_fn(save_dir, params=params, push_to_hub=push_to_hub)
    feature_extractor_save_fn(save_dir, push_to_hub=push_to_hub)
    tokenizer_save_fn(save_dir, push_to_hub=push_to_hub)

save_fn = partial(
    hf_save_fn,
    model_save_fn=model.save_pretrained,
    feature_extractor_save_fn=feature_extractor.save_pretrained,
    tokenizer_save_fn=tokenizer.save_pretrained,
    push_to_hub=False,
)


trainer = training.Trainer(
    config=trainer_config,
    training_step=training_step,
    validation_step=validation_step,
    pmap_kwargs={"axis_name": "batch", "donate_argnums": (0, 1)},
    collate_fn=collate_fn,
    model_save_fn=save_fn,
)


from datasets import interleave_datasets, load_dataset

train_data = [
    load_dataset("librispeech_asr", "clean", split="train.100", streaming=True),
    # load_dataset("librispeech_asr", "clean", split="train.360", streaming=True),
    # load_dataset("librispeech_asr", "other", split="train.500", streaming=True),
]
train_data = interleave_datasets(train_data)
val_data = load_dataset("librispeech_asr", "clean", split="validation", streaming=True)

state = TrainState.create(
    apply_fn=model.__call__,
    params=model.params,
    tx=create_tx(trainer_config.lr, trainer_config.weight_decay),
    loss_fn=partial(optax.ctc_loss, blank_id=tokenizer.pad_token_id),
    get_feat_extract_output_lengths=model._get_feat_extract_output_lengths,
)

try:
    new_state = trainer.train(state, train_data, val_data)
except KeyboardInterrupt:
    print("Interrupting training through KEYBOARD!!")

# Copyright 2018 The Google AI Language Team Authors and
# The HuggingFace Inc. team.
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

import os
from typing import Dict, Optional

import numpy as np
import torch
from omegaconf import DictConfig, OmegaConf
from pytorch_lightning import Trainer

from nemo.collections.common.losses import CrossEntropyLoss, MSELoss
from nemo.collections.nlp.data.glue_benchmark.glue_benchmark_dataset import GLUE_TASKS_NUM_LABELS, GLUEDataset
from nemo.collections.nlp.models.glue_benchmark.metrics_for_glue import compute_metrics
from nemo.collections.nlp.modules.common import SequenceClassifier, SequenceRegression
from nemo.collections.nlp.modules.common.lm_utils import get_lm_model
from nemo.collections.nlp.modules.common.tokenizer_utils import get_tokenizer
from nemo.collections.nlp.parts.utils_funcs import list2str, tensor2list
from nemo.core.classes import typecheck
from nemo.core.classes.modelPT import ModelPT
from nemo.core.neural_types import NeuralType
from nemo.utils import logging

__all__ = ['GLUEModel']

'''
Some transformer of this code were adapted from the HuggingFace library at
https://github.com/huggingface/transformers
Example of running a pretrained BERT model on the 9 GLUE tasks, read more
about GLUE benchmark here: https://gluebenchmark.com
Download the GLUE data by running the script:
https://gist.github.com/W4ngatang/60c2bdb54d156a41194446737ce03e2e

Some of these tasks have a small dataset and training can lead to high variance
in the results between different runs. Below is the median on 5 runs
(with different seeds) for each of the metrics on the dev set of the benchmark
with an uncased BERT base model (the checkpoint bert-base-uncased)
(source https://github.com/huggingface/transformers/tree/master/examples#glue).
Task	Metric	                        Result
CoLA	Matthew's corr	                48.87
SST-2	Accuracy	                    91.74
MRPC	F1/Accuracy	                 90.70/86.27
STS-B	Person/Spearman corr.	     91.39/91.04
QQP	    Accuracy/F1	                 90.79/87.66
MNLI	Matched acc./Mismatched acc. 83.70/84.83
QNLI	Accuracy	                    89.31
RTE	    Accuracy	                    71.43
WNLI	Accuracy	                    43.66

'''


class GLUEModel(ModelPT):
    @property
    def input_types(self) -> Optional[Dict[str, NeuralType]]:
        return self.bert_model.input_types

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        return self.pooler.output_types

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        """
        Initializes model to use BERT model for GLUE tasks.
        """
        self.data_dir = cfg.dataset.data_dir
        if not os.path.exists(self.data_dir):
            raise FileNotFoundError(
                "GLUE datasets not found. For more details on how to get the data, see: "
                "https://gist.github.com/W4ngatang/60c2bdb54d156a41194446737ce03e2e"
            )

        if cfg.task_name not in cfg.supported_tasks:
            raise ValueError(f'{cfg.task_name} not in supported task. Choose from {cfg.supported_tasks}')
        self.task_name = cfg.task_name

        # MNLI task has two separate dev sets: matched and mismatched
        cfg.train_ds.file_name = os.path.join(self.data_dir, cfg.train_ds.file_name)
        if self.task_name == "mnli":
            cfg.validation_ds.file_name = [
                os.path.join(self.data_dir, 'dev_matched.tsv'),
                os.path.join(self.data_dir, 'dev_mismatched.tsv'),
            ]
        else:
            cfg.validation_ds.file_name = os.path.join(self.data_dir, cfg.validation_ds.file_name)
        logging.info(f'Using {cfg.validation_ds.file_name} for model evaluation.')

        if cfg.language_model.bert_config_file is not None:
            logging.info(
                (
                    f"HuggingFace BERT config file found. "
                    f"LM will be instantiated from: {cfg.language_model.bert_config_file}"
                )
            )
            self.vocab_size = json.load(open(cfg.language_model.bert_config_file))['vocab_size']
        elif cfg.language_model.bert_config and cfg.language_model.bert_config.vocab_size is not None:
            self.vocab_size = cfg.language_model.bert_config.vocab_size
        else:
            self.vocab_size = None

        cfg.tokenizer.vocab_size = self.vocab_size
        self._setup_tokenizer(cfg.tokenizer)

        super().__init__(cfg=cfg, trainer=trainer)

        num_labels = GLUE_TASKS_NUM_LABELS[self.task_name]

        self.bert_model = get_lm_model(
            model_type=cfg.language_model.model_type,
            pretrained_model_name=cfg.language_model.pretrained_model_name,
            config_file=cfg.language_model.bert_config_file,
            config_dict=OmegaConf.to_container(cfg.language_model.bert_config)
            if cfg.language_model.bert_config
            else None,
            checkpoint_file=cfg.language_model.bert_checkpoint,
        )
        self.hidden_size = self.bert_model.hidden_size

        # uses [CLS] token for classification (the first token)
        if self.task_name == "sts-b":
            self.pooler = SequenceRegression(hidden_size=self.hidden_size)
            self.loss = MSELoss()
        else:
            self.pooler = SequenceClassifier(hidden_size=self.hidden_size, num_classes=num_labels, log_softmax=False)
            self.loss = CrossEntropyLoss()

        # Optimizer setup needs to happen after all model weights are ready
        self.setup_optimization(cfg.optim)

    @typecheck()
    def forward(self, input_ids, token_type_ids, attention_mask):
        hidden_states = self.bert_model(
            input_ids=input_ids, token_type_ids=token_type_ids, attention_mask=attention_mask
        )
        output = self.pooler(hidden_states=hidden_states)
        return output

    def training_step(self, batch, batch_idx):
        input_ids, input_type_ids, input_mask, labels = batch
        model_output = self(input_ids=input_ids, token_type_ids=input_type_ids, attention_mask=input_mask)

        if self.task_name == "sts-b":
            loss = self.loss(preds=model_output, labels=labels)
        else:
            loss = self.loss(logits=model_output, labels=labels)
        tensorboard_logs = {'train_loss': loss, 'lr': self._optimizer.param_groups[0]['lr']}
        return {'loss': loss, 'log': tensorboard_logs}

    def validation_step(self, batch, batch_idx, dataloader_idx=0):
        input_ids, input_type_ids, input_mask, labels = batch
        model_output = self(input_ids=input_ids, token_type_ids=input_type_ids, attention_mask=input_mask)

        if self.task_name == "sts-b":
            val_loss = self.loss(preds=model_output, labels=labels)
        else:
            val_loss = self.loss(logits=model_output, labels=labels)

        if self.task_name != 'sts-b':
            model_output = torch.argmax(model_output, 1)

        eval_tensors = {'preds': model_output, 'labels': labels}
        tensorboard_logs = {'val_loss': val_loss}
        return {'val_loss': val_loss, 'log': tensorboard_logs, 'eval_tensors': eval_tensors}

    def multi_validation_epoch_end(self, outputs, dataloader_idx: int = 0):
        """
        Called at the end of validation to aggregate outputs.
        outputs: list of individual outputs of each validation step.
        """
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()
        preds = torch.cat([x['eval_tensors']['preds'] for x in outputs])
        labels = torch.cat([x['eval_tensors']['labels'] for x in outputs])

        all_preds = []
        all_labels = []
        if torch.distributed.is_initialized():
            world_size = torch.distributed.get_world_size()
            for ind in range(world_size):
                all_preds.append(torch.empty_like(preds))
                all_labels.append(torch.empty_like(labels))
            torch.distributed.all_gather(all_preds, preds)
            torch.distributed.all_gather(all_labels, labels)
        else:
            all_preds.append(preds)
            all_labels.append(labels)

        tensorboard_logs = {}
        if not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0:
            preds = []
            labels = []
            for p in all_preds:
                preds.extend(tensor2list(p))
            for l in all_labels:
                labels.extend(tensor2list(l))

            tensorboard_logs = compute_metrics(self.task_name, np.array(preds), np.array(labels))
            logging.info(f'{self._validation_names[dataloader_idx].upper()} evaluation: {tensorboard_logs}')

            # writing labels and predictions to a file in output_dir is specified in the config
            output_dir = self._cfg.output_dir
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
                filename = os.path.join(output_dir, self.task_name + '.txt')
                logging.info(f'Saving labels and predictions to {filename}')
                with open(filename, 'w') as f:
                    f.write('labels\t' + list2str(labels) + '\n')
                    f.write('preds\t' + list2str(preds) + '\n')

        tensorboard_logs['val_loss'] = avg_loss
        return {'val_loss': avg_loss, 'log': tensorboard_logs}

    def _setup_tokenizer(self, cfg: DictConfig):
        tokenizer = get_tokenizer(
            tokenizer_name=cfg.tokenizer_name,
            data_file=cfg.data_file,
            tokenizer_model=cfg.tokenizer_model,
            sample_size=cfg.sample_size,
            special_tokens=OmegaConf.to_container(cfg.special_tokens) if cfg.special_tokens else None,
            vocab_file=cfg.vocab_file,
            vocab_size=cfg.vocab_size,
            do_lower_case=cfg.do_lower_case,
        )
        self.tokenizer = tokenizer

    def setup_training_data(self, train_data_config: Optional[DictConfig]):
        self._train_dl = self._setup_dataloader_from_config(cfg=train_data_config)

    def setup_validation_data(self, val_data_config: Optional[DictConfig]):
        self._validation_dl = self._setup_dataloader_from_config(cfg=val_data_config)

    def setup_test_data(self, test_data_config: Optional[DictConfig]):
        self._test_dl = self.__setup_dataloader_from_config(cfg=test_data_config)

    def _setup_dataloader_from_config(self, cfg: DictConfig):
        dataset = GLUEDataset(
            file_name=cfg.file_name,
            task_name=self.task_name,
            tokenizer=self.tokenizer,
            max_seq_length=self._cfg.dataset.max_seq_length,
            use_cache=self._cfg.dataset.use_cache,
        )

        return torch.utils.data.DataLoader(
            dataset=dataset,
            collate_fn=dataset.collate_fn,
            batch_size=cfg.batch_size,
            shuffle=cfg.shuffle,
            num_workers=self._cfg.dataset.num_workers,
            pin_memory=self._cfg.dataset.pin_memory,
            drop_last=self._cfg.dataset.drop_last,
        )

    @classmethod
    def list_available_models(cls) -> Optional[Dict[str, str]]:
        pass

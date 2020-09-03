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
from typing import List, Optional

import nemo
from nemo.collections.common.tokenizers.huggingface.auto_tokenizer import AutoTokenizer
from nemo.collections.nlp.modules.common.common_utils import get_pretrained_lm_models_list
from nemo.collections.nlp.modules.common.huggingface.huggingface_utils import (
    get_all_huggingface_pretrained_lm_models_list,
)
from nemo.collections.nlp.modules.common.megatron.megatron_utils import get_megatron_tokenizer

__all__ = ['get_tokenizer']


def get_tokenizer_list() -> List[str]:
    """
    Returns all all supported tokenizer names
    """
    s = set(get_pretrained_lm_models_list())
    s.update(set(get_all_huggingface_pretrained_lm_models_list()))
    return ["sentencepiece"] + list(s)


def get_tokenizer(
    tokenizer_name: str,
    data_file: Optional[str] = None,
    tokenizer_model: Optional[str] = None,
    sample_size: Optional[int] = None,
    special_tokens: Optional[List[str]] = None,
    vocab_file: Optional[str] = None,
    vocab_size: Optional[int] = None,
    do_lower_case: Optional[bool] = None,
):
    """
    Args:
        tokenizer_name: sentencepiece or pretrained model from the hugging face list,
            for example: bert-base-cased
            To see the list of pretrained models, use: nemo_nlp.modules.common.get_all_huggingface_pretrained_lm_models_list()
        data_file: data file used to build sentencepiece
        tokenizer_model: tokenizer model file of sentencepiece
        sample_size: sample size for building sentencepiece
        special_tokens: dict of special tokens
        vocab_file: path to vocab file
        vocab_size: vocab size for building sentence piece
        do_lower_case: (whether to apply lower cased) - only applicable when tokenizer is build with vocab file or with
             sentencepiece
    """
    full_huggingface_pretrained_model_list = get_all_huggingface_pretrained_lm_models_list()

    if tokenizer_name not in get_tokenizer_list():
        raise ValueError(
            f'Provided tokenizer_name: "{tokenizer_name}" is not supported, choose from {get_tokenizer_list()}'
        )

    if tokenizer_name.split('-') and tokenizer_name.split('-')[0] == "megatron":
        if do_lower_case is None:
            do_lower_case = (
                do_lower_case
                or nemo.collections.nlp.modules.common.megatron.megatron_utils.is_lower_cased_megatron(tokenizer_name)
            )
        if vocab_file is None:
            vocab_file = nemo.collections.nlp.modules.common.megatron.megatron_utils.get_megatron_vocab_file(
                tokenizer_name
            )
        tokenizer_name = get_megatron_tokenizer(tokenizer_name)

    if tokenizer_name in full_huggingface_pretrained_model_list:
        if special_tokens is None:
            special_tokens_dict = {}
        else:
            special_tokens_dict = special_tokens
        tokenizer = AutoTokenizer(
            pretrained_model_name=tokenizer_name,
            vocab_file=vocab_file,
            do_lower_case=do_lower_case,
            **special_tokens_dict,
        )
    elif tokenizer_name == 'sentencepiece':
        if not tokenizer_model and not data_file:
            raise ValueError(f'either tokenizer model or data_file must passed')
        if not tokenizer_model or not os.path.exists(tokenizer_model):
            num_special_tokens = 0
            if special_tokens:
                num_special_tokens = len(set(special_tokens.values()))
            tokenizer_model, _ = nemo.collections.common.tokenizers.sentencepiece_tokenizer.create_spt_model(
                data_file=data_file,
                vocab_size=vocab_size - num_special_tokens,
                special_tokens=None,
                sample_size=sample_size,
                do_lower_case=do_lower_case,
                output_dir=os.path.dirname(data_file) + '/spt',
            )
        tokenizer = nemo.collections.common.tokenizers.sentencepiece_tokenizer.SentencePieceTokenizer(
            model_path=tokenizer_model, special_tokens=special_tokens
        )
    else:
        raise ValueError(f'{tokenizer_name} is not supported')
    return tokenizer

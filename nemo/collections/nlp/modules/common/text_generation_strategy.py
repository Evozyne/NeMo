# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
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

import abc
import copy
import os
import re
import warnings
from typing import List, Set, Tuple

import torch

from nemo.collections.nlp.modules.common.lm_utils import pad_batch
from nemo.collections.nlp.modules.common.megatron.utils import get_ltor_masks_and_position_ids

try:
    from apex.transformer.pipeline_parallel.utils import get_num_microbatches

    HAVE_APEX = True

except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False

try:
    from megatron.core.pipeline_parallel.schedules import get_forward_backward_func

    HAVE_MEGATRON_CORE = True

except (ImportError, ModuleNotFoundError):

    HAVE_MEGATRON_CORE = False


# the text representation of eos_id, it applies for all tokenizers
END_OF_SEQ = '<|endoftext|>'


class TextGenerationStrategy:
    """
    Base class for TextGeneration Strategy
    """

    def __init__(self, model):
        self.model = model
        if self.model.training:
            # TODO in the future this should raise an exception
            warnings.warn(
                "Generation started while the model is in training mode, switching to eval mode "
                "(this situation may raise an exception in future versions, please call `eval()` before generation)"
            )
            self.model.eval()
        self._end_of_generation_cache = None

    def forward_step(self, batch, tensor_shape):
        fwd_bwd_function = get_forward_backward_func()
        output_tensor = fwd_bwd_function(
            forward_step_func=self.model.get_forward_output_only_func(),
            data_iterator=iter([batch,]),
            model=[self.forward_model],
            num_microbatches=get_num_microbatches(),
            forward_only=True,
            seq_length=tensor_shape[0],
            micro_batch_size=tensor_shape[1],
        )

        return output_tensor

    def tokenize_batch(self, sentences, max_len, add_BOS):
        """
        convert the sentences into lists of tokens, pad them to the same length, add bos tokens if it is needed
        Args:
            sentences (List[str]): list of input sentences in str format.
            max_len (int): max number of tokens to generate.
            add_BOS (bool): whether to add the BOS token at the beginning
        Returns:
            Tuple[torch.Tensor], the tokenized and padded torch tensor and the token context length tensor.
        """
        tokenizer = self.model.tokenizer
        if add_BOS:
            context_tokens = [[tokenizer.bos_id] + tokenizer.text_to_ids(s) for s in sentences]
        else:
            context_tokens = [tokenizer.text_to_ids(s) for s in sentences]
        context_tokens, context_lengths = pad_batch(context_tokens, tokenizer.eos_id, max_len)
        context_tokens_tensor = torch.cuda.LongTensor(context_tokens)
        context_length_tensor = torch.cuda.LongTensor(context_lengths)
        return context_tokens_tensor, context_length_tensor

    @abc.abstractclassmethod
    def clip_max_len(self, maxlen: int) -> int:
        """ clip the max len based on the LM model max sequence length
        Args:
            maxlen (int): the max len computed from the context and number of tokens to generate
        returns (int):
            the clip the max length based of the LM model max sequence length
        """
        pass

    @abc.abstractclassmethod
    def init_batch(self, context_tokens: torch.Tensor, context_length: int, compute_attention_mask: bool):
        """initialize the batch data before the inference steps.
           It will save the intermediate results as object attributes
           context_length (int): the context token length
           compute_attention_mask: bool: set to True to compute attention mask (not needed for FA)
        Args:
            context_tokens (torch.Tensor):  The padded context tokens including the space for tokens to be generated 
        """
        pass

    @abc.abstractclassmethod
    def prepare_batch_at_step(
        self, tokens: torch.Tensor, maxlen: int, micro_batch_size: int, step: int, context_length: int
    ) -> Tuple[List[torch.Tensor], List[int]]:
        """
        generate the batch used in inference for each of the steps
        Args:
            tokens  (torch.Tensor): the context tokens
            maxlen (int): the maximum length in the context tokens
            micro_batch_size (int): text generation batch size
            step (int): the inference step count
            context_length (int): the new token position in the tokens
        returns:
            a tuple of list of tensor arguments for the model and a list of tensor shape required by forward method
        """
        pass

    @abc.abstractclassmethod
    def post_process(self, tokens: torch.Tensor, new_tokens: torch.Tensor, context_length: int):
        """
        At the end of the single step inference, post process the inference results
        Args:
            tokens  (torch.Tensor): the context tokens
            new_token (torch.Tensor): sampled new token id
            context_length (int): the new token position in the tokens
        """
        pass

    def end_of_generation_condition(
        self, tokens: torch.Tensor, prev: torch.Tensor, eod_id: int, end_strings: List[str]
    ) -> torch.Tensor:
        """
        return whether the generation should stop based on the previous token
        Args:
            tokens (torch.Tensor): the generated tokens so far
            prev  (torch.Tensor): the previous token
            eod_id (int): the end of document token id
            end_strings (List[str]): the list of end of generation strings
        returns:
            a boolean tensor indicating whether the generation should stop
        """
        if (len(end_strings) == 1 and end_strings[0] == END_OF_SEQ) or not end_strings:
            # Simple scenario: only finish on end of document token.
            return prev == eod_id

        end_tokens, end_strings_to_check = self._get_end_of_generation_tokens_and_strings(eod_id, end_strings)
        assert end_tokens

        is_end = torch.isin(prev, torch.tensor(list(end_tokens), dtype=prev.dtype, device=prev.device))

        if end_strings_to_check:
            # The loop below is inefficient (see warning in `_get_end_of_generation_tokens_and_strings()`)
            # TODO In addition, we will not stop if the model generates an end string followed by extra characters,
            # e.g., if `end_string` is "Done" and there exists a "Done!" token it could generate tokens
            #       [..., ".", "Done!"]
            # which would fail the `endswith("Done")` check. However, stopping when "Done!" is generated would not
            # work either, since we would need to post-process the generated string to truncate the extra "!".
            # ==> this is left for future work if there is a compelling use case requiring this feature.
            for idx, token_seq in enumerate(tokens):
                text = self.model.tokenizer.ids_to_text(token_seq.tolist())
                is_end[idx] |= any(text.endswith(end_string) for end_string in end_strings_to_check)

        return is_end

    def post_generation_process(self, output):
        """
        At the end of the text generation, post process the results
        Args:
            output  (dict): the text generation output dictionary
        """
        return output

    def _get_end_of_generation_tokens_and_strings(
        self, eod_id: int, end_strings: List[str]
    ) -> Tuple[Set[int], List[str]]:
        """
        return the tokens and strings indicating the end of generation
        Args:
            eod_id (int): the end of document token id
            end_strings (List[str]): the list of end of generation strings
        Returns:
            a pair `(tokens, strings)` where `tokens` is a set of tokens (int) and `strings` is a list of strings,
            which must all be used to identify the end of generation (`tokens` always contains `eod_id`, while
            `strings` may be empty if all end strings are associated to unique tokens)
        """
        tokenizer = self.model.tokenizer
        # A cache is used to remember which end strings are associated to unique tokens vs. which ones
        # require an actual string comparison.
        if self._end_of_generation_cache is None or self._end_of_generation_cache["tokenizer"] is not tokenizer:
            # Invalidate the cache.
            self._end_of_generation_cache = {
                "tokenizer": tokenizer,
                "end_string_to_token": {END_OF_SEQ: eod_id},
                "end_strings_to_check": set(),
            }
        end_string_to_token = self._end_of_generation_cache["end_string_to_token"]

        end_tokens = {eod_id}  # always include `eod_id`, even if `END_OF_SEQ` is not within `end_strings`
        end_strings_to_check = []  # will contain end strings that have no associated special token

        for end_string in end_strings:
            try:
                end_tokens.add(end_string_to_token[end_string])
                continue
            except KeyError:
                if end_string in self._end_of_generation_cache["end_strings_to_check"]:
                    end_strings_to_check.append(end_string)
                    continue

            # `end_string` does not exist in the cache yet: check if `end_string` is a special token for
            # the tokenizer. Ideally, we would simply use `tokenizer.text_to_ids(end_string)`, but some
            # tokenizers (e.g., SentencePiece) may prefix the special token with another token associated
            # to an empty string. The code below is thus meant to extract the special token associated to
            # `end_string` (if it exists). Note that we use "<extra_id_1>" as prefix string to have a low
            # risk of the tokenizer merging it with `end_string`, but this is somewhat arbitrary.
            ids_ref = tokenizer.text_to_ids("<extra_id_1>")
            ids_with_end_string = tokenizer.text_to_ids(f"<extra_id_1>{end_string}")
            if len(ids_with_end_string) == len(ids_ref) + 1 and ids_with_end_string[:-1] == ids_ref:
                # We can assume that the extra token is the one corresponding to `end_string`.
                end_string_to_token[end_string] = ids_with_end_string[-1]
                end_tokens.add(ids_with_end_string[-1])
            else:
                # No special token.
                warnings.warn(
                    f"The end string '{end_string}' has no associated special token: this may slow down "
                    "generation (consider using a different tokenizer or modifying `end_strings`)"
                )
                self._end_of_generation_cache["end_strings_to_check"].add(end_string)
                end_strings_to_check.append(end_string)

        return end_tokens, end_strings_to_check


class GPTModelTextGenerationStrategy(TextGenerationStrategy):
    def __init__(self, model):
        super().__init__(model)
        self.forward_model = self.model.model

    def clip_max_len(self, maxlen: int) -> int:
        """ clip the max len based on the LM model max sequence length"""

        # for positional embedding types that allow length extrapolation, don't clip the max length
        if self.model.cfg.get("position_embedding_type", "learned_absolute") == "learned_absolute":
            if maxlen > self.model.cfg.encoder_seq_length + 1:
                maxlen = self.model.cfg.encoder_seq_length + 1
        return maxlen

    def init_batch(self, context_tokens: torch.Tensor, context_length: int, compute_attention_mask: bool):
        """initialize the batch data before the inference steps."""
        # Move to GPU.
        tokenizer = self.model.tokenizer
        tokens = context_tokens.contiguous().cuda()
        # Get the attention mask and postition ids.
        self.attention_mask, _, self.position_ids = get_ltor_masks_and_position_ids(
            tokens,
            tokenizer.eos_id,
            self.model.cfg.get('reset_position_ids', False),
            self.model.cfg.get('reset_attention_mask', False),
            self.model.cfg.get('eod_mask_loss', False),
            compute_attention_mask=compute_attention_mask,
        )

    def prepare_batch_at_step(
        self,
        tokens: torch.Tensor,
        maxlen: int,
        micro_batch_size: int,
        step: int,
        context_length: int,
        compute_attention_mask: bool = True,
    ) -> Tuple[List[torch.Tensor], List[int]]:
        """
        generate the batch used in inference for each of the steps
        """
        # types2use = None
        if step == 0:
            # Allocate memory for the entire context.
            set_inference_key_value_memory = True
            tokens2use = tokens[:, :context_length]
            positions2use = self.position_ids[:, :context_length]
            # not using type2use. uncomment it if it is used
            # if type_ids is not None:
            #     types2use = type_ids[:, :context_length]
        else:
            # Set this to false so the memory is not reallocated.
            set_inference_key_value_memory = False
            tokens2use = tokens[:, context_length - 1].view(micro_batch_size, -1)
            positions2use = self.position_ids[:, context_length - 1].view(micro_batch_size, -1)
            # not using type2use. uncomment it if it is used
            # if type_ids is not None:
            #     types2use = type_ids[:, context_length - 1].view(batch_size, -1)

        """Prepare batch for each of the inference steps"""
        attention_mask_repeat = None
        if compute_attention_mask:
            attention_mask_repeat = torch.concat([self.attention_mask for _ in range(micro_batch_size)])

        setkey_value_array = torch.tensor(
            [set_inference_key_value_memory] * micro_batch_size, device=torch.cuda.current_device()
        )
        len_array = torch.tensor([maxlen] * micro_batch_size, device=torch.cuda.current_device())

        batch = [tokens2use, attention_mask_repeat, positions2use, setkey_value_array, len_array]
        tensor_shape = [tokens2use.shape[1], micro_batch_size, self.model.cfg.hidden_size]
        return batch, tensor_shape


def neva_process_prompts(prompt, tokenizer, multimodal_cfg, num_media_latents, conv_template):
    from nemo.collections.multimodal.data.neva.neva_dataset import (
        DEFAULT_IMAGE_TOKEN,
        preprocess_llama_2,
        preprocess_multimodal,
        preprocess_nvgpt,
        preprocess_v1,
    )

    list_data_dict = []
    if multimodal_cfg["conv_template"] == "nvgpt":
        record = {
            'system': 'A chat between a curious user and an artificial intelligence assistant. The assistant gives helpful, detailed, and polite answers to the user\'s questions.\n\n',
            'conversations': [{'from': 'User', 'value': prompt}, {'from': 'Assistant', 'value': '',},],
        }

        for turn in record['conversations']:  #
            if turn.get('value') is not None:
                turn['value'] = re.sub('<image>', f'{DEFAULT_IMAGE_TOKEN}\n', turn['value'])
        list_data_dict.append(record)

        sources = preprocess_multimodal(
            copy.deepcopy(list_data_dict), multimodal_cfg, num_media_latents
        )  # HARDCODED FOR NOW
        data_dict = preprocess_nvgpt(sources, tokenizer, multimodal_cfg)

    elif multimodal_cfg["conv_template"] == "llama_2":
        record = {
            'conversations': [{'from': 'human', 'value': prompt,}, {'from': 'gpt', 'value': '',},],
        }

        for turn in record['conversations']:
            if turn.get('value') is not None:
                turn['value'] = re.sub('<image>', f'{DEFAULT_IMAGE_TOKEN}\n', turn['value'])
        list_data_dict.append(record)

        sources = preprocess_multimodal(
            copy.deepcopy(list_data_dict), multimodal_cfg, num_media_latents
        )  # HARDCODED FOR NOW
        data_dict = preprocess_llama_2(sources, tokenizer, multimodal_cfg)
    elif multimodal_cfg["conv_template"] == "v1":
        record = {
            'conversations': [{'from': 'human', 'value': prompt,}, {'from': 'gpt', 'value': '',},],
        }

        for turn in record['conversations']:
            if turn.get('value') is not None:
                turn['value'] = re.sub('<image>', f'{DEFAULT_IMAGE_TOKEN}\n', turn['value'])
        list_data_dict.append(record)

        sources = preprocess_multimodal(
            copy.deepcopy(list_data_dict), multimodal_cfg, num_media_latents
        )  # HARDCODED FOR NOW
        data_dict = preprocess_v1(sources, tokenizer, multimodal_cfg)
    else:
        raise ValueError(f"Conversation template `{conv_template}` is not supported in Neva now.")
    return data_dict['tokens'].tolist()


class NevaModelTextGenerationStrategy(TextGenerationStrategy):
    def __init__(self, model):
        super().__init__(model)
        self.forward_model = self.model.model
        self.num_media_latents = model.cfg.data.get("image_token_len", 576)
        self.tokenizer = self.model.tokenizer
        self.image_paths = []
        self.cfg = self.model.cfg
        self.data_cfg = self.model.cfg.data

        add_extra_token = 0
        self.multimodal_cfg = dict(
            is_multimodal=self.data_cfg.is_multimodal,
            sep_image_conv_front=self.data_cfg.sep_image_conv_front,
            conv_template=self.data_cfg.get("conv_template", "nvgpt"),
            image_token_len=self.data_cfg.image_token_len,
            image_folder=self.data_cfg.image_folder,
            image_aspect_ratio=self.data_cfg.image_aspect_ratio,
            use_im_start_end=getattr(self.cfg.mm_cfg, 'use_im_start_end', False),
            image_processor=None,
            add_extra_token=add_extra_token,
            context_length=self.cfg.encoder_seq_length,
        )

    def clip_max_len(self, maxlen: int) -> int:
        """ clip the max len based on the LM model max sequence length"""
        if maxlen > self.model.cfg.encoder_seq_length + 1:
            maxlen = self.model.cfg.encoder_seq_length + 1
        return maxlen

    def init_batch(self, context_tokens: torch.Tensor, context_length: int, compute_attention_mask: bool):
        """initialize the batch data before the inference steps."""
        # Move to GPU.
        tokenizer = self.model.tokenizer
        tokens = context_tokens.contiguous().cuda()
        # Get the attention mask and postition ids.
        self.attention_mask, _, self.position_ids = get_ltor_masks_and_position_ids(
            tokens,
            eod_token=tokenizer.eos_id,
            eod_mask_loss=False,
            reset_attention_mask=False,
            reset_position_ids=False,
            compute_attention_mask=compute_attention_mask,
        )

    def tokenize_batch(self, prompt, max_len, add_BOS):

        if type(prompt) == str:
            context_tokens = neva_process_prompts(
                prompt,
                self.tokenizer,
                self.multimodal_cfg,
                self.num_media_latents,
                self.multimodal_cfg['conv_template'],
            )
        elif type(prompt) == list:
            context_tokens = []
            for p in prompt:
                context_tokens.append(
                    neva_process_prompts(
                        p,
                        self.tokenizer,
                        self.multimodal_cfg,
                        self.num_media_latents,
                        self.multimodal_cfg['conv_template'],
                    )[0]
                )
        else:
            raise ValueError(f'{type(prompt)} is not supported for tokenization')

        context_tokens, context_lengths = pad_batch(context_tokens, self.tokenizer.eos_id, max_len)
        context_tokens_tensor = torch.cuda.LongTensor(context_tokens)
        context_length_tensor = torch.cuda.LongTensor(context_lengths)
        return context_tokens_tensor, context_length_tensor

    def prepare_batch_at_step(
        self,
        tokens: torch.Tensor,
        maxlen: int,
        micro_batch_size: int,
        step: int,
        context_length: int,
        compute_attention_mask: bool = True,
        media=None,
    ) -> Tuple[List[torch.Tensor], List[int]]:
        """
        generate the batch used in inference for each of the steps
        """
        # types2use = None
        if step == 0:
            # Allocate memory for the entire context.
            set_inference_key_value_memory = True
            tokens2use = tokens[:, :context_length]
            positions2use = self.position_ids[:, :context_length]
            # not using type2use. uncomment it if it is used
            # if type_ids is not None:
            #     types2use = type_ids[:, :context_length]
        else:
            # Set this to false so the memory is not reallocated.
            set_inference_key_value_memory = False
            tokens2use = tokens[:, context_length - 1].view(micro_batch_size, -1)
            positions2use = self.position_ids[:, context_length - 1].view(micro_batch_size, -1)
            # not using type2use. uncomment it if it is used
            # if type_ids is not None:
            #     types2use = type_ids[:, context_length - 1].view(batch_size, -1)

        """Prepare batch for each of the inference steps"""
        attention_mask_repeat = None
        if compute_attention_mask:
            attention_mask_repeat = torch.concat([self.attention_mask for _ in range(micro_batch_size)])

        setkey_value_array = torch.tensor(
            [set_inference_key_value_memory] * micro_batch_size, device=torch.cuda.current_device()
        )
        len_array = torch.tensor([maxlen] * micro_batch_size, device=torch.cuda.current_device())
        batch = [tokens2use, attention_mask_repeat, positions2use, media, setkey_value_array, len_array]
        tensor_shape = [tokens2use.shape[1], micro_batch_size, self.model.cfg.hidden_size]
        return batch, tensor_shape


class PromptLearningModelTextGenerationStrategy(TextGenerationStrategy):
    def __init__(self, model, task_ids):
        super().__init__(model)
        self.task_ids = task_ids
        self.forward_model = self.model

    def init_batch(self, context_tokens: torch.Tensor, context_length: int, compute_attention_mask: bool):
        """initialize the batch data before the inference steps."""
        # Move to GPU.
        tokenizer = self.model.tokenizer
        tokens = context_tokens.contiguous().cuda()
        # Get the attention mask and postition ids.
        self.attention_mask, _, self.position_ids = get_ltor_masks_and_position_ids(
            tokens,
            tokenizer.eos_id,
            self.model.cfg.get('reset_position_ids', False),
            self.model.cfg.get('reset_attention_mask', False),
            self.model.cfg.get('eod_mask_loss', False),
            compute_attention_mask=compute_attention_mask,
        )

    def clip_max_len(self, maxlen: int) -> int:
        """ clip the max len based on the LM model max sequence length"""
        if maxlen > self.model.frozen_model.cfg.encoder_seq_length + 1:
            maxlen = self.model.frozen_model.cfg.encoder_seq_length + 1
        return maxlen

    def prepare_batch_at_step(
        self,
        tokens: torch.Tensor,
        maxlen: int,
        micro_batch_size: int,
        step: int,
        context_length: int,
        compute_attention_mask: bool,
    ) -> Tuple[List[torch.Tensor], List[int]]:
        # types2use = None
        if step == 0:
            # Allocate memory for the entire context.
            set_inference_key_value_memory = True
            tokens2use = tokens[:, :context_length]
            positions2use = self.position_ids[:, :context_length]
            # not using type2use. uncomment it if it is used
            # if type_ids is not None:
            #     types2use = type_ids[:, :context_length]
        else:
            # Set this to false so the memory is not reallocated.
            set_inference_key_value_memory = False
            tokens2use = tokens[:, context_length - 1].view(micro_batch_size, -1)
            positions2use = self.position_ids[:, context_length - 1].view(micro_batch_size, -1)
            # not using type2use. uncomment it if it is used
            # if type_ids is not None:
            #     types2use = type_ids[:, context_length - 1].view(batch_size, -1)

        """Prepare batch for each of the inference steps"""
        attention_mask_repeat = None
        if compute_attention_mask:
            attention_mask_repeat = torch.concat([self.attention_mask for _ in range(micro_batch_size)])
        setkey_value_array = torch.tensor(
            [set_inference_key_value_memory] * micro_batch_size, device=torch.cuda.current_device()
        )
        len_array = torch.tensor([maxlen] * micro_batch_size, device=torch.cuda.current_device())

        batch = [tokens2use, attention_mask_repeat, positions2use, self.task_ids, setkey_value_array, len_array]
        tensor_shape = [tokens2use.shape[1], micro_batch_size, self.model.frozen_model.cfg.hidden_size]
        return batch, tensor_shape

    def post_process(self, tokens: torch.Tensor, new_tokens: torch.Tensor, context_length: int):
        """
        At the end of the inference, post process the inference results
        """
        # Replace special soft prompt token ids with unk token ids
        if (
            self.model.pseudo_token_ids_start is not None
        ):  # TODO: (@adithyare) prompt learning logic can be greatly simplified by removing data preparation logic from model logic.
            tokenizer = self.model.tokenizer
            pseudo_token_ids_start = self.model.pseudo_token_ids_start
            new_tokens[(new_tokens >= pseudo_token_ids_start)] = tokenizer.unk_id
            tokens[:, :context_length][(tokens[:, :context_length] >= pseudo_token_ids_start)] = tokenizer.unk_id


class RetroModelTextGenerationStrategy(TextGenerationStrategy):
    def __init__(self, model):
        super().__init__(model)
        self.forward_model = self.model.model

        # retro args
        # self.retro_num_neighbors
        # self.retro_gpt_retrieved_length =     

    def clip_max_len(self, maxlen: int) -> int:
        """ clip the max len based on the LM model max sequence length"""

        # for positional embedding types that allow length extrapolation, don't clip the max length
        if self.model.cfg.get("position_embedding_type", "learned_absolute") == "learned_absolute":
            if maxlen > self.model.cfg.encoder_seq_length + 1:
                maxlen = self.model.cfg.encoder_seq_length + 1
        return maxlen

    def tokenize_batch(self, sentences, max_len, add_BOS):
        """
        convert the sentences into lists of tokens, pad them to the same length, add bos tokens if it is needed
        Args:
            sentences (List[str]): list of input sentences in str format.
            max_len (int): max number of tokens to generate.
            add_BOS (bool): whether to add the BOS token at the beginning
        Returns:
            Tuple[torch.Tensor], the tokenized and padded torch tensor and the token context length tensor.
        """
        tokenizer = self.model.tokenizer
        if add_BOS:
            context_tokens = [[tokenizer.bos_id] + tokenizer.text_to_ids(s) for s in sentences]
        else:
            context_tokens = [tokenizer.text_to_ids(s) for s in sentences]
        context_tokens, context_lengths = pad_batch(context_tokens, tokenizer.eos_id, max_len)
        context_tokens_tensor = torch.cuda.LongTensor(context_tokens)
        context_length_tensor = torch.cuda.LongTensor(context_lengths)
        return context_tokens_tensor, context_length_tensor

    def tokenize_neighbors_batch(self, neighbors, retro_args):
        tokenizer = self.model.tokenizer
        r = retro_args['retro_gpt_retrieved_length']
        retro_num_neighbors = retro_args['retro_num_neighbors']
        ft_neighbours = retro_args['ft_neighbours']
        reuse_top = retro_args['reuse_top']

        # tokenize neighbors
        neighbors_tokens = []
        for neighbor in neighbors:
            neighbors_tokens.append(tokenizer.tokenize(neighbor))

        # take top k neighbours 
        if reuse_top:
            valid_neighbours_tokens = neighbors_tokens[:retro_num_neighbors]
        else:
            valid_neighbours_tokens = neighbors_tokens[ft_neighbours:retro_num_neighbors + ft_neighbours]

        # pad neighbors
        padded_valid_neighbours_tokens = []
        for neighbour_tokens in valid_neighbours_tokens:
            if len(neighbour_tokens) >= r:
                padded_neighbour_tokens = neighbour_tokens[:r]
            else:
                padded_neighbour_tokens = neighbour_tokens + [tokenizer.eos_id] * (r - len(neighbour_tokens))
            padded_valid_neighbours_tokens.append(padded_neighbour_tokens)            

        # check if have enough neighbors
        if len(padded_valid_neighbours_tokens) < retro_num_neighbors:
            assert ValueError("neighbours are not enough, add empty ones and create mask for those empty ones")

        # cast to torch tensor
        padded_valid_neighbours_tokens = torch.cuda.LongTensor(padded_valid_neighbours_tokens)

        return padded_valid_neighbours_tokens

    def forward_step(self, batch, tensor_shape):
        fwd_bwd_function = get_forward_backward_func()
        output_tensor = fwd_bwd_function(
            forward_step_func=self.model.get_forward_output_only_func(),
            data_iterator=iter([batch,]),
            model=[self.forward_model],
            num_microbatches=get_num_microbatches(),
            forward_only=True,
            seq_length=tensor_shape[0],
            micro_batch_size=tensor_shape[1],
        )

        return output_tensor

    def init_batch(self, 
                   context_tokens: torch.Tensor, 
                   context_length: int, 
                   compute_attention_mask: bool,
                   **extra):
        """initialize the batch data before the inference steps."""
        # Move to GPU.
        tokenizer = self.model.tokenizer
        tokens = context_tokens.contiguous().cuda()
        extra['neighbors_tokens'] = extra['neighbors_tokens'].contiguous().cuda()

        # Get the attention mask and postition ids.
        self.attention_mask, _, self.position_ids = get_ltor_masks_and_position_ids(
            tokens,
            tokenizer.eos_id,
            self.model.cfg.get('reset_position_ids', False),
            self.model.cfg.get('reset_attention_mask', False),
            self.model.cfg.get('eod_mask_loss', False),
            compute_attention_mask=compute_attention_mask,
        )

        # Get the attention mask and postition ids for neighbors (retro_generation.retro_generate_tokens_probs_and_return_on_first_stage)
        _, _, self.neighbor_position_ids = get_ltor_masks_and_position_ids(
            extra['neighbors_tokens'],
            tokenizer.eod,
            self.model.cfg.get('reset_position_ids', False),
            self.model.cfg.get('reset_attention_mask', False),
            self.model.cfg.get('eod_mask_loss', False),
        )
        self.neighbor_attention_mask = None

    def prepare_batch_at_step(
        self,
        tokens: torch.Tensor,
        maxlen: int,
        micro_batch_size: int,
        step: int,
        context_length: int,
        compute_attention_mask: bool = True,
        **extra,
    ) -> Tuple[List[torch.Tensor], List[int]]:
        """
        generate the batch used in inference for each of the steps
        """
        # types2use = None
        if step == 0:
            # Allocate memory for the entire context.
            set_inference_key_value_memory = True
            tokens2use = tokens[:, :context_length]
            positions2use = self.position_ids[:, :context_length]
            # not using type2use. uncomment it if it is used
            # if type_ids is not None:
            #     types2use = type_ids[:, :context_length]
        else:
            # Set this to false so the memory is not reallocated.
            set_inference_key_value_memory = False
            tokens2use = tokens[:, context_length - 1].view(micro_batch_size, -1)
            positions2use = self.position_ids[:, context_length - 1].view(micro_batch_size, -1)
            # not using type2use. uncomment it if it is used
            # if type_ids is not None:
            #     types2use = type_ids[:, context_length - 1].view(batch_size, -1)

        """Prepare batch for each of the inference steps"""
        attention_mask_repeat = None
        if compute_attention_mask:
            attention_mask_repeat = torch.concat([self.attention_mask for _ in range(micro_batch_size)])

        setkey_value_array = torch.tensor(
            [set_inference_key_value_memory] * micro_batch_size, device=torch.cuda.current_device()
        )
        len_array = torch.tensor([maxlen] * micro_batch_size, device=torch.cuda.current_device())

        batch = [tokens2use, attention_mask_repeat, positions2use, extra['neighbors_tokens'], self.neighbor_attention_mask, self.neighbor_position_ids, setkey_value_array, len_array]
        tensor_shape = [tokens2use.shape[1], micro_batch_size, self.model.cfg.hidden_size]
        return batch, tensor_shape

    # def _pad_neighbours_for_query_only(args, nb_tokens, pad_id, ft_neighbours):
    #     # take top k neighbours and padding
    #     neighbours_tokens = []
    #     retro_args = get_retro_args()
    #     r = retro_args.retro_gpt_retrieved_length

    #     if args.reuse_top:
    #         valid_nb_tokens = nb_tokens[:args.retro_num_neighbors]
    #     else:
    #         valid_nb_tokens = nb_tokens[ft_neighbours:args.retro_num_neighbors + ft_neighbours]

    #     for nb_token in valid_nb_tokens:
    #         if len(nb_token) >= r:
    #             nb_token = nb_token[:r]
    #         else:
    #             nb_token = nb_token + [pad_id] * (r - len(nb_token))
    #         neighbours_tokens.append(nb_token)
    #     print("len(nb_tokens)", len(nb_tokens))
    #     print("len(neighbours_tokens)", len(neighbours_tokens))
    #     print("args.retro_num_neighbors", args.retro_num_neighbors)

    #     if len(neighbours_tokens) < args.retro_num_neighbors:
    #         assert ValueError("neighbours are not enough, add empty ones and create mask for those empty ones")
    #     neighbours_tokens = np.array(neighbours_tokens)
    #     return neighbours_tokens

    # def _tokenize_prompts(prompts=None, tokens_to_generate=None,
    #                     add_BOS=None, rank=0):
    #     """Tokenize prompts and make them avaiable on all ranks."""

    #     # On all ranks set to None so we can pass them to functions
    #     sizes_list = None
    #     prompts_tokens_cuda_long_tensor = None
    #     prompts_length_cuda_long_tensor = None

    #     # On the specified rank, build the above.
    #     if torch.distributed.get_rank() == rank:
    #         assert prompts is not None
    #         assert tokens_to_generate is not None
    #         # Tensor of tokens padded and their unpadded length.
    #         prompts_tokens_cuda_long_tensor, prompts_length_cuda_long_tensor = \
    #             _tokenize_prompts_and_batch(prompts, tokens_to_generate, add_BOS)
    #         # We need the sizes of these tensors for the boradcast
    #         sizes_list = [prompts_tokens_cuda_long_tensor.size(0), # Batch size
    #                     prompts_tokens_cuda_long_tensor.size(1)] # Sequence lenght

    #     # First, broadcast the sizes.
    #     sizes_tensor = broadcast_int_list(2, int_list=sizes_list, rank=rank)

    #     # Now that we have the sizes, we can boradcast the tokens
    #     # and length tensors.
    #     sizes = sizes_tensor.tolist()
    #     prompts_tokens_cuda_long_tensor = broadcast_tensor(
    #         sizes, torch.int64, tensor=prompts_tokens_cuda_long_tensor, rank=rank)
    #     prompts_length_cuda_long_tensor = broadcast_tensor(
    #         sizes[0], torch.int64, tensor=prompts_length_cuda_long_tensor,
    #         rank=rank)

    #     return prompts_tokens_cuda_long_tensor, prompts_length_cuda_long_tensor

    # def _tokenize_prompts_and_batch(prompts, tokens_to_generate, add_BOS):
    #     """Given a set of prompts and number of tokens to generate:
    #         - tokenize prompts
    #         - set the sequence length to be the max of length of prompts
    #         plus the number of tokens we would like to generate
    #         - pad all the sequences to this length so we can convert them
    #         into a 2D tensor.
    #     """

    #     # Tokenize all the prompts.
    #     tokenizer = get_tokenizer()
    #     if add_BOS:
    #         prompts_tokens = [[tokenizer.eod] + tokenizer.tokenize(prompt)
    #                         for prompt in prompts]
    #     else:
    #         prompts_tokens = [tokenizer.tokenize(prompt) for prompt in prompts]

    #     # Now we have a list of list of tokens which each list has a different
    #     # size. We want to extend this list to:
    #     #   - incorporate the tokens that need to be generated
    #     #   - make all the sequences equal length.
    #     # Get the prompts length.
    #     prompts_length = [len(prompt_tokens) for prompt_tokens in prompts_tokens]
    #     # Get the max prompts length.
    #     max_prompt_len = max(prompts_length)
    #     # Set the tokens to generate to the max prompts length for Retro
    #     args = get_args()
    #     if args.retro_add_retriever:
    #         tokens_to_generate = max_prompt_len
    #     # Number of tokens in the each sample of the batch.
    #     samples_length = max_prompt_len + tokens_to_generate
    #     # Now update the list of list to be of the same size: samples_length.
    #     for prompt_tokens, prompt_length in zip(prompts_tokens, prompts_length):
    #         padding_size = samples_length - prompt_length
    #         prompt_tokens.extend([tokenizer.eod] * padding_size)

    #     # Now we are in a structured format, we can convert to tensors.
    #     prompts_tokens_tensor = torch.cuda.LongTensor(prompts_tokens)
    #     prompts_length_tensor = torch.cuda.LongTensor(prompts_length)

    #     return prompts_tokens_tensor, prompts_length_tensor

    # def _reformat_prompt_short(query, neighbours, dataset_name, ft_neighbours, \
    #                         max_output_len, tokenizer, max_seq_length):
    #     if not query.endswith("?"):
    #         query = query + "?"
    #     query = "Question: {} Answer: The answer is".format(query)

    #     if ft_neighbours > 0:
    #         context = "\n\n".join(neighbours[0:ft_neighbours]) + "\n\n"
    #         context_tokens = tokenizer.tokenize(context)
    #         dialogue_tokens = tokenizer.tokenize(query)
    #         context_tokens = context_tokens[:max_seq_length - max_output_len - len(dialogue_tokens)]
    #         context = tokenizer.detokenize(context_tokens)
    #         all_input = context + query
    #         input_tokens = tokenizer.tokenize(all_input)
    #     else:
    #         all_input = query
    #         input_tokens = tokenizer.tokenize(all_input)

    #     return input_tokens


def model_inference_strategy_dispatcher(model, **args):
    from nemo.collections.multimodal.models.multimodal_llm.neva.neva_model import MegatronNevaModel
    from nemo.collections.nlp.models.language_modeling.megatron_gpt_model import MegatronGPTModel
    from nemo.collections.nlp.models.language_modeling.megatron_gpt_prompt_learning_model import (
        MegatronGPTPromptLearningModel,
    )
    from nemo.collections.nlp.models.language_modeling.megatron_retrieval_model import MegatronRetrievalModel
    from nemo.collections.nlp.modules.common.retro_inference_strategies_legacy import (
        RetroFileQAModelTextGenerationStrategy,
        RetroModelTextGenerationStrategy,
        RetroQAModelTextGenerationStrategy,
    )

    if isinstance(model, MegatronNevaModel):
        return NevaModelTextGenerationStrategy(model)
    if isinstance(model, MegatronGPTPromptLearningModel):
        return PromptLearningModelTextGenerationStrategy(model, **args)
    elif isinstance(model, MegatronGPTModel):
        return GPTModelTextGenerationStrategy(model)
    elif isinstance(model, MegatronRetrievalModel):
        strategy_name = args['strategy']
        del args['strategy']
        megatron_lm_compatible = model.model.megatron_lm_compatible
        args['megatron_lm_compatible'] = megatron_lm_compatible
        if strategy_name == 'RetroModelTextGenerationStrategy':
            return RetroModelTextGenerationStrategy(model, **args)
        elif strategy_name == 'RetroQAModelTextGenerationStrategy':
            return RetroQAModelTextGenerationStrategy(model, **args)
        elif strategy_name == 'RetroFileQAModelTextGenerationStrategy':
            return RetroFileQAModelTextGenerationStrategy(model, **args)
        else:
            raise ValueError(f'{strategy_name} is not supported for inference')
    else:
        raise ValueError(f'{model} is not supported for inference')

    # Should call GPTModel or Megatron Retrieval Model's forward method

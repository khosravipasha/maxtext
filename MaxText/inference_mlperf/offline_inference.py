# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Callable, List
import dataclasses
from collections import defaultdict
import jax
from jax import numpy as jnp
import numpy as np
import queue
import os
import functools
import threading
import traceback
import signal
import random

from jetstream.engine import engine_api

import logging
# pylint: disable=no-name-in-module
from MaxText.maxengine import set_engine_vars_from_base_engine

log = logging.getLogger(__name__)


@dataclasses.dataclass
class InputData:
  id: str
  tokens: jax.Array
  true_length: int


class JetThread(threading.Thread):

  def run(self):
    try:
      super().run()
    except Exception as e:  # pylint: disable=broad-exception-caught
      print(f"Thread {self.name} encountered an error: {e}")
      traceback.print_exc()
      os.kill(os.getpid(), signal.SIGKILL)


class OfflineInference:

  def __init__(self, engine: engine_api.Engine, params, base_engine: engine_api.Engine, enable_batch_prefill: bool):
    self.live = False
    self.engine = engine
    self.decode_state = None
    self.decode_state_executable = None
    if params is None:
      self.relayout_params = True
      params = engine.load_params()
    else:
      self.relayout_params = False
      rng = jax.random.PRNGKey(0)
      set_engine_vars_from_base_engine(engine, base_engine, rng)
    self.params = params

    self.enable_batch_prefill = enable_batch_prefill
    self.batch_size = engine.max_concurrent_decodes
    self.max_prefill_length = engine.config.max_prefill_predict_length
    self.max_decode_length = engine.config.max_target_length - engine.config.max_prefill_predict_length
    metadata = engine.get_tokenizer()
    self.tokenizer = engine.build_tokenizer(metadata)
    self.dummy = False

    self._cached_pref = {}
    self._cached_pref_batch = {}
    self._cached_generate = None
    self.detokenize_backlog = queue.Queue(10)
    self.prefill_buckets = defaultdict(list)

    self._decode_state_executable = None

  def init_decode_state(self):
    if self.decode_state is None:
      assert self._decode_state_executable is not None, "Decode state executable is none"
      self.decode_state = self._decode_state_executable(None)

  def warmup(self, max_length, warmup_samples):

    self._cached_generate, self.params, self._decode_state_executable = self.engine.aot_compile(
        self.params, pass_rng_shape=False
    )

    self.init_decode_state()

    interesting_buckets = [
        64,
        128,
        256,
        512,
        1024,
        2048,
        4096,
    ]
    i32_scalar = jax.ShapeDtypeStruct((), int)

    for length in interesting_buckets:
      if length > max_length:
        break
      log.info("Compiling prefill: %d", length)
      input_data = jax.ShapeDtypeStruct((length,), jnp.dtype("int32"))

      insert_with_layout = jax.jit(
          self._prefill_insert,
          in_shardings=(self.engine.param_layouts, None, None, None, self.engine.decode_state_layouts),
          out_shardings=(
              None,
              self.engine.decode_state_layouts,
          ),
          donate_argnames=("decode_state"),
      )
      lowered_insert = insert_with_layout.lower(
          self.params, input_data, i32_scalar, i32_scalar, self.engine.decode_state_shapes
      )
      self._cached_pref[length] = lowered_insert.compile(compiler_options=None)

      if length in (64, 1024):
        continue

      input_data_batch = jax.ShapeDtypeStruct((max_length,), jnp.dtype("int32"))
      min_num_prompts = max_length // length
      max_num_prompts = max_length // (length // 2)
      possible_prompts = range(min_num_prompts, max_num_prompts)
      for num_prompts in possible_prompts:
        log.info("Compiling batched prefill: %d num_prompts: %d", length, num_prompts)
        self._cached_pref_batch[(length, num_prompts)] = (
            jax.jit(
                self._prefill_insert_batch,
                in_shardings=(
                    self.engine.param_layouts,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    self.engine.decode_state_layouts,
                ),
                out_shardings=(
                    None,
                    self.engine.decode_state_layouts,
                ),
                static_argnames=(
                    "num_prompts",
                    "padded_length",
                ),
                donate_argnames=("decode_state",),
            )
            .lower(
                self.params,
                input_data_batch,
                jnp.arange(0, 16, dtype=int),
                num_prompts,
                jnp.arange(0, max_length, dtype=int),
                jnp.ones(max_length, dtype=int),
                jnp.arange(0, max_length, 64, dtype=int),
                length,
                jnp.full(16, length, dtype=int),
                self.engine.decode_state_shapes,
            )
            .compile(compiler_options=None)
        )

    self.batch_inference(warmup_samples, desc="warmup")

  def _prefill_insert(self, params, tokens, slot, true_length, decode_state):
    """return decodestate."""
    padded_len = tokens.shape[0]
    prefill_result, first_token = self.engine.prefill(params=params, padded_tokens=tokens, true_length=true_length)
    decode_state = self.engine.insert(prefill_result, decode_state, slot)
    return first_token, decode_state

  def _prefill_insert_batch(
      self,
      params,
      tokens,
      slots,
      num_prompts,
      decoder_positions,
      decoder_segment_ids,
      start_pos,
      padded_length,
      true_lengths,
      decode_state,
  ):
    """return decodestate."""
    cache, prefill_results, first_tokens = self.engine.prefill_concat(
        params=params,
        padded_tokens=tokens,
        decoder_positions=decoder_positions,
        decoder_segment_ids=decoder_segment_ids,
        start_pos=start_pos,
        true_lengths=true_lengths,
        num_prompts=num_prompts,
    )
    decode_state = self.engine.insert_partial(
        prefill_results,
        decode_state,
        cache,
        slots,
        num_prompts=num_prompts,
        start_indices=start_pos,
        seq_len=padded_length,
    )
    return first_tokens, decode_state

  def batch_inference_with_callback(
      self,
      data: List[InputData],
      emit_first_token: Callable[[str, int], bool],
      emit_token: Callable[[str, int], bool],
      desc: str,
  ):
    """callback is a function that takes id and token. It will be called once per output

    token.
    """

    def prefill(prefill_bucket, prefill_len):
      nonlocal self
      if self.dummy:
        log.info("dummy prefill")
        return 123
      if (
          not self.enable_batch_prefill
          or prefill_len == self.max_prefill_length
          or prefill_len * len(prefill_bucket) < self.max_prefill_length
      ):
        prefill_result = []
        prefill_fn = self._prefill_insert
        if (cached := self._cached_pref.get(prefill_len)) is not None:
          prefill_fn = cached
        else:
          assert False, "prefill fn not found"

        for slot, row in prefill_bucket:
          first_token, self.decode_state = prefill_fn(self.params, row.tokens, slot, row.true_length, self.decode_state)
          prefill_result.append((first_token, slot, row))
        return prefill_result
      else:
        num_prompts = len(prefill_bucket)
        sequence_indicators = []
        total_len = 0
        for idx, (slot, row) in enumerate(prefill_bucket):
          sequence_indicators.append(np.full(row.true_length, idx * 2 + 1, dtype=int))
          total_len += row.true_length
        sequence_indicators.append(np.zeros(self.max_prefill_length - total_len, dtype=int))
        sequence_indicator = jnp.array(np.concatenate(sequence_indicators))

        positions = [np.arange(0, row.true_length) for (slot, row) in prefill_bucket]
        positions.append(np.arange(0, self.max_prefill_length - total_len))
        positions = jnp.array(np.concatenate(positions))

        tokens = [row.tokens[: row.true_length] for (slot, row) in prefill_bucket]
        tokens.append(jnp.zeros(self.max_prefill_length - total_len, dtype=int))
        tokens = jnp.concat(tokens)
        slots = [slot for (slot, row) in prefill_bucket]
        true_lengths = [row.true_length for (slot, row) in prefill_bucket]
        start_pos = np.cumsum([0] + [row.true_length for (slot, row) in prefill_bucket])[:-1]
        start_pos = start_pos.tolist()

        # pad slots to keep static shape of jitted function input
        def pad_num_prompts_len_array(array_to_pad, pad_len):
          if len(array_to_pad) < pad_len:
            array_to_pad.extend([0] * (pad_len - len(array_to_pad)))
          return jnp.array(array_to_pad)

        slots = pad_num_prompts_len_array(slots, 16)
        true_lengths = pad_num_prompts_len_array(true_lengths, 16)
        start_pos = pad_num_prompts_len_array(start_pos, 16)

        prefill_fn = self._prefill_insert_batch
        log.info("invoking compiled function with length %d num_prompts %d", prefill_len, num_prompts)
        if (cached := self._cached_pref_batch.get((prefill_len, num_prompts))) is not None:
          prefill_fn = cached
        else:
          assert False, "prefill batch not found"

        first_tokens, self.decode_state = prefill_fn(
            self.params,
            tokens,
            slots,
            positions,
            sequence_indicator,
            start_pos,
            true_lengths,
            self.decode_state,
        )  # pytype: disable=missing-parameter
        prefill_result = [(first_tokens[idx], slot, row) for (idx, (slot, row)) in enumerate(prefill_bucket)]

        return prefill_result

    def prefill_batch(prefill_bucket, padded_len):
      nonlocal self
      prefill_results = prefill(prefill_bucket, padded_len)
      for _first_token, _slot, _row in prefill_results:
        log.info(
            "Put row of len %d true length %d slot %s to detokenize backlog", _row.tokens.shape[0], _row.true_length, _slot
        )
        self.detokenize_backlog.put((_first_token, True, _row.id, _slot), block=True)

    empty_slots = list(range(self.batch_size))
    slot_to_id = {}
    num_prefills = {}
    num_decodes = 0

    dummy_length = 1

    def decode():
      nonlocal self
      nonlocal dummy_length
      if self.dummy:
        log.info("Dummy generate")
        res = engine_api.ResultTokens(
            data=np.array([[123, 1, dummy_length]] * self.batch_size),
            tokens_idx=(0, 0),
            valid_idx=(0, 0),
            length_idx=(0, 0),
            samples_per_slot=(0, 0),
        )
        dummy_length += 1
        self.decode_state, result_tokens = self.decode_state, res
      else:
        gen_fn = self.engine.generate
        if self._cached_generate is not None:
          gen_fn = self._cached_generate
        else:
          assert False, "no generate fn"
        result_tokens_l = []
        for i in range(10):
          self.decode_state, result_tokens = gen_fn(self.params, self.decode_state, None)
          result_tokens_l.append(result_tokens)
      for i in range(10):
        # result_tokens.copy_to_host_async()
        result_tokens = result_tokens_l[i].convert_to_numpy()
        self.detokenize_backlog.put((result_tokens, False, 0, 0), block=True)
        # log.info("Decode put result %d to queue", i)

    def detokenize():
      nonlocal self
      nonlocal slot_to_id
      nonlocal empty_slots
      while self.live:
        # log.info("Detokenize start")
        newly_empty = []
        result_tokens, is_first_token, row_id, _slot = self.detokenize_backlog.get(block=True)
        # result_tokens = result_tokens.convert_to_numpy()
        # log.info("Detokenize get from queue")
        if is_first_token:
          first_token = result_tokens.data[0][0].item()
          should_terminate = emit_first_token(row_id, first_token)
          if not should_terminate:
            slot_to_id[_slot] = row_id
          else:
            empty_slots.append(_slot)
          continue
        for slot, id_ in slot_to_id.items():
          token, is_valid, length = result_tokens.data[slot]
          log.debug("slot is %s, length is %d", slot, length)
          should_finish = False
          if is_valid:
            should_finish = emit_token(id_, token.item())
          if should_finish or length >= self.max_decode_length:
            newly_empty.append(slot)
            log.debug("Detokenize free up %s, length %d", slot, length)
        # Add slots of those that are empty to empty
        for slot in newly_empty:
          del slot_to_id[slot]
          empty_slots.append(slot)
        if newly_empty and self.detokenize_backlog.qsize() == 0 and len(slot_to_id.items()) == 0:
          break

    detokenize_thread = JetThread(
        target=functools.partial(
            detokenize,
        ),
        name="detokenize",
    )
    self.live = True
    detokenize_thread.start()
    total_num_prefills = 0
    for row in data:
      while not empty_slots:
        # If slots are all full, decode until there are free slots
        # to insert
        num_decodes += 1
        log.info("decode-%s-%d", desc, num_decodes)
        decode()
      # do one insert
      padded_len = len(row.tokens)
      num_prefills[padded_len] = 1 if padded_len not in num_prefills else num_prefills[padded_len] + 1
      log.debug(
          "prefill-%s-%d num_prefills %d padded_len %d true_length %d num_empty_slots %d num_decodes %d",
          desc,
          num_prefills,
          sum(num_prefills.values()),
          padded_len,
          row.true_length,
          len(empty_slots),
          num_decodes,
      )
      total_num_prefills += 1
      log.info("Total num prefill: %d", total_num_prefills)
      slot = empty_slots.pop()
      # directly prefill prompts
      if not self.enable_batch_prefill:
        first_token, slot, row = prefill([(slot, row)], padded_len)[0]
        self.detokenize_backlog.put((first_token, True, row.id, slot), block=True)
        continue

      if len(self.prefill_buckets[padded_len // 2]) != 0:
        prefill_batch(self.prefill_buckets[padded_len // 2], padded_len // 2)
        self.prefill_buckets[padded_len // 2] = []
      if padded_len == self.max_prefill_length:
        first_token, slot, row = prefill([(slot, row)], padded_len)[0]
        self.detokenize_backlog.put((first_token, True, row.id, slot), block=True)
        continue
      if padded_len == 64:
        row.tokens = jnp.concat([row.tokens, jnp.zeros(64, dtype=int)])
        padded_len = 128

      self.prefill_buckets[padded_len].append((slot, row))
      prefill_buckets_len = {k: len(self.prefill_buckets[k]) for k in self.prefill_buckets}
      log.debug("prefill buckets %d", prefill_buckets_len)
      if len(self.prefill_buckets[padded_len]) * padded_len >= self.max_prefill_length:
        total_true_len = sum((row.true_length for (slot, row) in self.prefill_buckets[padded_len]))
        # Can't hold another buffer, prefill right away
        if self.max_prefill_length - padded_len // 2 < total_true_len <= self.max_prefill_length:
          log.debug(
              "Normal batch %d total padded len %d total true len %d",
              padded_len,
              len(self.prefill_buckets[padded_len]) * padded_len,
              total_true_len,
          )
          prefill_batch(self.prefill_buckets[padded_len], padded_len)
          self.prefill_buckets[padded_len] = []
        # Already overloading, left over the last and do prefill
        elif total_true_len > self.max_prefill_length:
          log.debug(
              "Overloading %d total padded len %d total true len %d",
              padded_len,
              len(self.prefill_buckets[padded_len]) * padded_len,
              total_true_len,
          )
          current = self.prefill_buckets[padded_len][-1]
          prefill_batch(self.prefill_buckets[padded_len][:-1], padded_len)
          self.prefill_buckets[padded_len] = [current]
    # For leftover requests in buckets at the end of computation, do prefill individually.
    for padded_len in self.prefill_buckets:
      prefill_batch(self.prefill_buckets[padded_len], padded_len)
    self.prefill_buckets = defaultdict(list)
    while slot_to_id:
      log.info("decode-%s-%d num_filled_slots %d", desc, num_decodes, len(slot_to_id))
      num_decodes += 1
      decode()

    self.live = False
    detokenize_thread.join()
    log.info("summary-%s-prefills-%d-decodes-%d completed.", desc, num_prefills, num_decodes)

  def batch_inference(self, data: List[InputData], desc=""):
    """data is list of obj with id, tokens, and true length"""
    data_dict = defaultdict(list)
    log.info("sorting data")
    for row in data:
      data_dict[row.tokens.shape[0]].append(row)
    data_dict[128] += data_dict[64]
    data_dict[64] = []
    data = []
    for padded_len in [128, 256, 512, 1024]:
      log.info("padded len: %d, num: %d", padded_len, len(data_dict[padded_len]))
      random.shuffle(data_dict[padded_len])
      data += data_dict[padded_len]
    log.info("finished sorting data")
    res = defaultdict(list)

    def callback(id_, token):
      nonlocal res
      if token == self.tokenizer.eos_id:
        log.debug("res[%d] eos", id_)
      if not res[id_] or res[id_][-1] != self.tokenizer.eos_id:
        res[id_].append(token)
      return token == self.tokenizer.eos_id

    self.batch_inference_with_callback(data, emit_first_token=callback, emit_token=callback, desc=desc)
    return res

"""
Copyright 2024 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

     https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

"""Integration tests for test_checkpointing.sh"""
from datetime import datetime
import subprocess
import os.path
import pytest
from MaxText.globals import PKG_DIR
from MaxText.tests.globals import TEST_DISABLE_SUBPROCESS, TEST_DISABLE_SUBPROCESS_STR


def run_checkpointing(attention_type):
  """Tests grain checkpoint determinism."""

  run_date = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
  script_path = os.path.join(os.path.dirname(PKG_DIR), "end_to_end", "test_checkpointing.sh")
  if not os.path.isfile(script_path):
    raise FileNotFoundError(script_path)
  command = [
      "bash",
      script_path,
      f"runner_{run_date}",  # run_name
      r"gs://runner-maxtext-logs",  # output_path
      r"gs://maxtext-dataset",  # dataset_path
      "False",  # collect_stack_trace
      "grain",  # dataset_type
      attention_type,
      "False",  # async_checkpointing"
  ]

  subprocess.run(command, check=True, cwd=os.path.dirname(PKG_DIR))


@pytest.mark.integration_test
@pytest.mark.tpu_only
@pytest.mark.skipif(TEST_DISABLE_SUBPROCESS, reason=TEST_DISABLE_SUBPROCESS_STR)
def test_autoselected_attention():
  run_checkpointing("autoselected")


@pytest.mark.integration_test
@pytest.mark.gpu_only
@pytest.mark.skipif(TEST_DISABLE_SUBPROCESS, reason=TEST_DISABLE_SUBPROCESS_STR)
def test_with_dot_product():
  run_checkpointing("dot_product")

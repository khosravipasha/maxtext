#!/bin/bash

# This file is both an integration test that runs once a day on a v4-16 and documentation for how to get started with Llama3-70b. 
# Please make sure you have run end_to_end/tpu/llama3/70b/1_test_llama3_70b.sh before running commands from this file. 

# The flow of this file is as follows:
# 1. Run decoding, finetuning of Llama3-70B with the converted checkpoint obtained from end_to_end/tpu/llama3/70b/1_test_llama3_70b.sh. Also, run pretraining of Llama3-70B
# 2. Convert the scanned checkpoint from step 1 into unscanned checkpoint format and run more efficient decoding.
# 3. Run decoding from the finetuned checkpoint from step 1


# Example Usage: export BASE_OUTPUT_PATH=/path/to/GCS/bucket; bash end_to_end/tpu/llama3/70b/2_test_llama3_70b.sh
# Use the same BASE_OUTPUT_PATH as end_to_end/tpu/llama3/70b/1_test_llama3_70b.sh
# Please note that in these two scripts (1_test_llama3_70b.sh and 2_test_llama3_70b.sh) BASE_OUTPUT_PATH is assumed to be already a unique path across multiple runs and 
# the subfolders names aka RUN_NAMEs are static. Please remember to change BASE_OUTPUT_PATH across different runs.

set -ex

# Installing torch for deps in forward_pass_logit_checker.py
python3 -m pip install torch --index-url https://download.pytorch.org/whl/cpu

export MODEL_VARIATION='llama3-70b'

if [ -z "${BASE_OUTPUT_PATH}" ]; then
    # Non-Googlers please remember to point `BASE_OUTPUT_PATH` to a GCS bucket that you own, this bucket will store all the files generated by MaxText during a run
    # Use the same BASE_OUTPUT_PATH as end_to_end/tpu/llama3/70b/1_test_llama3_70b.sh
    export BASE_OUTPUT_PATH=gs://runner-maxtext-logs/$(date +%Y-%m-%d-%H-%M)
    echo "BASE_OUTPUT_PATH is not set, using BASE_OUTPUT_PATH = ${BASE_OUTPUT_PATH}"
fi



# Non-Googlers please remember to point `DATASET_PATH` to the GCS bucket where you have your training data
export DATASET_PATH=gs://maxtext-dataset


# We define `CONVERTED_CHECKPOINT` to refer to the checkpoint subdirectory. This way it is easier to use this path in the `train.py` and `decode.py` commands
export CONVERTED_CHECKPOINT=${BASE_OUTPUT_PATH}/${MODEL_VARIATION}/scanned_chkpt/0/items
export RUN_NAME=unscanned_chkpt
# We defined path to unscanned checkpoint created in 1_test_llama3_70b.sh
export UNSCANNED_CKPT_PATH=${BASE_OUTPUT_PATH}/${RUN_NAME}/checkpoints/0/items

# We run decoding on the `UNSCANNED_CKPT_PATH` for efficient decoding on the unscanned version of the checkpoint. Note that this checkpoint only has parameters and no optimizer state. 
# So, we use it by specifying`load_parameters_path=${CONVERTED_CHECKPOINT}`
python3 -m MaxText.decode MaxText/configs/base.yml tokenizer_path=assets/tokenizer_llama3.tiktoken load_parameters_path=${UNSCANNED_CKPT_PATH} per_device_batch_size=1 run_name=runner_$(date +%Y-%m-%d-%H-%M) max_prefill_predict_length=4 max_target_length=16 dataset_type=synthetic async_checkpointing=false scan_layers=false model_name=${MODEL_VARIATION} attention=dot_product prompt="I love to"

# We can also run decoding (albeit in a bit unoptimized way) by using the scanned converted checkpoint located at `CONVERTED_CHECKPOINT`. Note again that this checkpoint only has parameters and no optimizer state. So, we use it by specifying`load_parameters_path=${CONVERTED_CHECKPOINT}`
python3 -m MaxText.decode MaxText/configs/base.yml tokenizer_path=assets/tokenizer_llama3.tiktoken load_parameters_path=${CONVERTED_CHECKPOINT} per_device_batch_size=1 run_name=runner_$(date +%Y-%m-%d-%H-%M) max_prefill_predict_length=4 max_target_length=16 dataset_type=synthetic async_checkpointing=false model_name=${MODEL_VARIATION} attention=dot_product prompt="I love to"

# Alternatively, we skip to running finetuning by using the scanned converted checkpoint located at `CONVERTED_CHECKPOINT`. Again, we use it by specifying`load_parameters_path=${CONVERTED_CHECKPOINT}`. Note that scanned checkpoint helps with efficient finetuning
export FINETUNE_RUN_NAME=runner_finetune
python3 -m MaxText.train MaxText/configs/base.yml base_output_directory=${BASE_OUTPUT_PATH} dataset_path=${DATASET_PATH} tokenizer_path=assets/tokenizer_llama3.tiktoken load_parameters_path=${CONVERTED_CHECKPOINT} per_device_batch_size=1 run_name=${FINETUNE_RUN_NAME} steps=10 async_checkpointing=false model_name=${MODEL_VARIATION} checkpoint_period=5

# We also run pre-training, this is similar to the finetuning command except we don't pass any checkpoint directory to load parameters from
python3 -m MaxText.train MaxText/configs/base.yml base_output_directory=${BASE_OUTPUT_PATH} dataset_path=${DATASET_PATH} tokenizer_path=assets/tokenizer_llama3.tiktoken per_device_batch_size=1 run_name=runner_$(date +%Y-%m-%d-%H-%M) steps=5 enable_checkpointing=false model_name=${MODEL_VARIATION}

# Note that the finetune run checkpoint generates the `full state` which has both parameters and optimizer state. For decoding, we only need to use the parameters. 
# So, we can use the `MaxText/generate_param_only_checkpoint.py` to convert the full state checkpoint into a parameter only checkpoint for more efficient memory use. Note that the path provided to the flag `load_full_state_path` is the path to the checkpoint subdirectory inside the `BASE_OUTPUT_PATH` from our previous finetuning run.
# `force_unroll=true` is converting the output parameter only checkpoint into an unscanned format for efficient decoding
export PARAM_RUN_NAME=param_chkpt
python3 -m MaxText.generate_param_only_checkpoint MaxText/configs/base.yml base_output_directory=${BASE_OUTPUT_PATH} load_full_state_path=${BASE_OUTPUT_PATH}/${FINETUNE_RUN_NAME}/checkpoints/5/items run_name=${PARAM_RUN_NAME} model_name=${MODEL_VARIATION} force_unroll=true

# Now, run decoding on the checkpoint generated from our finetune run.
python3 -m MaxText.decode MaxText/configs/base.yml tokenizer_path=assets/tokenizer_llama3.tiktoken load_parameters_path=${BASE_OUTPUT_PATH}/${PARAM_RUN_NAME}/checkpoints/0/items per_device_batch_size=1 run_name=runner_$(date +%Y-%m-%d-%H-%M) max_prefill_predict_length=4 max_target_length=16 dataset_type=synthetic steps=10 async_checkpointing=false scan_layers=false model_name=${MODEL_VARIATION} attention=dot_product prompt="I love to"

# We also test whether the forward pass logits match the golden logits for Llama3-70B
python3 -m MaxText.tests.forward_pass_logit_checker MaxText/configs/base.yml base_output_directory=${BASE_OUTPUT_PATH} tokenizer_path=assets/tokenizer_llama3.tiktoken load_parameters_path=${UNSCANNED_CKPT_PATH} run_name=forward_pass_test per_device_batch_size=1 model_name=${MODEL_VARIATION} max_prefill_predict_length=4 max_target_length=4 dataset_type=synthetic dtype=float32 async_checkpointing=false scan_layers=false

# coding=utf-8
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

import torch

from megatron import get_args
from megatron import get_timers
from megatron import mpu
from megatron import get_num_microbatches
from megatron.p2p_communication import recv_forward, recv_backward
from megatron.p2p_communication import send_forward, send_backward
from megatron.p2p_communication import send_forward_recv_backward, send_backward_recv_forward
from megatron.p2p_communication import send_forward_recv_forward, send_backward_recv_backward
from megatron.p2p_communication import send_forward_backward_recv_forward_backward

input_tensors = []
output_tensors  = []


def forward_step(forward_step_func, data_iterator, model, input_tensor, losses_reduced):
    """Forward step."""
    timers = get_timers()

    timers('forward-compute').start()
    output_tensor = forward_step_func(data_iterator, model, input_tensor)
    if mpu.is_pipeline_last_stage():
        loss, loss_reduced = output_tensor
        output_tensor = loss / get_num_microbatches()
        losses_reduced.append(loss_reduced)
    timers('forward-compute').stop()

    return output_tensor


def backward_step(optimizer, input_tensor, output_tensor, output_tensor_grad):
    """Backward step."""
    args = get_args()

    timers = get_timers()
    timers('backward-compute').start()

    # Retain the grad on the input_tensor.
    if input_tensor is not None:
        input_tensor.retain_grad()

    # Backward pass.
    if output_tensor_grad is None:
        output_tensor = optimizer.scale_loss(output_tensor)
    torch.autograd.backward(output_tensor, grad_tensors=output_tensor_grad)

    # Collect the grad of the input_tensor.
    input_tensor_grad = None
    if input_tensor is not None:
        input_tensor_grad = input_tensor.grad

    timers('backward-compute').stop()

    return input_tensor_grad


def forward_backward_no_pipelining(forward_step_func, data_iterator, model,
                                   optimizer, timers, forward_only):
    """Run forward and backward passes without inter-stage communication."""
    assert len(model) == 1
    model = model[0]

    losses_reduced = []
    for i in range(get_num_microbatches()):
        input_tensor, output_tensor_grad = None, None
        output_tensor = forward_step(forward_step_func, data_iterator, model,
                                     input_tensor, losses_reduced)
        if not forward_only:
            backward_step(optimizer, input_tensor, output_tensor,
                          output_tensor_grad)

    return losses_reduced


def forward_backward_pipelining_with_interleaving(forward_step_func, data_iterator, model,
                                                  optimizer, timers, forward_only):
    """Run interleaved 1F1B schedule."""
    input_tensors = [[] for _ in range(len(model))]
    output_tensors = [[] for _ in range(len(model))]
    losses_reduced = []
    if not forward_only:
        output_tensor_grads = [[] for _ in range(len(model))]

    pipeline_parallel_size = mpu.get_pipeline_model_parallel_world_size()
    pipeline_parallel_rank = mpu.get_pipeline_model_parallel_rank()

    # Compute number of warmup and remaining microbatches.
    num_model_chunks = len(model)
    num_microbatches = get_num_microbatches() * num_model_chunks
    all_warmup_microbatches = False
    if forward_only:
        num_warmup_microbatches = num_microbatches
    else:
        if get_num_microbatches() == pipeline_parallel_size:
            num_warmup_microbatches = num_microbatches
            all_warmup_microbatches = True
        else:
            num_warmup_microbatches = \
                (pipeline_parallel_size - pipeline_parallel_rank - 1) * 2
            num_warmup_microbatches += (num_model_chunks - 1) * pipeline_parallel_size
            num_warmup_microbatches = min(num_warmup_microbatches, num_microbatches)
    num_microbatches_remaining = \
        num_microbatches - num_warmup_microbatches

    def get_model_chunk_id(k, forward):
        k_in_group = k % (pipeline_parallel_size * num_model_chunks)
        i = k_in_group // pipeline_parallel_size
        if not forward:
            i = (num_model_chunks - i - 1)
        return i

    def forward_step_helper(k):
        model_chunk_id = get_model_chunk_id(k, forward=True)
        mpu.set_virtual_pipeline_model_parallel_rank(model_chunk_id)

        if mpu.is_pipeline_first_stage():
            if len(input_tensors[model_chunk_id]) == len(output_tensors[model_chunk_id]):
                input_tensors[model_chunk_id].append(None)
        input_tensor = input_tensors[model_chunk_id][-1]
        output_tensor = forward_step(forward_step_func, data_iterator[model_chunk_id],
                                     model[model_chunk_id],
                                     input_tensor, losses_reduced)
        output_tensors[model_chunk_id].append(output_tensor)

        return output_tensor

    def backward_step_helper(k):
        model_chunk_id = get_model_chunk_id(k, forward=False)
        mpu.set_virtual_pipeline_model_parallel_rank(model_chunk_id)

        if mpu.is_pipeline_last_stage():
            if len(output_tensor_grads[model_chunk_id]) == 0:
                output_tensor_grads[model_chunk_id].append(None)
        input_tensor = input_tensors[model_chunk_id].pop(0)
        output_tensor = output_tensors[model_chunk_id].pop(0)
        output_tensor_grad = output_tensor_grads[model_chunk_id].pop(0)
        input_tensor_grad = \
            backward_step(optimizer, input_tensor, output_tensor, output_tensor_grad)

        return input_tensor_grad

    # Run warmup forward passes.
    mpu.set_virtual_pipeline_model_parallel_rank(0)
    input_tensors[0].append(recv_forward(timers, use_ring_exchange=True))
    for k in range(num_warmup_microbatches):
        output_tensor = forward_step_helper(k)
        next_forward_model_chunk_id = get_model_chunk_id(k+1, forward=True)
        recv_prev = True
        if mpu.is_pipeline_first_stage(ignore_virtual=True):
            if next_forward_model_chunk_id == 0:
                recv_prev = False
        if k == (num_microbatches - 1):
            recv_prev = False
        if mpu.is_pipeline_last_stage():
            output_tensor = None
        if k == (num_warmup_microbatches - 1) and not forward_only and \
                not all_warmup_microbatches:
            input_tensor_grad = None
            recv_next = True
            if mpu.is_pipeline_last_stage(ignore_virtual=True):
                recv_next = False
            input_tensor, output_tensor_grad = \
                send_forward_backward_recv_forward_backward(
                        output_tensor, input_tensor_grad,
                        recv_prev=recv_prev, recv_next=recv_next,
                        timers=timers)
            output_tensor_grads[num_model_chunks-1].append(output_tensor_grad)
        else:
            input_tensor = send_forward_recv_forward(output_tensor, recv_prev, timers)
        input_tensors[next_forward_model_chunk_id].append(input_tensor)

    # Run 1F1B in steady state.
    for k in range(num_microbatches_remaining):
        # Forward pass.
        forward_k = k + num_warmup_microbatches
        output_tensor = forward_step_helper(forward_k)

        # Backward pass.
        backward_k = k
        input_tensor_grad = backward_step_helper(backward_k)

        # Send output_tensor and input_tensor_grad, receive input_tensor
        # and output_tensor_grad.

        # Determine if current stage has anything to send in either direction,
        # otherwise set tensor to None.
        forward_model_chunk_id = get_model_chunk_id(forward_k, forward=True)
        mpu.set_virtual_pipeline_model_parallel_rank(forward_model_chunk_id)
        if mpu.is_pipeline_last_stage():
            output_tensor = None

        backward_model_chunk_id = get_model_chunk_id(backward_k, forward=False)
        mpu.set_virtual_pipeline_model_parallel_rank(backward_model_chunk_id)
        if mpu.is_pipeline_first_stage():
            input_tensor_grad = None

        # Determine if peers are sending, and where in data structure to put
        # received tensors.
        recv_prev = True
        if mpu.is_pipeline_first_stage(ignore_virtual=True):
            # First stage is ahead of last stage by (pipeline_parallel_size - 1).
            next_forward_model_chunk_id = get_model_chunk_id(
                forward_k - (pipeline_parallel_size - 1), forward=True)
            if next_forward_model_chunk_id == (num_model_chunks - 1):
                recv_prev = False
            next_forward_model_chunk_id += 1
        else:
            next_forward_model_chunk_id = get_model_chunk_id(forward_k + 1, forward=True)

        recv_next = True
        if mpu.is_pipeline_last_stage(ignore_virtual=True):
            # Last stage is ahead of first stage by (pipeline_parallel_size - 1).
            next_backward_model_chunk_id = get_model_chunk_id(
                backward_k - (pipeline_parallel_size - 1), forward=False)
            if next_backward_model_chunk_id == 0:
                recv_next = False
            next_backward_model_chunk_id -= 1
        else:
            next_backward_model_chunk_id = get_model_chunk_id(backward_k + 1, forward=False)

        # If last iteration, don't receive; we already received one extra before the
        # start of the for loop.
        if k == (num_microbatches_remaining - 1):
            recv_prev = False

        # Communicate tensors.
        input_tensor, output_tensor_grad = \
            send_forward_backward_recv_forward_backward(
                    output_tensor, input_tensor_grad,
                    recv_prev=recv_prev, recv_next=recv_next,
                    timers=timers)

        # Put input_tensor and output_tensor_grad in data structures in the right location.
        if recv_prev:
            input_tensors[next_forward_model_chunk_id].append(input_tensor)
        if recv_next:
            output_tensor_grads[next_backward_model_chunk_id].append(output_tensor_grad)

    # Run cooldown backward passes.
    if not forward_only:
        if all_warmup_microbatches:
            output_tensor_grads[num_model_chunks-1].append(
                recv_backward(timers, use_ring_exchange=True))
        for k in range(num_microbatches_remaining, num_microbatches):
            input_tensor_grad = backward_step_helper(k)
            next_backward_model_chunk_id = get_model_chunk_id(k+1, forward=False)
            recv_next = True
            if mpu.is_pipeline_last_stage(ignore_virtual=True):
                if next_backward_model_chunk_id == (num_model_chunks - 1):
                    recv_next = False
            if k == (num_microbatches - 1):
                recv_next = False
            output_tensor_grads[next_backward_model_chunk_id].append(
                send_backward_recv_backward(input_tensor_grad, recv_next, timers))

    return losses_reduced


def forward_backward_pipelining(forward_step_func, data_iterator, model,
                                optimizer, timers, forward_only):
    """Run 1F1B schedule, with communication and warmup + cooldown microbatches as needed."""
    timers = get_timers()
    args = get_args()

    assert len(model) == 1
    model = model[0]

    # Compute number of warmup microbatches.
    num_microbatches = get_num_microbatches()
    num_warmup_microbatches = \
        (mpu.get_pipeline_model_parallel_world_size() -
         mpu.get_pipeline_model_parallel_rank() - 1)
    num_warmup_microbatches = min(
        num_warmup_microbatches,
        num_microbatches)
    if num_microbatches == 1:
        num_warmup_microbatches = 1
    if args.gpipe:
        num_warmup_microbatches = num_microbatches
    num_microbatches_remaining = \
        num_microbatches - num_warmup_microbatches

    input_tensors = []
    output_tensors = []
    losses_reduced = []

    # Run warmup forward passes.
    for i in range(num_warmup_microbatches):
        input_tensor = recv_forward(timers)
        output_tensor = forward_step(forward_step_func, data_iterator, model,
                                     input_tensor, losses_reduced)
        # Barrier before first receive to measure forward stall.
        if i == (num_warmup_microbatches - 1) and not args.gpipe and num_microbatches > 1:
            timers('forward-pipeline-stall').start()
            torch.distributed.barrier(group=mpu.get_pipeline_model_parallel_group())
            timers('forward-pipeline-stall').stop()
        send_forward(output_tensor, timers)

        input_tensors.append(input_tensor)
        output_tensors.append(output_tensor)

    # Barrier before first receive to measure forward stall.
    if num_warmup_microbatches == 0 and not args.gpipe:
        timers('forward-pipeline-stall').start()
        torch.distributed.barrier(group=mpu.get_pipeline_model_parallel_group())
        timers('forward-pipeline-stall').stop()

    # Before running 1F1B, need to receive first forward tensor.
    # If all microbatches are run in warmup / cooldown phase, then no need to
    # receive this tensor here.
    if num_microbatches_remaining > 0:
        input_tensor = recv_forward(timers)

    # Run 1F1B in steady state.
    for i in range(num_microbatches_remaining):
        last_iteration_in_for_loop = (i == (num_microbatches_remaining - 1))

        output_tensor = forward_step(forward_step_func, data_iterator, model,
                                     input_tensor, losses_reduced)
        if forward_only:
            send_forward(output_tensor, timers)
        else:
            output_tensor_grad = send_forward_recv_backward(output_tensor, timers)

        # Add input_tensor and output_tensor to end of list, then pop from the
        # start of the list for backward pass.
        input_tensors.append(input_tensor)
        output_tensors.append(output_tensor)

        if forward_only:
            if not last_iteration_in_for_loop:
                input_tensor = recv_forward(timers)
        else:
            input_tensor, output_tensor = input_tensors.pop(0), output_tensors.pop(0)

            input_tensor_grad = \
                backward_step(optimizer, input_tensor, output_tensor,
                              output_tensor_grad)

            if last_iteration_in_for_loop:
                input_tensor = None
                send_backward(input_tensor_grad, timers)
            else:
                input_tensor = send_backward_recv_forward(input_tensor_grad, timers)

    # Run cooldown backward passes.
    if not forward_only:
        for i in range(num_warmup_microbatches):
            input_tensor = input_tensors.pop(0)
            output_tensor = output_tensors.pop(0)

            output_tensor_grad = recv_backward(timers)

            input_tensor_grad = \
                backward_step(optimizer, input_tensor, output_tensor,
                              output_tensor_grad)

            send_backward(input_tensor_grad, timers)

    return losses_reduced


def forward_backward_pipelining_no_flushes(forward_step_func, data_iterator, model,
                                           optimizer, timers,
                                           first_iteration=False, last_iteration=False):
    """Run 1F1B schedule, with communication and warmup + cooldown microbatches as needed."""
    timers = get_timers()

    assert len(model) == 1
    model = model[0]

    # Compute number of warmup microbatches.
    num_microbatches = get_num_microbatches()
    num_warmup_microbatches = \
        (mpu.get_pipeline_model_parallel_world_size() -
         mpu.get_pipeline_model_parallel_rank() - 1)
    num_warmup_microbatches = min(
        num_warmup_microbatches,
        num_microbatches)
    num_cooldown_microbatches = num_warmup_microbatches
    if last_iteration:
        num_microbatches_remaining = \
            num_microbatches - num_warmup_microbatches
    else:
        num_microbatches_remaining = num_microbatches
    num_warmup_microbatches_in_first_iteration = num_warmup_microbatches
    if not first_iteration:
        num_warmup_microbatches = 0
    if not last_iteration:
        num_cooldown_microbatches = 0

    global input_tensors, output_tensors
    losses_reduced = []

    # Run warmup forward passes.
    for i in range(num_warmup_microbatches):
        input_tensor = recv_forward(timers)
        optimizer.swap_to_older_version()
        output_tensor = forward_step(forward_step_func, data_iterator, model,
                                     input_tensor, losses_reduced)
        # Barrier before first receive to measure forward stall.
        if i == (num_warmup_microbatches - 1):
            timers('forward-pipeline-stall').start()
            torch.distributed.barrier(group=mpu.get_pipeline_model_parallel_group())
            timers('forward-pipeline-stall').stop()
        send_forward(output_tensor, timers)

        input_tensors.append(input_tensor)
        output_tensors.append(output_tensor)

    if first_iteration:
        # Barrier before first receive to measure forward stall.
        if num_warmup_microbatches == 0:
            timers('forward-pipeline-stall').start()
            torch.distributed.barrier(group=mpu.get_pipeline_model_parallel_group())
            timers('forward-pipeline-stall').stop()

        # Before running 1F1B, need to receive first forward tensor.
        # If all microbatches are run in warmup / cooldown phase, then no need to
        # receive this tensor here.
        if num_microbatches_remaining > 0:
            input_tensors.append(recv_forward(timers))

    # Run 1F1B in steady state.
    for i in range(num_microbatches_remaining):
        last_iteration_in_for_loop = (i == (num_microbatches_remaining - 1))

        input_tensor = input_tensors[-1]
        if i < (num_microbatches - num_warmup_microbatches_in_first_iteration):
            optimizer.swap_to_older_version()
        else:
            optimizer.swap_to_newer_version()
        output_tensor = forward_step(forward_step_func, data_iterator, model,
                                     input_tensor, losses_reduced)
        output_tensor_grad = send_forward_recv_backward(output_tensor, timers)

        # Add output_tensor to end of list, then pop from the
        # start of the list for backward pass.
        output_tensors.append(output_tensor)

        input_tensor, output_tensor = input_tensors.pop(0), output_tensors.pop(0)

        optimizer.swap_to_older_version()
        input_tensor_grad = \
            backward_step(optimizer, input_tensor, output_tensor,
                          output_tensor_grad)

        if last_iteration_in_for_loop and last_iteration:
            input_tensor = None
            send_backward(input_tensor_grad, timers)
        else:
            input_tensor = send_backward_recv_forward(input_tensor_grad, timers)
            input_tensors.append(input_tensor)

    # Run cooldown backward passes.
    for i in range(num_cooldown_microbatches):
        input_tensor = input_tensors.pop(0)
        output_tensor = output_tensors.pop(0)

        output_tensor_grad = recv_backward(timers)

        optimizer.swap_to_older_version()
        input_tensor_grad = \
            backward_step(optimizer, input_tensor, output_tensor,
                          output_tensor_grad)

        send_backward(input_tensor_grad, timers)

    return losses_reduced

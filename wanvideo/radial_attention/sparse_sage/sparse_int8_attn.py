"""
Copyright (c) 2024 by SageAttention team.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import torch, math
import triton
import triton.language as tl
import torch.nn.functional as F

@triton.jit
def _attn_fwd_inner(acc, l_i, old_m, q, q_scale, kv_len,
                    K_ptrs, K_bid_ptr, K_scale_ptr, V_ptrs, stride_kn, stride_vn, start_m,  
                    BLOCK_M: tl.constexpr, HEAD_DIM: tl.constexpr, BLOCK_N: tl.constexpr,  
                    STAGE: tl.constexpr, offs_m: tl.constexpr, offs_n: tl.constexpr,  
                    ):
    if STAGE == 1:
        lo, hi = 0, start_m * BLOCK_M
    elif STAGE == 2:
        lo, hi = start_m * BLOCK_M, (start_m + 1) * BLOCK_M
        lo = tl.multiple_of(lo, BLOCK_M)
        K_scale_ptr += lo // BLOCK_N
        K_ptrs += stride_kn * lo
        V_ptrs += stride_vn * lo
    elif STAGE == 3:
        lo, hi = 0, kv_len
    for start_n in range(lo, hi, BLOCK_N):
        kbid = tl.load(K_bid_ptr + start_n//BLOCK_N)
        if kbid:
            k_mask = offs_n[None, :] < (kv_len - start_n)   
            k = tl.load(K_ptrs, mask = k_mask)
            k_scale = tl.load(K_scale_ptr)
            qk = tl.dot(q, k).to(tl.float32) * q_scale * k_scale 
            if STAGE == 2:
                mask = offs_m[:, None] >= (start_n + offs_n[None, :])
                qk = qk + tl.where(mask, 0, -1.0e6)
                local_m = tl.max(qk, 1)
                new_m = tl.maximum(old_m, local_m)
                qk -= new_m[:, None]
            else:
                local_m = tl.max(qk, 1)
                new_m = tl.maximum(old_m, local_m)
                qk = qk - new_m[:, None]

            p = tl.math.exp2(qk)
            l_ij = tl.sum(p, 1)
            alpha = tl.math.exp2(old_m - new_m)
            l_i = l_i * alpha + l_ij
            acc = acc * alpha[:, None]
            v = tl.load(V_ptrs, mask = offs_n[:, None] < (kv_len - start_n))
            p = p.to(tl.float16)                     
            acc += tl.dot(p, v, out_dtype=tl.float16)   
            old_m = new_m
        K_ptrs += BLOCK_N * stride_kn
        K_scale_ptr += 1
        V_ptrs += BLOCK_N * stride_vn
    return acc, l_i, old_m

@triton.jit
def _attn_fwd(Q, K, K_blkid, V, Q_scale, K_scale, Out,  
              stride_qz, stride_qh, stride_qn,
              stride_kz, stride_kh, stride_kn,  
              stride_vz, stride_vh, stride_vn,  
              stride_oz, stride_oh, stride_on,  
              stride_kbidq, stride_kbidk,
              qo_len, kv_len, H:tl.constexpr, num_kv_groups:tl.constexpr, 
              HEAD_DIM: tl.constexpr,  
              BLOCK_M: tl.constexpr,  
              BLOCK_N: tl.constexpr,  
              STAGE: tl.constexpr
              ):
    start_m = tl.program_id(0)
    off_z = tl.program_id(2).to(tl.int64)
    off_h = tl.program_id(1).to(tl.int64)
    q_scale_offset = (off_z * H + off_h) * tl.cdiv(qo_len, BLOCK_M)
    k_scale_offset = (off_z * (H // num_kv_groups) + off_h // num_kv_groups) * tl.cdiv(kv_len, BLOCK_N)  
    k_bid_offset = (off_z * (H // num_kv_groups) + off_h // num_kv_groups) * stride_kbidq
    offs_m = start_m * BLOCK_M + tl.arange(0, BLOCK_M)
    offs_n = tl.arange(0, BLOCK_N)
    offs_k = tl.arange(0, HEAD_DIM)
    Q_ptrs = Q + (off_z * stride_qz + off_h * stride_qh) + offs_m[:, None] * stride_qn + offs_k[None, :]
    Q_scale_ptr = Q_scale + q_scale_offset + start_m
    K_ptrs = K + (off_z * stride_kz + (off_h // num_kv_groups) * stride_kh) + offs_n[None, :] * stride_kn + offs_k[:, None] 
    K_scale_ptr = K_scale + k_scale_offset
    K_bid_ptr = K_blkid + k_bid_offset + start_m * stride_kbidk 
    V_ptrs = V + (off_z * stride_vz + (off_h // num_kv_groups) * stride_vh) + offs_n[:, None] * stride_vn + offs_k[None, :]
    O_block_ptr = Out + (off_z * stride_oz + off_h * stride_oh) + offs_m[:, None] * stride_on + offs_k[None, :]
    m_i = tl.zeros([BLOCK_M], dtype=tl.float32) - float("inf")
    l_i = tl.zeros([BLOCK_M], dtype=tl.float32) + 1.0
    acc = tl.zeros([BLOCK_M, HEAD_DIM], dtype=tl.float32)
    q = tl.load(Q_ptrs, mask = offs_m[:, None] < qo_len)
    q_scale = tl.load(Q_scale_ptr)
    acc, l_i, m_i = _attn_fwd_inner(acc, l_i, m_i, q, q_scale, kv_len, K_ptrs, K_bid_ptr, K_scale_ptr, V_ptrs, stride_kn, stride_vn,
                                    start_m,  
                                    BLOCK_M, HEAD_DIM, BLOCK_N,  
                                    4 - STAGE, offs_m, offs_n 
                                    )
    if STAGE != 1:
        acc, l_i, _ = _attn_fwd_inner(acc, l_i, m_i, q, q_scale, kv_len, K_ptrs, K_bid_ptr, K_scale_ptr, V_ptrs, stride_kn, stride_vn,
                                        start_m,  
                                        BLOCK_M, HEAD_DIM, BLOCK_N,  
                                        2, offs_m, offs_n 
                                        )
    acc = acc / l_i[:, None]
    tl.store(O_block_ptr, acc.to(Out.type.element_ty), mask = (offs_m[:, None] < qo_len))


def forward(q, k, k_block_id, v, q_scale, k_scale, is_causal=False, tensor_layout="HND", output_dtype=torch.float16):
    BLOCK_M = 128
    BLOCK_N = 64
    stage = 3 if is_causal else 1
    o = torch.empty(q.shape, dtype=output_dtype, device=q.device)

    if tensor_layout == "HND":
        b, h_qo, qo_len, head_dim = q.shape
        _, h_kv, kv_len, _ = k.shape
        stride_bz_q, stride_h_q, stride_seq_q = q.stride(0), q.stride(1), q.stride(2)
        stride_bz_k, stride_h_k, stride_seq_k = k.stride(0), k.stride(1), k.stride(2)
        stride_bz_v, stride_h_v, stride_seq_v = v.stride(0), v.stride(1), v.stride(2)
        stride_bz_o, stride_h_o, stride_seq_o = o.stride(0), o.stride(1), o.stride(2)
    elif tensor_layout == "NHD":
        b, qo_len, h_qo, head_dim = q.shape
        _, kv_len, h_kv, _ = k.shape
        stride_bz_q, stride_h_q, stride_seq_q = q.stride(0), q.stride(2), q.stride(1)
        stride_bz_k, stride_h_k, stride_seq_k = k.stride(0), k.stride(2), k.stride(1)
        stride_bz_v, stride_h_v, stride_seq_v = v.stride(0), v.stride(2), v.stride(1)
        stride_bz_o, stride_h_o, stride_seq_o = o.stride(0), o.stride(2), o.stride(1)
    else:
        raise ValueError(f"tensor_layout {tensor_layout} not supported")
    
    if is_causal:
        assert qo_len == kv_len, "qo_len and kv_len must be equal for causal attention"

    HEAD_DIM_K = head_dim
    num_kv_groups = h_qo // h_kv

    grid = (triton.cdiv(qo_len, BLOCK_M), h_qo, b   )
    _attn_fwd[grid](
        q, k, k_block_id, v, q_scale, k_scale, o,  
        stride_bz_q, stride_h_q, stride_seq_q, 
        stride_bz_k, stride_h_k, stride_seq_k,  
        stride_bz_v, stride_h_v, stride_seq_v,  
        stride_bz_o, stride_h_o, stride_seq_o,
        k_block_id.stride(1), k_block_id.stride(2),
        qo_len, kv_len,
        h_qo, num_kv_groups,
        BLOCK_M=BLOCK_M, BLOCK_N=BLOCK_N, HEAD_DIM=HEAD_DIM_K,  
        STAGE=stage,  
        num_warps=4 if head_dim == 64 else 8,
        num_stages=4)
    return o

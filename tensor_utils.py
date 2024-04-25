import torch
from torch import nn


def roll_tensor(tensor, shifts, dim):
    # 获取tensor的形状
    shape = tensor.size()

    # 确保dim在有效范围内
    assert dim >= 0 and dim < len(shape), "Invalid dimension"

    # 生成一个索引tensor
    indices = (
        torch.arange(shape[dim], device=tensor.device)
        .view([1] * dim + [-1] + [1] * (len(shape) - dim - 1))
        .expand(shape)
    )

    # 将shifts转换为tensor并调整形状
    shifts = shifts.view([-1] + [1] * (len(shape) - 1))

    # 计算移位后的索引
    shifted_indices = (indices - shifts) % shape[dim]

    # 使用移位后的索引对tensor进行索引操作
    result = tensor.gather(dim, shifted_indices.expand(shape))

    return result


def roll_tensor_1d(tensor, shifts):
    # 获取tensor的形状
    shape = tensor.size()
    dim = 1  # 沿着第二个维度进行移位

    # 确保dim在有效范围内
    assert dim >= 0 and dim < len(shape), "Invalid dimension"

    # 生成一个索引tensor
    indices = (
        torch.arange(shape[dim])
        .view([1] * dim + [-1] + [1] * (len(shape) - dim - 1))
        .expand(shape)
    )

    # 将shifts转换为tensor并调整形状
    shifts = shifts.view([-1] + [1] * (len(shape) - 1))

    # 计算移位后的索引
    shifted_indices = (indices - shifts) % shape[dim]

    # 使用移位后的索引对tensor进行索引操作
    result = tensor.gather(dim, shifted_indices.expand(shape))

    return result


def shift_tensor(x, shifts_text_dim, shifts_mel_dim):
    """
    Shift the tensor x to the right along the text and mel dimensions.

    Args:
        x (torch.Tensor): The input tensor of shape (B, I, J, K).
        shifts_text_dim (torch.Tensor): The shift amounts along the text dimension of shape (B,).
        shifts_mel_dim (torch.Tensor): The shift amounts along the mel dimension of shape (B,).

    Returns:
        torch.Tensor: The right-shifted tensor of shape (B, I, J, K).
    """
    x = roll_tensor(x, shifts=shifts_text_dim, dim=1)
    x = roll_tensor(x, shifts=shifts_mel_dim, dim=2)
    return x


def one_hot(B, I, device):
    x = torch.full((I,), -float("inf"), device=device)
    x[0] = 0
    return x


def reverse_text_mel_direction_add_onehot(
    log_boundary_backward, text_mask_backward, mel_mask_backward
):
    B, I, _ = log_boundary_backward.shape

    onehot = one_hot(B, I, device=log_boundary_backward.device)[None, :, None].repeat(
        B, 1, 1
    )
    log_boundary_backward = torch.cat((onehot, log_boundary_backward), dim=2)
    shifts_text_dim = compute_max_length_diff(text_mask_backward)
    shifts_mel_dim = compute_max_length_diff(mel_mask_backward)
    log_boundary_backward = shift_tensor(
        log_boundary_backward.flip(1, 2),
        shifts_text_dim=shifts_text_dim,
        shifts_mel_dim=shifts_mel_dim,
    )
    log_boundary_backward = torch.cat((onehot, log_boundary_backward), dim=2)
    return log_boundary_backward


def compute_max_length_diff(mask):
    lengths = mask.sum(1)
    return lengths.max() - lengths


def gen_i_range_mask(B, I, J, K, i_lens, j_lens):
    indices_i = (
        torch.arange(I, device=i_lens.device).unsqueeze(0).unsqueeze(-1).repeat(B, 1, J)
    )
    indices_j = (
        torch.arange(J, device=i_lens.device).unsqueeze(0).unsqueeze(0).repeat(B, I, 1)
    )
    indices = indices_i + indices_j

    limit_s = (i_lens - 1).unsqueeze(-1).unsqueeze(-1).expand(B, I, J)
    limit_e = j_lens.unsqueeze(-1).unsqueeze(-1).expand(B, I, J)

    mask_b = (indices >= limit_s).flip(1)
    mask_e = (indices < limit_e).flip(1)

    mask = (mask_b & mask_e).unsqueeze(-1)
    diff = i_lens - i_lens.max()
    mask = roll_tensor(mask, shifts=diff, dim=1)

    bool_tensor = i_lens.unsqueeze(1) > torch.arange(I, device=i_lens.device)
    bool_tensor = bool_tensor[:, :, None, None].repeat(1, 1, J, 1)
    mask = mask * bool_tensor
    mask = mask.repeat(1, 1, 1, K)

    return mask


def gen_tri(B, I, J, K, device):
    triu = torch.triu(torch.ones((K, J), device=device), diagonal=0)
    triu = triu.unsqueeze(-1).unsqueeze(0)  # (1, K, J, 1)
    triu = triu.repeat(B, 1, 1, I)  # (B, K, J, I)
    triu = triu.transpose(1, 3)  # (B, I, J, K)
    return triu.bool()


def gen_most_i_mask(B, I, J, K, i_lens, j_lens, device):
    mask = torch.ones((B, I, J, K), dtype=torch.bool, device=device)
    for b in range(B):
        mask[b, i_lens[b] - 1, : j_lens[b] - 1] = False
    return mask


def get_invalid_tri_mask(B, I, J, K, text_mask, mel_mask, force_assign_last):
    i_lens = text_mask.sum(1)
    j_lens = mel_mask.sum(1)
    energy_mask = gen_i_range_mask(B, I, J, K, i_lens, j_lens)
    tri_ijk_mask = gen_tri(B, I, J, K, device=text_mask.device)
    if force_assign_last:
        most_i_mask = gen_most_i_mask(
            B, I, J, K, i_lens, j_lens, device=text_mask.device
        )
        return (~energy_mask) | (~tri_ijk_mask) | (~most_i_mask)
    else:
        return (~energy_mask) | (~tri_ijk_mask)


def pad_log_cond_prob_gt_backward(B, J, log_eps):
    x = torch.tril(torch.ones(B, 1, J - 1, J - 1), diagonal=1)
    x.masked_fill_(x == 0, log_eps)
    x.masked_fill_(x == 1, 0)
    return x


def geq_to_gt_and_pad_on_i_dim(
    log_cond_prob_geq_backward, text_mask, mel_mask, log_eps
):
    B, I = text_mask.shape
    _, J = mel_mask.shape

    log_cond_prob_gt_backward = log_cond_prob_geq_backward.roll(
        shifts=-1, dims=2
    )  # >= -> >

    pad = pad_log_cond_prob_gt_backward(B, J, log_eps)
    log_cond_prob_gt_backward = torch.cat((log_cond_prob_gt_backward, pad), dim=1)[
        :, :, :-1
    ]
    return log_cond_prob_gt_backward


if __name__ == "__main__":
    # 示例用法 1
    tensor1 = torch.tensor(
        [
            [[[1, 2, 3], [4, 5, 6]], [[7, 8, 9], [10, 11, 12]]],
            [[[13, 14, 15], [16, 17, 18]], [[19, 20, 21], [22, 23, 24]]],
        ]
    )
    shifts1 = torch.tensor([1, 2])  # 每个sample在最后一个维度上的右移量
    dim1 = 3  # 在最后一个维度上进行移位
    result1 = roll_tensor(tensor1, shifts1, dim1)
    print("示例 1 - 在最后一个维度上移位:")
    print(result1)

    # 示例用法 2
    tensor2 = torch.tensor(
        [
            [[1, 2], [3, 4], [5, 6]],
            [[7, 8], [9, 10], [11, 12]],
            [[13, 14], [15, 16], [17, 18]],
        ]
    )
    shifts2 = torch.tensor([1, 2, 3])  # 每个sample在第二个维度上的左移量
    dim2 = 1  # 在第二个维度上进行移位
    result2 = roll_tensor(tensor2, shifts2, dim2)
    print("示例 2 - 在第二个维度上移位:")
    print(result2)

    # 示例用法
    tensor = torch.tensor([[1, 2, 3, 4, 5], [6, 7, 8, 9, 10]])
    shifts = torch.tensor([1, 2])  # 第一行右移1个位置,第二行右移2个位置

    result = roll_tensor_1d(tensor, shifts)
    print(result)

    # 测试用例1
    B, I, J, K = 2, 5, 10, 10
    i_lens = torch.tensor([5, 2])
    j_lens = torch.tensor([10, 5])
    masked_tensor = gen_i_range_mask(B, I, J, K, i_lens, j_lens).int()
    print(masked_tensor.shape)

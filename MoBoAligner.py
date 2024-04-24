import torch
import torch.nn as nn
import math
from mask import *
from roll import roll_tensor, roll_tensor_1d
import numpy as np
import warnings
import monotonic_align


class MoBoAligner(nn.Module):
    def __init__(self, temperature_min=0.1, temperature_max=1.0):
        super(MoBoAligner, self).__init__()
        self.temperature_min = temperature_min
        self.temperature_max = temperature_max
        self.log_eps = -1000

    def check_parameter_validity(self, text_mask, mel_mask, direction):
        d = set(direction)
        assert len(d) >= 1 and d.issubset(set(["forward", "backward"])), "Direction must be a subset of 'forward' or 'backward'."
        if torch.any(text_mask.sum(1) >= mel_mask.sum(1)):
            warnings.warn("Warning: The dimension of text embeddings (I) is greater than or equal to the dimension of mel spectrogram embeddings (J), which may affect alignment performance.")

    def compute_energy(self, text_embeddings, mel_embeddings):
        """
        Compute the energy matrix between text embeddings and mel embeddings.

        Args:
            text_embeddings (torch.Tensor): The text embeddings of shape (B, I, D_text).
            mel_embeddings (torch.Tensor): The mel spectrogram embeddings of shape (B, J, D_mel).

        Returns:
            torch.Tensor: The energy matrix of shape (B, I, J).
        """
        text_channels = text_embeddings.size(-1)
        mel_channels = mel_embeddings.size(-1)
        return torch.bmm(text_embeddings, mel_embeddings.transpose(1, 2)) / math.sqrt(
            text_channels * mel_channels
        )

    def apply_gumbel_noise(self, energy, temperature_ratio):
        """
        Apply Gumbel noise and temperature to the energy matrix.

        Args:
            energy (torch.Tensor): The energy matrix of shape (B, I, J).
            temperature_ratio (float): The temperature ratio for Gumbel noise.

        Returns:
            torch.Tensor: The energy matrix with Gumbel noise and temperature applied.
        """
        temperature = (
            self.temperature_min
            + (self.temperature_max - self.temperature_min) * temperature_ratio
        )
        noise = -torch.log(-torch.log(torch.rand_like(energy)))
        return (energy + noise) / temperature
    
    def compute_energy_and_masks_backward(self, energy, text_mask, mel_mask):
        shifts_text_dim = self.compute_max_length_diff(text_mask)
        shifts_mel_dim = self.compute_max_length_diff(mel_mask)
        
        energy_backward = self.left_shift(energy.flip(1).flip(2), shifts_text_dim=shifts_text_dim, shifts_mel_dim=shifts_mel_dim)
        text_mask_backward = roll_tensor_1d(text_mask.flip(1), shifts=shifts_text_dim)
        mel_mask_backward = roll_tensor_1d(mel_mask.flip(1), shifts=shifts_mel_dim)

        energy_backward = energy_backward[:, 1:, 1:]
        text_mask_backward = text_mask_backward[:, 1:]
        mel_mask_backward = mel_mask_backward[:, 1:]
        return energy_backward, text_mask_backward, mel_mask_backward

    def compute_log_cond_prob(self, energy, text_mask, mel_mask):
        """
        Compute the log conditional probability of the alignment in the specified direction.

        Args:
            energy (torch.Tensor): The energy matrix of shape (B, I, J).
            text_mask (torch.Tensor): The text mask of shape (B, I).
            mel_mask (torch.Tensor): The mel spectrogram mask of shape (B, J).

        Returns:
            tuple: A tuple containing:
                - log_cond_prob (torch.Tensor): The log conditional probability tensor of shape (B, I, J).
                - log_cond_prob_geq (torch.Tensor): The log cumulative conditional probability tensor of shape (B, I, J)
        """
        B, I = text_mask.shape
        _, J = mel_mask.shape
        
        energy_4D = energy.unsqueeze(-1).repeat(1, 1, 1, J)  # (B, I, J, K)
        tri_invalid = get_invalid_tri_mask(B, I, J, J, text_mask, mel_mask)
        energy_4D.masked_fill_(tri_invalid, self.log_eps)
        log_cond_prob = energy_4D - torch.logsumexp(energy_4D, dim=2, keepdim=True) # on the J dimension

        log_cond_prob_geq = torch.logcumsumexp(log_cond_prob.flip(2), dim=2).flip(2)
        log_cond_prob_geq.masked_fill_(tri_invalid, self.log_eps)
        return log_cond_prob, log_cond_prob_geq

    def right_shift(self, x, shifts_text_dim, shifts_mel_dim):
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
        shifts_mel_dim[1:] = shifts_mel_dim[1:] - 1
        x = roll_tensor(x, shifts=shifts_mel_dim, dim=3)
        return x

    def left_shift(self, x, shifts_text_dim, shifts_mel_dim):
        """
        Shift the tensor x to the left along the text and mel dimensions.

        Args:
            x (torch.Tensor): The input tensor of shape (B, I, J).
            shifts_text_dim (torch.Tensor): The shift amounts along the text dimension of shape (B,).
            shifts_mel_dim (torch.Tensor): The shift amounts along the mel dimension of shape (B,).

        Returns:
            torch.Tensor: The left-shifted tensor of shape (B, I, J).
        """
        x = x.unsqueeze(-1)
        x = roll_tensor(x, shifts=-shifts_text_dim, dim=1)
        x = roll_tensor(x, shifts=-shifts_mel_dim, dim=2)
        x = x.squeeze(-1)
        return x

    def compute_max_length_diff(self, mask):
        """
        Compute the difference between the maximum length and the actual length for each sequence in the batch.

        Args:
            mask (torch.Tensor): The mask tensor of shape (B, L).

        Returns:
            torch.Tensor: The difference tensor of shape (B,).
        """
        lengths = mask.sum(1)
        return lengths.max() - lengths

    def compute_forward_pass(self, log_cond_prob, text_mask, mel_mask):
        """
        Compute forward recursively in the log domain.

        Args:
            log_cond_prob (torch.Tensor): The log conditional probability tensor for forward of shape (B, I, J, K).
            text_mask (torch.Tensor): The text mask of shape (B, I).
            mel_mask (torch.Tensor): The mel spectrogram mask of shape (B, J).

        Returns:
            torch.Tensor: The forward tensor of shape (B, I+1, J+1).
        """
        B, I = text_mask.shape
        _, J = mel_mask.shape

        B_ij = torch.full((B, I + 1, J + 1), -float("inf"), device=log_cond_prob.device)
        B_ij[:, 0, 0] = 0  # Initialize forward[0, 0] = 0
        B_ij[:, -1, -1] = 0  # Initialize forward[I, J] = 0
        for i in range(1, I):
            B_ij[:, i, i:(J - I + i + 2)] = torch.logsumexp(
                B_ij[:, i - 1, :-1].unsqueeze(1)
                + log_cond_prob[:, i - 1, (i - 1) : (J - I + i + 1)],
                dim=2, # sum at the K dimension
            )

        return B_ij

    def compute_boundary_prob(self, prob, log_cond_prob_geq_or_gt, text_mask, mel_mask):
        """
        Compute the log-delta, which is the log of the probability P(B_{i-1} < j <= B_i).

        Args:
            prob (torch.Tensor): The forward or backward tensor of shape (B, I+1, J+1) for forward, or (B, I, J) for backward.
            log_cond_prob_geq_or_gt (torch.Tensor): The log cumulative conditional probability tensor of shape (B, I, J).
            text_mask (torch.Tensor): The text mask of shape (B, I).
            mel_mask (torch.Tensor): The mel spectrogram mask of shape (B, J).

        Returns:
            torch.Tensor: The log-delta tensor of shape (B, I, J).
        """
        B, I = text_mask.shape
        _, J = mel_mask.shape
        x = prob[:, :-1, :-1].unsqueeze(-1).repeat(1, 1, 1, J) + log_cond_prob_geq_or_gt.transpose(2, 3) # (B, I, K, J)
        mask = gen_upper_left_mask(B, I, J, J)
        x.masked_fill_(mask == 0, self.log_eps) # for avoid logsumexp to produce -inf
        x = torch.logsumexp(x, dim = 2)
        mask = phone_boundary_mask(text_mask, mel_mask)
        y = x.masked_fill(mask, -float("Inf"))
        return y
    
    def combine_bidirectional_alignment(self, log_delta_forward, log_delta_backward):
        log_2 = math.log(2.0)
        log_delta = torch.logaddexp(log_delta_forward - log_2, log_delta_backward - log_2)
        return log_delta
    
    @torch.no_grad()
    def hard_alignment(self, log_probs, text_mask, mel_mask):
        """
        Compute the Viterbi path for the maximum alignment probabilities.

        Args:
            log_probs (torch.Tensor): The log probabilities tensor of shape (B, I, J).
            text_mask (torch.Tensor): The mask tensor of shape (B, I) indicating valid positions of text.
            mel_mask (torch.Tensor): The mask tensor of shape (B, J) indicating valid positions of mel.
        Returns:
            torch.Tensor: The tensor representing the hard alignment path of shape (B, I, J).
        """
        mask = text_mask.unsqueeze(-1) * mel_mask.unsqueeze(1)
        attn = monotonic_align.maximum_path(log_probs, mask)
        return attn

    def forward(
        self, text_embeddings, mel_embeddings, text_mask, mel_mask, temperature_ratio, direction
    ):
        """
        Compute the soft alignment (gamma) and the expanded text embeddings.

        Args:
            text_embeddings (torch.Tensor): The text embeddings of shape (B, I, D_text).
            mel_embeddings (torch.Tensor): The mel spectrogram embeddings of shape (B, J, D_mel).
            text_mask (torch.Tensor): The text mask of shape (B, I).
            mel_mask (torch.Tensor): The mel spectrogram mask of shape (B, J).
            temperature_ratio (float): The temperature ratio for Gumbel noise.
            direction (str): The direction of the alignment, subset of ["forward", "backward"].

        Returns:
            tuple: A tuple containing:
                - soft_alignment (torch.Tensor): The soft alignment tensor of shape (B, I, J) in the log domain.
                - hard_alignment (torch.Tensor): The hard alignment tensor of shape (B, I, J).
                - expanded_text_embeddings (torch.Tensor): The expanded text embeddings of shape (B, J, D_text).
        """
        # Check length of text < length of mel and direction is either "forward" or "backward"
        self.check_parameter_validity(text_mask, mel_mask, direction)

        # Compute the energy matrix
        energy = self.compute_energy(text_embeddings, mel_embeddings)

        # Apply Gumbel noise and temperature
        energy = self.apply_gumbel_noise(energy, temperature_ratio)

        if "forward" in direction:
            # Compute the log conditional probability P(B_i=j | B_{i-1}=k), P(B_i \geq j | B_{i-1}=k) for forward
            log_cond_prob_forward, log_cond_prob_forward_geq = self.compute_log_cond_prob(
                energy, text_mask, mel_mask
            )
            
            # Compute forward recursively in the log domain
            forward = self.compute_forward_pass(log_cond_prob_forward, text_mask, mel_mask)
            
            # Compute the forward P(B_{i-1}<j\leq B_i)
            log_delta_forward = self.compute_boundary_prob(forward, log_cond_prob_forward_geq, text_mask, mel_mask)
        
        if 'backward' in direction:
            # Compute the energy matrix for backward direction
            energy_backward, text_mask_backward, mel_mask_backward = self.compute_energy_and_masks_backward(energy, text_mask, mel_mask)
            
            # Compute the log conditional probability P(B_i=j | B_{i+1}=k), P(B_i \lt j | B_{i+1}=k) for backward
            log_cond_prob_backward, log_cond_prob_geq_backward = self.compute_log_cond_prob(
                energy_backward, text_mask_backward, mel_mask_backward
            )

            # Compute the log conditional probability P(B_i \gt j | B_{i+1}=k)
            log_cond_prob_gt_backward = log_cond_prob_geq_backward.roll(shifts=1, dims=2)

            # Compute backward recursively in the log domain
            backward = self.compute_forward_pass(log_cond_prob_backward, text_mask_backward, mel_mask_backward)

            # Compute the backward P(B_{i-1}<j\leq B_i)
            log_delta_backward = self.compute_boundary_prob(backward, log_cond_prob_gt_backward, text_mask_backward, mel_mask_backward)

        # Combine the forward and backward log-delta
        if direction == ["forward"]:
            soft_alignment = log_delta_forward
        elif direction == ["backward"]:
            soft_alignment = log_delta_backward
        else:
            soft_alignment = self.combine_bidirectional_alignment(log_delta_forward, log_delta_backward)

        # Use soft_alignment to compute the expanded text embeddings
        expanded_text_embeddings = torch.bmm(torch.exp(soft_alignment).transpose(1, 2), text_embeddings)
        expanded_text_embeddings = expanded_text_embeddings * mel_mask.unsqueeze(2)

        hard_alignment = self.hard_alignment(soft_alignment, text_mask, mel_mask)

        return soft_alignment, hard_alignment, expanded_text_embeddings
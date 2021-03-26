import torch
from torch import nn


class SCM(nn.Module):
    def forward(self, x: torch.Tensor, mask: torch.Tensor = None, normalize: bool = True):
        """Compute the spatial covariance matrix from a STFT signal x.

        Args:
            x (torch.ComplexTensor): shape  [batch, mics, freqs, frames]
            mask (torch.Tensor): [batch, 1, freqs, frames] or [batch, 1, freqs, frames]. Optional
            normalize (bool): Whether to normalize with the mask mean per bin.

        Returns:
            torch.ComplexTensor, the SCM with shape (batch, mics, mics, freqs)
        """
        batch, mics, freqs, frames = x.shape
        if mask is None:
            mask = torch.ones(batch, 1, freqs, frames)
        if mask.ndim == 3:
            mask = mask[:, None]

        psd = torch.einsum("bmft,bnft->bmnf", mask * x, x.conj())
        if normalize:
            psd /= mask.sum(-1, keepdim=True).transpose(-1, -2)
        return psd


class _BeamFormer(nn.Module):
    @staticmethod
    def apply_beamforming_vector(bf_vector: torch.Tensor, mix: torch.Tensor):
        """Apply the beamforming vector to the mixture. Output (batch, freqs, frames).

        Args:
            bf_vector: shape (batch, mics, freqs)
            mix: shape (batch, mics, freqs, frames).
        """
        return torch.einsum("...mf,...mft->...ft", bf_vector.conj(), mix)


class MvdrBeamformer(_BeamFormer):
    def forward(
        self,
        mix: torch.Tensor,
        target_scm: torch.Tensor,
        noise_scm: torch.Tensor,
    ):
        """Compute and apply MVDR beamformer from the speech and noise SCM matrices

        Args:
            mix (torch.ComplexTensor): shape (batch, mics, freqs, frames)
            target_scm (torch.ComplexTensor): (batch, mics, mics, freqs)
            noise_scm (torch.ComplexTensor): (batch, mics, mics, freqs)

        Returns:
            Filtered mixture. torch.ComplexTensor (batch, freqs, frames)
        """
        # Get Acoustic transfer function (1st PCA of Σss)
        e_val, e_vec = torch.symeig(target_scm.permute(0, 3, 1, 2), eigenvectors=True)
        atf_vect = e_vec[..., -1]  # bfm
        return self.from_atf_vect(mix=mix, atf_vec=atf_vect.transpose(-1, -2), noise_scm=noise_scm)

    def from_atf_vect(
        self,
        mix: torch.Tensor,
        atf_vec: torch.Tensor,
        noise_scm: torch.Tensor,
    ):
        """Compute and apply MVDR beamformer from the ATF vector and noise SCM matrix.

        Args:
            mix (torch.ComplexTensor): shape (batch, mics, freqs, frames)
            atf_vec (torch.ComplexTensor): (batch, mics, freqs)
            noise_scm (torch.ComplexTensor): (batch, mics, mics, freqs)

        Returns:
            Filtered mixture. torch.ComplexTensor (batch, freqs, frames)
        """
        noise_scm_t = noise_scm.permute(0, 3, 1, 2)  # -> bfmm
        atf_vec_t = atf_vec.transpose(-1, -2).unsqueeze(-1)  # -> bfm1

        # numerator, _ = torch.solve(atf_vec_t, noise_scm_t)  # -> bfm1
        numerator = stable_solve(atf_vec_t, noise_scm_t)  # -> bfm1

        denominator = torch.matmul(atf_vec_t.conj().transpose(-1, -2), numerator)  # -> bf11
        bf_vect = (numerator / denominator).squeeze(-1).transpose(-1, -2)  # -> bfm1  -> bmf
        output = self.apply_beamforming_vector(bf_vect, mix=mix)  # -> bft
        return output


class SdwMwfBeamformer(_BeamFormer):
    def __init__(self, mu=1.0):
        super().__init__()
        self.mu = mu

    def forward(
        self, mix: torch.Tensor, target_scm: torch.Tensor, noise_scm: torch.Tensor, ref_mic: int = 0
    ):
        """Compute and apply MVDR beamformer.

        Args:
            mix (torch.ComplexTensor): shape (batch, mics, freqs, frames)
            target_scm (torch.ComplexTensor): (batch, mics, mics, freqs)
            noise_scm (torch.ComplexTensor): (batch, mics, mics, freqs)
            ref_mic (int): reference microphone.

        Returns:
            Filtered mixture. torch.ComplexTensor (batch, freqs, frames)
        """
        noise_scm_t = noise_scm.permute(0, 3, 1, 2)  # -> bfmm
        target_scm_t = target_scm.permute(0, 3, 1, 2)  # -> bfmm

        denominator = target_scm_t + self.mu * noise_scm_t
        bf_vect, _ = torch.solve(target_scm_t, denominator)
        bf_vect = bf_vect[..., ref_mic].transpose(-1, -2)  # -> bfm1  -> bmf
        output = self.apply_beamforming_vector(bf_vect, mix=mix)  # -> bft
        return output


class GEVBeamformer(_BeamFormer):
    def forward(self, mix: torch.Tensor, target_scm: torch.Tensor, noise_scm: torch.Tensor):
        """Compute and apply the GEV beamformer.
        We compute the principal component of noise_scm^-1 @ target_scm by solving the GEV decomposition

        Args:
            mix: shape (batch, mics, freqs, frames)
            target_scm: (batch, mics, mics, freqs)
            noise_scm: (batch, mics, mics, freqs)

        Returns:
            Filtered mixture. torch.ComplexTensor (batch, freqs, frames)
        """
        noise_scm_t = noise_scm.permute(0, 3, 1, 2)
        noise_scm_t = condition_covariance(noise_scm_t, 1e-6)
        e_val, e_vec = generalized_eigenvalue_decomposition(
            target_scm.permute(0, 3, 1, 2), noise_scm_t
        )
        bf_vect = e_vec[..., -1]
        # Normalize
        bf_vect /= torch.norm(bf_vect, dim=-1, keepdim=True)
        bf_vect = bf_vect.squeeze(-1).transpose(-1, -2)  # -> bft
        output = self.apply_beamforming_vector(bf_vect, mix=mix)  # -> bft
        return output


def stable_solve(inp, mat):
    """Return torch.solve in mat is non-singular, else regularize `mat` and torch.solve."""
    try:
        return torch.solve(inp, mat)[0]
    except RuntimeError:
        mat = condition_covariance(mat, 1e-6)
        return torch.solve(inp, mat)[0]


def condition_covariance(x, gamma, dim1=-2, dim2=-1):
    """see https://stt.msu.edu/users/mauryaas/Ashwini_JPEN.pdf (2.3)"""
    # Assume 4d with ...mm
    if dim1 != -2 or dim2 != -1:
        raise NotImplementedError
    scale = gamma * batch_trace(x, dim1=dim1, dim2=dim2)[..., None, None] / x.shape[dim1]
    scaled_eye = torch.eye(x.shape[dim1])[None, None] * scale
    return (x + scaled_eye) / (1 + gamma)


def batch_trace(x, dim1=-2, dim2=-1):
    """Compute the trace along dim1 and dim2 for a any matrix ndim>=2."""
    return torch.diagonal(x, dim1=dim1, dim2=dim2).sum(-1)


def generalized_eigenvalue_decomposition(a, b):
    """Solves the generalized eigenvalue decomposition through cholesky decomposition.
    Returns eigen values and eigen vectors.
    """
    cholesky = torch.cholesky(b)
    inv_cholesky = torch.inverse(cholesky)
    # Compute C matrix L⁻1 A L^-T
    cmat = inv_cholesky @ a @ inv_cholesky.conj().transpose(-1, -2)
    # Performing the eigenvalue decomposition
    e_val, e_vec = torch.symeig(cmat, eigenvectors=True)
    # Collecting the eigenvectors
    e_vec = torch.matmul(inv_cholesky.conj().transpose(-1, -2), e_vec)
    return e_val, e_vec

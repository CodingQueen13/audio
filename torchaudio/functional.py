import numpy as np
import torch

def scale(tensor, factor):
    # type: (Tensor, int) -> Tensor
    if not tensor.dtype.is_floating_point:
        tensor = tensor.to(torch.float32)

    return tensor / factor


def pad_trim(tensor, ch_dim, max_len, len_dim, fill_value):
    # type: (Tensor, int, int, int, float) -> Tensor
    assert tensor.size(ch_dim) < 128, \
        "Too many channels ({}) detected, see channels_first param.".format(tensor.size(ch_dim))
    if max_len > tensor.size(len_dim):
        padding = [max_len - tensor.size(len_dim)
                   if (i % 2 == 1) and (i // 2 != len_dim)
                   else 0
                   for i in range(4)]
        with torch.no_grad():
            tensor = torch.nn.functional.pad(tensor, padding, "constant", fill_value)
    elif max_len < tensor.size(len_dim):
        tensor = tensor.narrow(len_dim, 0, max_len)
    return tensor


def downmix_mono(tensor, ch_dim):
    # type: (Tensor, int) -> Tensor
    if not tensor.dtype.is_floating_point:
        tensor = tensor.to(torch.float32)

    tensor = torch.mean(tensor, ch_dim, True)
    return tensor


def LC2CL(tensor):
    # type: (Tensor) -> Tensor
    return tensor.transpose(0, 1).contiguous()


def spectrogram(sig, pad, window, n_fft, hop, ws, power, normalize):
    # type: (Tensor, int, Tensor, int, int, int, int, bool) -> Tensor
    assert sig.dim() == 2

    if pad > 0:
        with torch.no_grad():
            sig = torch.nn.functional.pad(sig, (pad, pad), "constant")
    window = window.to(sig.device)

    # default values are consistent with librosa.core.spectrum._spectrogram
    spec_f = torch.stft(sig, n_fft, hop, ws,
                        window, center=True,
                        normalized=False, onesided=True,
                        pad_mode='reflect').transpose(1, 2)
    if normalize:
        spec_f /= window.pow(2).sum().sqrt()
    spec_f = spec_f.pow(power).sum(-1)  # get power of "complex" tensor (c, l, n_fft)
    return spec_f


def create_fb_matrix(n_stft, f_min, f_max, n_mels):
    # type: (int, float, float, int) -> Tensor
    """ Create a frequency bin conversion matrix.

    Args:
        n_stft (int): number of filter banks from spectrogram
    """
    def _hertz_to_mel(f):
        # type: (float) -> Tensor
        return 2595. * torch.log10(torch.tensor(1.) + (f / 700.))

    def _mel_to_hertz(mel):
        # type: (Tensor) -> Tensor
        return 700. * (10**(mel / 2595.) - 1.)

    # get stft freq bins
    stft_freqs = torch.linspace(f_min, f_max, n_stft)
    # calculate mel freq bins
    m_min = 0. if f_min == 0 else _hertz_to_mel(f_min)
    m_max = _hertz_to_mel(f_max)
    m_pts = torch.linspace(m_min, m_max, n_mels + 2)
    f_pts = _mel_to_hertz(m_pts)
    # calculate the difference between each mel point and each stft freq point in hertz
    f_diff = f_pts[1:] - f_pts[:-1]  # (n_mels + 1)
    slopes = f_pts.unsqueeze(0) - stft_freqs.unsqueeze(1)  # (n_stft, n_mels + 2)
    # create overlapping triangles
    z = torch.tensor(0.)
    down_slopes = (-1. * slopes[:, :-2]) / f_diff[:-1]  # (n_stft, n_mels)
    up_slopes = slopes[:, 2:] / f_diff[1:]  # (n_stft, n_mels)
    fb = torch.max(z, torch.min(down_slopes, up_slopes))
    return fb


def mel_scale(spec_f, f_min, f_max, n_mels, fb=None):
    # type: (Tensor, float, float, int, Optional[Tensor]) -> Tuple[Tensor, Tensor]
    if fb is None:
        fb = create_fb_matrix(spec_f.size(2), f_min, f_max, n_mels).to(spec_f.device)
    else:
        # need to ensure same device for dot product
        fb = fb.to(spec_f.device)
    spec_m = torch.matmul(spec_f, fb)  # (c, l, n_fft) dot (n_fft, n_mels) -> (c, l, n_mels)
    return fb, spec_m


def spectrogram_to_DB(spec, multiplier, amin, db_multiplier, top_db):
    # type: (Tensor, float, float, float, Optional[float]) -> Tensor
    spec_db = multiplier * torch.log10(torch.clamp(spec, min=amin))
    spec_db -= multiplier * db_multiplier

    if top_db is not None:
        spec_db = torch.max(spec_db, spec_db.new_full((1,), spec_db.max() - top_db))
    return spec_db


def create_dct(n_mfcc, n_mels, norm):
    # type: (int, int, string) -> Tensor
    """
    Creates a DCT transformation matrix with shape (num_mels, num_mfcc),
    normalized depending on norm
    Returns:
        The transformation matrix, to be right-multiplied to row-wise data.
    """
    outdim = n_mfcc
    dim = n_mels
    # http://en.wikipedia.org/wiki/Discrete_cosine_transform#DCT-II
    n = np.arange(dim)
    k = np.arange(outdim)[:, np.newaxis]
    dct = np.cos(np.pi / dim * (n + 0.5) * k)
    if norm == 'ortho':
        dct[0] *= 1.0 / np.sqrt(2)
        dct *= np.sqrt(2.0 / dim)
    else:
        dct *= 2
    return torch.Tensor(dct.T)


def MFCC(sig, mel_spect, log_mels, s2db, dct_mat):
    # type: (Tensor, MelSpectrogram, bool, SpectrogramToDB, Tensor) -> Tensor
    if log_mels:
        log_offset = 1e-6
        mel_spect = torch.log(mel_spect + log_offset)
    else:
        mel_spect = s2db(mel_spect)
    mfcc = torch.matmul(mel_spect, dct_mat.to(mel_spect.device))
    return mfcc


def BLC2CBL(tensor):
    # type: (Tensor) -> Tensor
    return tensor.permute(2, 0, 1).contiguous()


def mu_law_encoding(x, qc):
    # type: (Tensor/ndarray, int) -> Tensor/ndarray
    mu = qc - 1.
    if isinstance(x, np.ndarray):
        x_mu = np.sign(x) * np.log1p(mu * np.abs(x)) / np.log1p(mu)
        x_mu = ((x_mu + 1) / 2 * mu + 0.5).astype(int)
    elif isinstance(x, torch.Tensor):
        if not x.dtype.is_floating_point:
            x = x.to(torch.float)
        mu = torch.tensor(mu, dtype=x.dtype)
        x_mu = torch.sign(x) * torch.log1p(mu *
                                           torch.abs(x)) / torch.log1p(mu)
        x_mu = ((x_mu + 1) / 2 * mu + 0.5).long()
    return x_mu


def mu_law_expanding(x, qc):
    # type: (Tensor/ndarray, int) -> Tensor/ndarray
    mu = qc - 1.
    if isinstance(x_mu, np.ndarray):
        x = ((x_mu) / mu) * 2 - 1.
        x = np.sign(x) * (np.exp(np.abs(x) * np.log1p(mu)) - 1.) / mu
    elif isinstance(x_mu, torch.Tensor):
        if not x_mu.dtype.is_floating_point:
            x_mu = x_mu.to(torch.float)
        mu = torch.tensor(mu, dtype=x_mu.dtype)
        x = ((x_mu) / mu) * 2 - 1.
        x = torch.sign(x) * (torch.exp(torch.abs(x) * torch.log1p(mu)) - 1.) / mu
    return x

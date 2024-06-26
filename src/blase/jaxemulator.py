r"""
emulator
--------------

Precomputed synthetic spectral models are awesome but imperfect and rigid.  Here we clone the most prominent spectral lines and continuum appearance of synthetic spectral models to turn them into tunable, flexible, semi-empirical models.  We can ultimately learn the properties of the pre-computed models with a neural network training loop, and then transfer those weights to real data, where a second transfer-learning training step can take place. The spectrum has :math:`N_{\rm pix} \sim 300,000` pixels and :math:`N_{\rm lines} \sim 5000` spectral lines.  The number of lines is set by the `prominence=` kwarg: lower produces more lines and higher (up to about 0.3) produces fewer lines.  
"""
from dataclasses import dataclass
import math
import jax
import jax.numpy as jnp
import numpy as np
from scipy.signal import find_peaks, peak_prominences, peak_widths
from tqdm import trange

from exojax.spec import voigt, vvoigt

# jax.config.update("jax_enable_x64", True)


class SparseLinearEmulator(object):
    r"""
    A sparse implementation of the LinearEmulator

    Parameters
    ----------
    wl_native : float vector
        The input wavelength at native sampling
    flux_native : float vector
        The continuum-flattened flux at native sampling
    prominence : int
        The threshold for detecting lines
    wing_cut_pixels : int
        The number of pixels centered on the line center to evaluate in the
        sparse implementation, default: 1000 pixels
    init_state_dict : dict
        A dictionary of model parameters to initialize the model with
    """

    def __init__(
        self,
        wl_native,
        flux_native=None,
        prominence=None,
        wing_cut_pixels=None,
        init_state_dict=None,
    ):
        # Read in the synthetic spectra at native resolution
        self.wl_native = jnp.array(wl_native)
        self.wl_min = wl_native.min()
        self.wl_max = wl_native.max()
        self.n_pix = len(wl_native)

        ## Set up "active area", where the region-of-interest is:
        ## Restrict the lines to the active region plus 30 A buffer region
        ## These are hardcoded, and care should be taken if changing them
        line_buffer = 30  # Angstroms
        active_buffer = 60  # Angstroms

        active_lower, active_upper = (
            self.wl_min + active_buffer,
            self.wl_max - active_buffer,
        )
        active_mask = (wl_native > active_lower) & (wl_native < active_upper)
        self.active_mask = jnp.array(active_mask)

        self.wl_active = self.wl_native[active_mask]

        if flux_native is not None:
            self.flux_native = jnp.array(flux_native)
            self.flux_active = self.flux_native[active_mask]
        else:
            self.flux_native = None
            self.flux_active = None

        # Set up line threshold, where lines are computed outside the active area
        line_threshold_lower, line_threshold_upper = (
            self.wl_min + line_buffer,
            self.wl_max - line_buffer,
        )

        if init_state_dict is not None:
            if prominence is not None:
                print(
                    "You have entered both an initial state dict and a prominence kwarg.  Discarding prominence kwarg in favor of state dict."
                )
            lam_centers = init_state_dict["lam_centers"]
            log_amps = init_state_dict["amplitudes"]
            log_sigma_widths = init_state_dict["sigma_widths"]
            log_gamma_widths = init_state_dict["gamma_widths"]

        elif init_state_dict is None and self.flux_native is not None:
            if prominence is None:
                prominence = 0.03
            (
                lam_centers,
                amplitudes,
                widths_angstroms,
            ) = self.detect_lines(
                self.wl_native, self.flux_native, prominence=prominence
            )

            # Experimentally determined scale factor tweaks
            amp_tweak = 0.14
            sigma_width_tweak = 1.28
            gamma_width_tweak = 1.52

            mask = (lam_centers > line_threshold_lower) & (
                lam_centers < line_threshold_upper
            )
            lam_centers = lam_centers[mask]
            log_amps = jnp.log(amplitudes[mask] * amp_tweak)
            log_sigma_widths = jnp.log(
                widths_angstroms[mask] / math.sqrt(2) * sigma_width_tweak
            )
            log_gamma_widths = jnp.log(
                widths_angstroms[mask] / math.sqrt(2) * gamma_width_tweak
            )
        elif init_state_dict is None and self.flux_native is None:
            raise ValueError(
                "Either flux_native or init_state_dict must be provided to specify the spectral lines"
            )

        # Fix the wavelength centers as gospel for now.
        self.lam_centers = jnp.array(lam_centers)
        self.amplitudes = jnp.array(log_amps)
        self.sigma_widths = jnp.array(log_sigma_widths)
        self.gamma_widths = jnp.array(log_gamma_widths)

        self.n_lines = len(lam_centers)

        self.a_coeff = jnp.array(1.0)
        self.b_coeff = jnp.array(0.0)
        self.c_coeff = jnp.array(0.0)

        self.wl_normed = (self.wl_native - 10_500.0) / 2500.0

        if self.flux_native is not None:
            self.target = jnp.array(self.flux_active)
        else:
            self.target = None

        ## Define the wing cut
        # Currently defined in *pixels*
        if wing_cut_pixels is None:
            wing_cut_pixels = 1000
        else:
            wing_cut_pixels = int(wing_cut_pixels)

        lines = self.lam_centers
        wl_native = self.wl_native
        print("Initializing a sparse model with {:} spectral lines".format(len(lines)))

        # Find the index position of each spectral line
        center_indices = np.searchsorted(wl_native, lines)

        # From that, determine the beginning and ending indices
        zero_indices = center_indices - (wing_cut_pixels // 2)
        too_low = zero_indices < 0

        ## The next lines are a JAX workaround for item assignment:
        # zero_indices[too_low] = 0 # can't do this in JAX
        zero_indices = zero_indices.at[too_low].set(0)

        end_indices = zero_indices + wing_cut_pixels
        too_high = end_indices > self.n_pix

        ## The next lines are a JAX workaround for item assignment:
        # zero_indices[too_low] = 0 # can't do this in JAX
        zero_indices = zero_indices.at[too_high].set(self.n_pix - wing_cut_pixels - 1)
        end_indices = end_indices.at[too_high].set(self.n_pix - 1)

        # Make a 2D array of the indices
        indices_2D = np.linspace(
            zero_indices, end_indices, num=wing_cut_pixels, endpoint=True
        )

        self.indices_2D = jnp.array(indices_2D.T, dtype=jnp.int32)
        self.indices_1D = self.indices_2D.reshape(-1)
        self.indices = np.expand_dims(self.indices_1D, axis=0)

        self.wl_2D = self.wl_native[self.indices_2D]
        self.wl_1D = self.wl_2D.reshape(-1)
        self.active_mask = self.active_mask
        self.radial_velocity = jnp.array(0.0)

    def detect_lines(self, wl_native, flux_native, prominence=0.03):
        """Identify the spectral lines in the native model

        Parameters
        ----------
        wl_native : torch.tensor
            The 1D vector of native model wavelengths (Angstroms)
        flux_native: torch.tensor
            The 1D vector of continuum-flattened model fluxes

        Returns
        -------
        tuple of tensors
            The wavelength centers, prominences, and widths for all ID'ed
            spectral lines
        """
        peaks, _ = find_peaks(-flux_native, distance=4, prominence=prominence)
        prominence_data = peak_prominences(-flux_native, peaks)
        width_data = peak_widths(-flux_native, peaks, prominence_data=prominence_data)
        lam_centers = wl_native[peaks]
        prominences = jnp.array(prominence_data[0])
        widths = width_data[0]
        d_lam = np.diff(wl_native)[peaks]
        # Convert FWHM in pixels to Gaussian sigma in Angstroms
        widths_angs = jnp.array(widths * d_lam / 2.355)

        return (lam_centers, prominences, widths_angs)

    def _lorentzian_line(self, lam_center, width, wavelengths):
        """Return a Lorentzian line, given properties"""
        return 1 / 3.141592654 * width / (width**2 + (wavelengths - lam_center) ** 2)

    def _gaussian_line(self, lam_center, width, wavelengths):
        """Return a normalized Gaussian line, given properties"""
        return (
            1.0
            / (width * 2.5066)
            * jnp.exp(-0.5 * ((wavelengths - lam_center) / width) ** 2)
        )

    def _compute_eta(self, fwhm_L, fwhm):
        """Compute the eta mixture ratio for pseudo-Voigt weighting"""
        f_ratio = fwhm_L / fwhm
        return 1.36603 * f_ratio - 0.47719 * f_ratio**2 + 0.11116 * f_ratio**3

    def _compute_fwhm(self, fwhm_L, fwhm_G):
        """Compute the fwhm for pseudo Voigt using the approximation"""

        return (
            fwhm_G**5
            + 2.69269 * fwhm_G**4 * fwhm_L**1
            + 2.42843 * fwhm_G**3 * fwhm_L**2
            + 4.47163 * fwhm_G**2 * fwhm_L**3
            + 0.07842 * fwhm_G**1 * fwhm_L**4
            + fwhm_L**5
        ) ** (1 / 5)

    def forward(self, ln_amplitudes, ln_sigma_widths, ln_gamma_widths):
        r"""The forward pass of the sparse implementation--- no wavelengths needed!

        Returns
        -------
        torch.tensor
            The 1D generative spectral model destined for backpropagation
        """
        return self.sparse_pseudo_Voigt_model(
            ln_amplitudes, ln_sigma_widths, ln_gamma_widths
        )

    def sparse_pseudo_Voigt_model(
        self, ln_amplitudes, ln_sigma_widths, ln_gamma_widths
    ):
        r"""A sparse pseudo-Voigt model

        The sparse matrix :math:`\hat{F}` is composed of the log flux
        values.  Instead of a dense matrix  :math:`\bar{F}`, the log fluxes
        are stored as trios of coordinate values and fluxes.
        :math:`(i, j, \ln{F_{ji}})`.  The computation proceeds as follows:

        .. math::

            \mathsf{S}_{\rm clone} = \exp{\Big(\sum_{j=1}^{N_{lines}} \ln{F_{ji}} \Big)}

        Returns
        -------
        torch.tensor
            The 1D generative sparse spectral model
        """
        fwhm_G = 2.3548 * jnp.exp(ln_sigma_widths)[:, None]
        fwhm_L = 2.0 * jnp.exp(ln_gamma_widths)[:, None]
        fwhm = self._compute_fwhm(fwhm_L, fwhm_G)
        eta = self._compute_eta(fwhm_L, fwhm)

        rv_shifted_centers = self.lam_centers * (
            1.0 + self.radial_velocity / 299_792.458
        )

        flux_2D = jnp.exp(ln_amplitudes)[:, None] * (
            eta
            * self._lorentzian_line(
                rv_shifted_centers[:, None],
                jnp.exp(ln_gamma_widths)[:, None],
                self.wl_2D,
            )
            + (1 - eta)
            * self._gaussian_line(
                rv_shifted_centers[:, None],
                jnp.exp(ln_sigma_widths)[:, None],
                self.wl_2D,
            )
        )

        # Enforce that you cannot have negative flux or emission lines
        flux_2D = jnp.clip(flux_2D, 1e-6, 1 - 1e-6)

        flux_1D = flux_2D.reshape(-1)
        ln_term = jnp.log(1 - flux_1D)

        ## This operation applies a sparse COALESCE operation:
        # Repeated indices get added together.
        flux_out = jnp.zeros_like(self.flux_native)
        flux_out = flux_out.at[self.indices_1D].add(ln_term)

        return jnp.exp(flux_out)[self.active_mask]


class SparseLinearEmissionEmulator(SparseLinearEmulator):
    """An emission line version of the sparse emulator"""

    def forward(self, ln_amplitudes, ln_sigma_widths, ln_gamma_widths):
        r"""The forward pass of the sparse implementation--- no wavelengths needed!

        Returns
        -------
        torch.tensor
            The 1D generative spectral model destined for backpropagation
        """
        return self.sparse_Voigt_model(ln_amplitudes, ln_sigma_widths, ln_gamma_widths)

    def sparse_Voigt_model(self, ln_amplitudes, ln_sigma_widths, ln_gamma_widths):
        r"""A sparse pseudo-Voigt model

        The sparse matrix :math:`\hat{F}` is composed of the log flux
        values.  Instead of a dense matrix  :math:`\bar{F}`, the log fluxes
        are stored as trios of coordinate values and fluxes.
        :math:`(i, j, \ln{F_{ji}})`.  The computation proceeds as follows:

        .. math::

            \mathsf{S}_{\rm clone} = \exp{\Big(\sum_{j=1}^{N_{lines}} \ln{F_{ji}} \Big)}

        Returns
        -------
        torch.tensor
            The 1D generative sparse spectral model
        """
        # fwhm_G = 2.3548 * jnp.exp(ln_sigma_widths)[:, None]
        # fwhm_L = 2.0 * jnp.exp(ln_gamma_widths)[:, None]
        # fwhm = self._compute_fwhm(fwhm_L, fwhm_G)

        rv_shifted_centers = self.lam_centers * (
            1.0 + self.radial_velocity / 299_792.458
        )

        flux_2D = (
            jnp.exp(ln_amplitudes)[:, None]
            * vvoigt(
                self.wl_2D - rv_shifted_centers[:, None],
                jnp.exp(ln_sigma_widths)[:, None],
                jnp.exp(ln_gamma_widths)[:, None],
            ).squeeze()
        )

        flux_1D = flux_2D.reshape(-1)

        ## This operation applies a sparse COALESCE operation:
        # Repeated indices get added together.
        flux_out = jnp.zeros_like(self.flux_native)
        flux_out = flux_out.at[self.indices_1D].add(flux_1D)

        return flux_out

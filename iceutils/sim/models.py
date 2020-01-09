#-*- coding: utf-8 -*-

import numpy as np
import matplotlib.pyplot as plt
from scipy.interpolate import UnivariateSpline, interp1d
import sys

from .utilities import *

class IceStream:
    
    def __init__(self, profile, calving_force, A, B2=6.0e2, n=3, m=1):

        # Save the profile object
        self.profile = profile

        # Physical parameters
        self.A = A
        self.g = 9.80665
        self.B2 = B2
        self.n = n
        self.m = m
        self.rho_ice = profile.rho_ice
        self.rho_water = profile.rho_water

        # Numerical parameters
        self.boundary_value_scale = 500.0

        # Epsilon value when computing the effective viscosity
        # When grid cell size gets smaller, this should also be smaller to ensure stability
        self.nu_eps = 1.0e-8

        # The forc at the calving front
        self.fs = calving_force

        # Constant array to multiply membrane stress Jacobian
        self.K = 2.0 * profile.D * A**(-1 / n)

        # Pre-allocate arrays for storing PDE array and Jacobian
        self.F = np.zeros(self.profile.N + 2)
        self.J = np.zeros((self.profile.N + 2, self.profile.N))

    def compute_pde_values(self, u, scale=1.0e-2):

        # Cache some parameters to use here
        g, n, m, A = [getattr(self, attr) for attr in ('g', 'n', 'm', 'A')]

        # Cache some variables from the profile
        D, h, alpha = [getattr(self.profile, attr) for attr in ('D', 'h', 'alpha')]

        # Compute gradient of velocity profile
        Du = np.dot(D, u)

        # Dynamic viscosity
        nu = A**(-1 / n) / (np.abs(Du)**((n - 1) / n) + self.nu_eps)

        # Membrane stresses
        membrane = scale * 2.0 * np.dot(D, h * nu * Du)

        # Basal drag
        drag = scale * self.B2 * u

        # Driving stress
        Td = scale * self.rho_ice * g * h * alpha

        # Fill out the PDE vector, including boundary values
        self.F[:-2] = membrane - drag + Td
        self.F[-2] = self.boundary_value_scale * Du[0]
        self.F[-1] = self.boundary_value_scale * (Du[-1] - self.fs)

        # Return a copy (numdifftools may manipulate self.F during Jacobian computation)
        return self.F.copy()

    def compute_jacobian(self, u, scale=1.0e-2):

        # Cache some parameters to use here
        g, n, m = [getattr(self, attr) for attr in ('g', 'n', 'm')]

        # Cache some variables from the profile
        D, h, alpha = [getattr(self.profile, attr) for attr in ('D', 'h', 'alpha')]

        # Compute gradient of velocity profile
        Du = np.dot(D, u)

        # Normalized nu (nu / A)
        nu = 1.0 / (np.abs(Du)**((n - 1) / n) + self.nu_eps)

        # Factor that looks like nu, but used in Jacobian
        nu_hat = (n - 1) / n * Du / (np.abs(Du)**((n + 1) / n) + self.nu_eps)

        # Jacobian related to | du/dx | ** ((1 -n) / n)
        Gp = self.gradient_product(-1.0 * (nu**2) * nu_hat, D)

        # Composite Jacobian related to membrane stresses
        J1 = self.gradient_product(h * Du, Gp)
        J2 = self.gradient_product(h * nu, D)
        J_membrane = scale * np.dot(self.K, J1 + J2)

        # Jacobian for sliding drag
        J_drag = scale * self.B2 * np.eye(u.size)

        # Fill out full Jacobian
        self.J[:-2,:] = J_membrane - J_drag
        self.J[-2,:] = self.boundary_value_scale * D[0,:]
        self.J[-1,:] = self.boundary_value_scale * D[-1,:]

        return self.J

    def compute_numerical_jacobian(self, u, step=1.0e-7):
        """
        Computes finite difference approximation to the Jacobian. Not intended to be used
        for simulation runs since it's quite slow, but it's useful for debugging.
        """
        import numdifftools as nd
        jacfun = nd.Jacobian(self.compute_pde_values, step=step)
        return jacfun(u)

    def gradient_product(self, b, A):
        """
        Returns the equivalent of the product:

        (I .* outer(b, 1s)) * A

        but using the faster einsum operation.
        """
        return np.einsum('ij,i->ij', A, b)


class LateralIceStream:
    
    def __init__(self, profile, calving_force, A, W=3000.0, As=100.0, mu=1.0, n=3, m=3):

        # Save the profile object
        self.profile = profile

        # Physical parameters
        self.W = W                  # FULL width of glacier
        self.A = A
        self.g = 9.80665
        self.As = As
        self.mu = mu
        self.n = n
        self.m = m
        self.rho_ice = profile.rho_ice
        self.rho_water = profile.rho_water

        # Numerical parameters
        self.boundary_value_scale = 500.0

        # Epsilon value when computing the effective viscosity
        # When grid cell size gets smaller, this should also be smaller to ensure stability
        self.nu_eps = 1.0e-8
        self.drag_eps = 1.0e-3

        # The force at the calving front
        self.fs = calving_force

        # Effective flotation height (any height below flotation is set to a small number)
        self.Hf = profile.h - self.rho_water / self.rho_ice * profile.depth
        self.Hf[self.Hf < 0.01] = 0.01

        # Constant array to multiply membrane stress Jacobian
        self.K = 2.0 * profile.D * A**(-1 / n)

        # Pre-allocate arrays for storing PDE array and Jacobian
        self.F = np.zeros(self.profile.N + 2)
        self.J = np.zeros((self.profile.N + 2, self.profile.N))

    def compute_pde_values(self, u, scale=1.0e-2, return_components=False):

        # Cache some parameters to use here
        g, n, m, W, A, As, mu = [
            getattr(self, attr) for attr in 
            ('g', 'n', 'm', 'W', 'A', 'As', 'mu')
        ]

        # Cache some variables from the profile
        D, h, depth, alpha, rho_ice, rho_water = [
            getattr(self.profile, attr) for attr in 
            ('D', 'h', 'depth', 'alpha', 'rho_ice', 'rho_water')
        ]

        # Compute gradient of velocity profile
        Du = np.dot(D, u)

        # Dynamic viscosity
        nu = A**(-1 / n) / (np.abs(Du)**((n - 1) / n) + self.nu_eps)

        # Membrane stresses
        membrane = scale * 2.0 * np.dot(D, h * nu * Du)

        # Basal drag
        absu = np.abs(u)
        usign = np.copysign(np.ones_like(u), u)
        basal_drag = scale * usign * mu * As * (self.Hf * absu)**(1 / m)

        # Lateral drag
        lateral_drag = scale * 2 * usign * h / W * (5 * absu / (A * W))**(1 / n)

        # Driving stress
        Td = scale * self.rho_ice * g * h * alpha

        # At this point, return individual components if requested
        if return_components:
            cdict = {'membrane': membrane, 'basal': basal_drag,
                     'lateral': lateral_drag, 'driving': Td}
            return cdict

        # Combine resistive stresses
        membrane -= (basal_drag + lateral_drag)

        # Fill out the PDE vector, including boundary values
        self.F[:-2] = membrane + Td
        self.F[-2] = self.boundary_value_scale * Du[0]
        self.F[-1] = self.boundary_value_scale * (Du[-1] - self.fs)

        # Return a copy (numdifftools may manipulate self.F during Jacobian computation)
        return self.F.copy()

    def compute_jacobian(self, u, scale=1.0e-2):

        # Cache some parameters to use here
        g, n, m, W, A, As, mu = [
            getattr(self, attr) for attr in 
            ('g', 'n', 'm', 'W', 'A', 'As', 'mu')
        ]

        # Cache some variables from the profile
        D, h, depth, alpha, rho_ice, rho_water = [
            getattr(self.profile, attr) for attr in 
            ('D', 'h', 'depth', 'alpha', 'rho_ice', 'rho_water')
        ]

        # Compute gradient of velocity profile
        Du = np.dot(D, u)

        # Normalized nu (nu / A)
        nu = 1.0 / (np.abs(Du)**((n - 1) / n) + self.nu_eps)

        # Factor that looks like nu, but used in Jacobian
        nu_hat = (n - 1) / n * Du / (np.abs(Du)**((n + 1) / n) + self.nu_eps)

        # Jacobian related to | du/dx | ** ((1 -n) / n)
        Gp = self.gradient_product(-1.0 * (nu**2) * nu_hat, D)

        # Composite Jacobian related to membrane stresses
        J1 = self.gradient_product(h * Du, Gp)
        J2 = self.gradient_product(h * nu, D)
        Jpde = scale * np.dot(self.K, J1 + J2)

        # Jacobian for sliding drag (diagonal)
        absu = np.abs(u)
        usign = np.copysign(np.ones_like(u), u)
        J_basal = (scale * mu * usign * As * (self.Hf * usign) / 
                   m * (self.Hf * absu)**((1 - m) / m))
        
        # Jacobian for lateral drag (diagonal)
        J_lateral = (scale * usign * 10 * h * usign /
                    (n * A * W**2) * (5 * absu / (A * W))**((1 - n) / n))

        # Subtract drag terms from diagonal of membrane stress Jacobian
        N = u.size
        Jpde[range(N), range(N)] -= J_basal
        Jpde[range(N), range(N)] -= J_lateral

        # Fill out full Jacobian
        self.J[:-2,:] = Jpde
        self.J[-2,:] = self.boundary_value_scale * D[0,:]
        self.J[-1,:] = self.boundary_value_scale * D[-1,:]

        return self.J

    def compute_numerical_jacobian(self, u, step=1.0e-7):
        """
        Computes finite difference approximation to the Jacobian. Not intended to be used
        for simulation runs since it's quite slow, but it's useful for debugging.
        """
        import numdifftools as nd
        jacfun = nd.Jacobian(self.compute_pde_values, step=step)
        return jacfun(u)

    def gradient_product(self, b, A):
        """
        Returns the equivalent of the product:

        (I .* outer(b, 1s)) * A

        but using the faster einsum operation.
        """
        return np.einsum('ij,i->ij', A, b)


# end of file

"""
Definition of Fluid, IncompressibleFlow as well as fluid-related functions.
"""
from functools import partial

import numpy as np

from phi import math, struct, field
from phi.field import mask, AngularVelocity, CenteredGrid, Grid
from phi.geom import union, GridCell
from . import advect
from .domain import Domain, DomainState
from .effect import Gravity, effect_applied, gravity_tensor
from .material import Material
from .physics import Physics, StateDependency
from ..math._helper import _multi_roll


def divergence_free(velocity: Grid, domain: Domain, obstacles=(), relative_tolerance: float = 1e-5, absolute_tolerance: float = 0.0, max_iterations: int = 1000, return_info=False, gradient='implicit'):
    """
Projects the given velocity field by solving for and subtracting the pressure.
    :param return_info: if True, returns a dict holding information about the solve as a second object
    :param velocity: StaggeredGrid
    :param domain: Domain matching the velocity field, used for boundary conditions
    :param obstacles: list of Obstacles
    :return: divergence-free velocity as StaggeredGrid
    """
    obstacle_mask = mask(union([obstacle.geometry for obstacle in obstacles]))
    active_mask = 1 - obstacle_mask.sample_at(GridCell(velocity.resolution, velocity.box).center)
    active_mask = CenteredGrid(active_mask, velocity.box, math.extrapolation.ZERO)
    active_extrapolation = math.extrapolation.PERIODIC if domain.boundaries == math.extrapolation.PERIODIC else math.extrapolation.ZERO
    accessible_mask = CenteredGrid(active_mask.data, active_mask.box, Material.accessible_extrapolation_mode(domain.boundaries))
    # --- Boundary Conditions---
    hard_bcs = field.stagger(accessible_mask, math.minimum)
    velocity *= hard_bcs
    for obstacle in obstacles:
        if not obstacle.is_stationary:
            obs_mask = mask(obstacle.geometry)
            angular_velocity = AngularVelocity(location=obstacle.geometry.center, strength=obstacle.angular_velocity, falloff=None)
            velocity = ((1 - obs_mask) * velocity + obs_mask * (angular_velocity + obstacle.velocity)).at(velocity)
    # --- Pressure solve ---
    divergence_field = field.divergence(velocity)
    pressure_guess = domain.grid(0)
    laplace_fun = partial(masked_laplace, active=active_mask, accessible=accessible_mask)
    converged, pressure, iterations = field.conjugate_gradient(laplace_fun, divergence_field, pressure_guess, relative_tolerance, absolute_tolerance, max_iterations, gradient)
    if not math.all(converged):
        raise ValueError('pressure solve did not converge')
    gradp = field.staggered_gradient(pressure)
    gradp *= hard_bcs
    velocity -= gradp
    return velocity if not return_info else (velocity, {'pressure': pressure, 'iterations': iterations, 'divergence': divergence_field})


def masked_laplace(pressure: CenteredGrid, active: CenteredGrid, accessible: CenteredGrid) -> CenteredGrid:
    """
    Compute the laplace of a pressure-like field in the presence of obstacles.

    :param pressure: input field
    :param active: Scalar field encoding active cells as ones and inactive (open/obstacle) as zero.
        Active cells are those for which physical constants_dict such as pressure or velocity are calculated.
    :param accessible: Scalar field encoding cells that are accessible, i.e. not solid, as ones and obstacles as zero.
    :return: laplace of pressure given the boundary conditions
    """
    extended_active_mask = field.pad(active, 1).data
    extended_fluid_mask = field.pad(accessible, 1).data
    extended_pressure = field.pad(pressure, 1).data
    active_pressure = extended_active_mask * extended_pressure
    by_dim = []
    for dim in pressure.shape.spatial.names:
        lower_active_pressure, upper_active_pressure = _multi_roll(active_pressure, dim, (-1, 1), diminish_others=(1, 1), names=pressure.shape.spatial.names)
        lower_accessible, upper_accessible = _multi_roll(extended_fluid_mask, dim, (-1, 1), diminish_others=(1, 1), names=pressure.shape.spatial.names)
        upper = upper_active_pressure * active.data
        lower = lower_active_pressure * active.data
        center = (- lower_accessible - upper_accessible) * pressure.data
        by_dim.append(center + upper + lower)
    data = math.sum(by_dim, axis=0)
    return CenteredGrid(data, pressure.box, pressure.extrapolation.gradient())


@struct.definition()
class Fluid(DomainState):
    """
    A Fluid state consists of a density field (centered grid) and a velocity field (staggered grid).
    """

    def __init__(self, domain, density=0.0, velocity=0.0, buoyancy_factor=0.0, tags=('fluid', 'velocityfield', 'velocity'), name='fluid', **kwargs):
        DomainState.__init__(self, **struct.kwargs(locals()))

    def default_physics(self):
        return IncompressibleFlow()

    @struct.variable(default=0, dependencies=DomainState.domain)
    def density(self, density):
        """
The marker density is stored in a CenteredGrid with dimensions matching the domain.
It describes the number of particles per physical volume.
        """
        return self.centered_grid('density', density)

    @struct.variable(default=0, dependencies=DomainState.domain)
    def velocity(self, velocity):
        """
The velocity is stored in a StaggeredGrid with dimensions matching the domain.
        """
        return self.staggered_grid('velocity', velocity)

    @struct.constant(default=0.0)
    def buoyancy_factor(self, fac):
        """
The default fluid physics can apply Boussinesq buoyancy as an upward force, proportional to the density.
This force is scaled with the buoyancy_factor (float).
        """
        return fac

    @struct.variable(default={}, holds_data=False)
    def solve_info(self, solve_info):
        return dict(solve_info)

    def __repr__(self):
        return "Fluid[density: %s, velocity: %s]" % (self.density, self.velocity)


class IncompressibleFlow(Physics):
    """
Physics modelling the incompressible Navier-Stokes equations.
Supports buoyancy proportional to the marker density.
Supports obstacles, density effects, velocity effects, global gravity.
    """

    def __init__(self, make_input_divfree=False, make_output_divfree=True, conserve_density=True):
        Physics.__init__(self, [StateDependency('obstacles', 'obstacle', blocking=True),
                                StateDependency('gravity', 'gravity', single_state=True),
                                StateDependency('density_effects', 'density_effect', blocking=True),
                                StateDependency('velocity_effects', 'velocity_effect', blocking=True)])
        self.make_input_divfree = make_input_divfree
        self.make_output_divfree = make_output_divfree
        self.conserve_density = conserve_density

    def step(self, fluid, dt=1.0, obstacles=(), gravity=Gravity(), density_effects=(), velocity_effects=()):
        # pylint: disable-msg = arguments-differ
        gravity = gravity_tensor(gravity, fluid.rank)
        velocity = fluid.velocity
        density = fluid.density
        if self.make_input_divfree:
            velocity, solve_info = divergence_free(velocity, fluid.domain, obstacles, return_info=True)
        # --- Advection ---
        density = advect.semi_lagrangian(density, velocity, dt=dt)
        velocity = advected_velocity = advect.semi_lagrangian(velocity, velocity, dt=dt)
        if self.conserve_density and np.all(Material.solid(fluid.domain.boundaries)):
            density = density.normalized(fluid.density)
        # --- Effects ---
        for effect in density_effects:
            density = effect_applied(effect, density, dt)
        for effect in velocity_effects:
            velocity = effect_applied(effect, velocity, dt)
        velocity += (density * -gravity * fluid.buoyancy_factor * dt).at(velocity)
        divergent_velocity = velocity
        # --- Pressure solve ---
        if self.make_output_divfree:
            velocity, solve_info = divergence_free(velocity, fluid.domain, obstacles, return_info=True)
        solve_info['advected_velocity'] = advected_velocity
        solve_info['divergent_velocity'] = divergent_velocity
        return fluid.copied_with(density=density, velocity=velocity, age=fluid.age + dt, solve_info=solve_info)



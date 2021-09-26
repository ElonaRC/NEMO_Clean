# Copyright (C) 2017, 2019 Ben Elliston
#
# This file is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.

"""The core of the simulation engine."""

from math import isclose

import numpy as np
import pandas as pd

from nemo import regions


def _sim(context, date_range):
    # reset generator internal state
    for gen in context.generators:
        gen.reset()
        # every generator must be assigned to a polygon
        assert gen.polygon is not None, \
            'every generator must be assigned a polygon'

    generation = np.zeros((len(date_range), len(context.generators)))
    spill = np.zeros((len(date_range), len(context.generators)))

    # Extract generators in the regions of interest.
    gens = [g for g in context.generators if g.region() in context.regions]

    # Zero out polygon demands we don't care about.
    for rgn in [r for r in regions.All if r not in context.regions]:
        for poly in rgn.polygons:
            context.demand[poly - 1] = 0

    # We are free to scribble all over demand_copy. Use ndarray for speed.
    demand_copy = context.demand.copy().values
    residual_demand = demand_copy.sum(axis=1)

    for hour, date in enumerate(date_range):
        hour_demand = demand_copy[hour]
        residual_hour_demand = residual_demand[hour]

        if context.verbose:
            print('STEP:', date)
            print('DEMAND:', {a: round(b, 2) for a, b in
                              enumerate(hour_demand)})

        _dispatch(context, hour, residual_hour_demand, gens, generation, spill)

        if context.verbose:
            print('ENDSTEP:', date)

    # Change the numpy arrays to dataframes for human consumption
    context.generation = pd.DataFrame(index=date_range, data=generation)
    context.spill = pd.DataFrame(index=date_range, data=spill)


def _store_spills(context, hour, gen, generators, spl):
    """
    Store spills from a generator into any storage.

    >>> ctx = type('context', (), {'verbose': 0})
    >>> from nemo import generators
    >>> g = generators.Hydro(1, 100)
    >>> h = generators.HydrogenStorage(400)
    >>> e = generators.Electrolyser(h, 1, 100, efficiency=1.0)
    >>> _store_spills(ctx, 0, g, [e], 50)
    0.0
    >>> h.storage
    250.0
    """
    for other in list(g for g in generators if g.storage_p):
        stored = other.store(hour, spl)
        spl -= stored
        if spl < 0 and isclose(spl, 0, abs_tol=1e-6):
            spl = 0
        assert spl >= 0

        # energy stored <= energy transferred, according to store's RTE
        if context.verbose:
            # show the energy transferred, not stored
            print('STORE:', gen.polygon, '->', end=' ')
            print(other.polygon, '(%.1f)' % stored)
    return spl


def _dispatch(context, hour, residual_hour_demand, gens, generation, spill):
    """Dispatch power from each generator in merit (list) order."""
    # async_demand is the maximum amount of the demand in this
    # hour that can be met from non-synchronous
    # generation. Non-synchronous generation in excess of this
    # value must be spilled.
    async_demand = residual_hour_demand * context.nsp_limit

    for gidx, generator in enumerate(gens):
        if not generator.synchronous_p and async_demand < residual_hour_demand:
            gen, spl = generator.step(hour, async_demand)
        else:
            gen, spl = generator.step(hour, residual_hour_demand)
        assert gen < residual_hour_demand or \
            isclose(gen, residual_hour_demand), \
            "generation (%.4f) > demand " % gen + \
            "(%.4f) for %s" % (residual_hour_demand, generator)
        generation[hour, gidx] = gen

        if not generator.synchronous_p:
            async_demand -= gen
            assert async_demand > 0 or isclose(async_demand, 0, abs_tol=1e-6)
            async_demand = max(0, async_demand)

        residual_hour_demand -= gen
        assert residual_hour_demand > 0 or \
            isclose(residual_hour_demand, 0, abs_tol=1e-6)
        residual_hour_demand = max(0, residual_hour_demand)

        if context.verbose:
            print('GENERATOR: %s,' % generator,
                  'generation: %.1f' % generation[hour, gidx],
                  'spill: %.1f' % spl,
                  'residual-demand: %.1f' % residual_hour_demand,
                  'async-demand: %.1f' % async_demand)

        if spl > 0:
            spill[hour, gidx] = \
                _store_spills(context, hour, generator, gens, spl)


def run(context, starthour=None, endhour=None):
    """Run the simulation.

    >>> from nemo import Context
    >>> c = Context()
    >>> c.regions = None
    >>> run(c)
    Traceback (most recent call last):
    ValueError: regions is not a list
    """
    if not isinstance(context.regions, list):
        raise ValueError('regions is not a list')

    if starthour is None:
        starthour = context.demand.index.min()
    if endhour is None:
        endhour = context.demand.index.max()
    date_range = pd.date_range(starthour, endhour, freq='H')

    _sim(context, date_range)

    # Calculate unserved energy.
    agg_demand = context.demand.sum(axis=1)
    agg_generation = context.generation.sum(axis=1)
    unserved = agg_demand - agg_generation
    # Ignore unserved events very close to 0 (rounding errors)
    context.unserved = unserved[~np.isclose(unserved, 0)]

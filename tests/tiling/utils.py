import sys
import os
import argparse
import math

import mpi4py

from pyop2.mpi import MPI
from firedrake.petsc import PETSc


def parser():
    """
    Used by ``explosive_source.py`` to parse user input.
    """

    p = argparse.ArgumentParser(description='Run Seigen with loop tiling')
    # Tiling
    p.add_argument('-n', '--num-unroll', type=int, help='time loop unroll factor',
                   default=1, dest='num_unroll')
    p.add_argument('-z', '--explicit-mode', type=int, help='split chain as [(f, l, ts), ...]',
                   default=0, dest='explicit_mode')
    p.add_argument('-t', '--tile-size', type=int, help='initial average tile size',
                   default=5, dest='tile_size')
    p.add_argument('-e', '--fusion-mode', help='(soft, hard, tile, only_tile)',
                   default='tile', dest='fusion_mode')
    p.add_argument('-p', '--part-mode', help='partition modes (chunk, metis)',
                   default='chunk', dest='part_mode')
    p.add_argument('-x', '--extra-halo', type=int, help='add extra halo layer',
                   default=0, dest='extra_halo')
    p.add_argument('--verbose', help='print additional information', action='store_true')
    p.add_argument('--log', help='output inspector to a file', action='store_true')
    p.add_argument('--glb-maps', help='use global maps', action='store_true')
    p.add_argument('--prefetch', help='use software prefetching', action='store_true')
    p.add_argument('--coloring', help='set iteration set coloring (default, rand, omp)',
                   default='default', dest='coloring')
    # Correctness
    p.add_argument('--debug', help='execute in debug mode', action='store_true')
    p.add_argument('--profile', help='enable Python-level profiling', action='store_true')
    p.add_argument('--check', help='check the numerical results', action='store_true')
    # Simulation
    p.add_argument('-y', '--poly-order', type=int, help='the method\'s order in space',
                   default=1, dest='poly_order')
    p.add_argument('-f', '--mesh-file', help='use a specific mesh file', dest='mesh_file')
    p.add_argument('-m', '--mesh-size', help='rectangular mesh, format: (Lx, Ly)',
                   default=(None, None), dest='mesh_size')
    p.add_argument('-ms', '--mesh-spacing', type=float, help='set the mesh spacing',
                   default=2.5, dest='ms')
    p.add_argument('-o', '--output', type=int, dest='output',
                   help='timesteps between two solution field writes', default=1)
    p.add_argument('-cn', '--courant-number', type=float,
                   help='set the courant number', default=0.05, dest='cn')
    p.add_argument('--time-max', type=float, help='set the simulation duration', default=2.5)
    p.add_argument('--timesteps-max', type=int, help='set max number of timesteps', default=0)
    p.add_argument('--no-tofile', help='do not store timings to file',
                   dest='tofile', action='store_false')
    # Kernel optimization
    p.add_argument('--coffee-opt', help='kernel optimization level', dest='coffee_opt',
                   choices=['O0', 'O1', 'O2', 'O3'], action='store', default='O2')
    # Forward to petsc4py
    p.add_argument('-log_view', help='tell PETSc to generate a log', action='store_true')
    # Set default values
    p.set_defaults(verbose=False, log=False, glb_maps=False, prefetch=False, debug=False,
                   profile=False, check=False, tofile=True)

    return p.parse_args()


def output_time(start, end, **kwargs):
    """
    Used by ``explosive_source.py`` at the end of a run to record to file
    useful information.
    """
    verbose = kwargs.get('verbose', False)
    tofile = kwargs.get('tofile', False)
    meshid = kwargs.get('meshid', 'default_mesh')
    ntimesteps = kwargs.get('ntimesteps', 0)
    nloops = kwargs.get('nloops', 0)
    tile_size = kwargs.get('tile_size', 0)
    partitioning = kwargs.get('partitioning', 'chunk')
    extra_halo = 'yes' if kwargs.get('extra_halo', False) else 'no'
    explicit_mode = kwargs.get('explicit_mode', None)
    glb_maps = 'yes' if kwargs.get('glb_maps', False) else 'no'
    poly_order = kwargs.get('poly_order', -1)
    domain = kwargs.get('domain', 'default_domain')
    coloring = kwargs.get('coloring', 'default')
    prefetch = 'yes' if kwargs.get('prefetch', False) else 'no'
    function_spaces = kwargs.get('function_spaces', [])
    backend = os.environ.get("SLOPE_BACKEND", "SEQUENTIAL")

    name = os.path.splitext(os.path.basename(sys.argv[0]))[0]  # Cut away the extension

    avg = lambda v: (sum(v) / len(v)) if v else 0.0

    # Where do I store the output ?
    output_dir = os.getcwd()

    # Find number of processes, and number of threads per process
    rank = MPI.COMM_WORLD.rank
    num_procs = MPI.COMM_WORLD.size
    num_threads = int(os.environ.get("OMP_NUM_THREADS", 1)) if backend == 'OMP' else 1

    # What execution mode is this?
    if num_procs == 1 and num_threads == 1:
        versions = ['sequential', 'openmp', 'mpi', 'mpi_openmp']
    elif num_procs == 1 and num_threads > 1:
        versions = ['openmp']
    elif num_procs > 1 and num_threads == 1:
        versions = ['mpi']
    else:
        versions = ['mpi_openmp']

    # Determine the total execution time (Python + kernel execution + MPI cost
    if rank in range(1, num_procs):
        MPI.COMM_WORLD.isend([start, end], dest=0)
    elif rank == 0:
        starts, ends = [0]*num_procs, [0]*num_procs
        starts[0], ends[0] = start, end
        for i in range(1, num_procs):
            starts[i], ends[i] = MPI.COMM_WORLD.recv(source=i)
        min_start, max_end = min(starts), max(ends)
        tot = round(max_end - min_start, 3)
        print "Time stepping: ", tot, "s"

    # Exit if user doesn't want timings to be recorded
    if not tofile:
        return

    # Determine (on rank 0):
    # ACT - Average Compute Time, pure kernel execution -
    # ACCT - Average Compute and Communication Time (ACS + MPI cost)
    # For this, first dump PETSc performance log info to temporary file as
    # currently there's no other clean way of accessing the times in petsc4py
    logfile = os.path.join(output_dir, 'seigenlog.py')
    vwr = PETSc.Viewer().createASCII(logfile)
    vwr.pushFormat(PETSc.Viewer().Format().ASCII_INFO_DETAIL)
    PETSc.Log().view(vwr)
    PETSc.Options().delValue('log_view')
    if rank == 0:
        with open(logfile, 'r') as f:
            content = f.read()
        exec(content) in globals(), locals()
        compute_times = [Stages['Main Stage']['ParLoopCKernel'][i]['time'] for i in range(num_procs)]
        mpi_times = [Stages['Main Stage']['ParLoopHaloEnd'][i]['time'] for i in range(num_procs)]
        ACT = round(avg(compute_times), 3)
        AMT = round(avg(mpi_times), 3)
        ACCT = ACT + AMT
        print "Average Compute Time: ", ACT, "s"
        print "Average Compute and Communication Time: ", ACCT, "s"

    # Determine if a multi-node execution
    platform = os.environ.get('NODENAME', 'unknown')

    # Adjust /tile_size/ and /version/ based on the problem that was actually run
    assert nloops >= 0
    if nloops == 0:
        tile_size = 0
        mode = "untiled"
    elif explicit_mode:
        mode = "fs%d" % explicit_mode
    else:
        mode = "loops%d" % nloops

    ### Print timings to file ###

    def fix(values):
        new_values = []
        for v in values:
            try:
                new_v = int(v)
            except ValueError:
                try:
                    new_v = float(v)
                except ValueError:
                    new_v = v.strip()
            if new_v != '':
                new_values.append(new_v)
        return tuple(new_values)

    if rank == 0:
        for version in versions:
            timefile = os.path.join(output_dir, "times", name, "poly_%d" % poly_order, domain,
                                    meshid, version, platform, "np%d_nt%d.txt" % (num_procs, num_threads))
            # Create directory and file (if not exist)
            if not os.path.exists(os.path.dirname(timefile)):
                os.makedirs(os.path.dirname(timefile))
            if not os.path.exists(timefile):
                open(timefile, 'a').close()
            # Read the old content, add the new time value, order
            # everything based on <execution time, #loops tiled>, write
            # back to the file (overwriting existing content)
            with open(timefile, "r+") as f:
                lines = [line.split('|') for line in f if line.strip()][2:]
                lines = [fix(i) for i in lines]
                lines += [(tot, ACT, ACCT, ntimesteps, mode, tile_size, partitioning,
                           extra_halo, glb_maps, coloring, prefetch)]
                lines.sort(key=lambda x: x[0])
                template = "| " + "%9s | " * 11
                prepend = template % ('time', 'ACT', 'ACCT', 'timesteps', 'mode', 'tilesize',
                                      'partmode', 'extrahalo', 'glbmaps', 'coloring', 'prefetch')
                lines = "\n".join([prepend, '-'*133] + [template % i for i in lines]) + "\n"
                f.seek(0)
                f.write(lines)
                f.truncate()

    ### Print DoFs summary to file ###

    dofsfile = os.path.join(output_dir, "times", name, "dofs_summary.txt")
    if rank == 0 and not os.path.exists(dofsfile):
        with open(dofsfile, 'a') as f:
            f.write("poly:numprocs:[fs1_dofs;fs2_dofs;...]\n")
    tot_dofs = [MPI.COMM_WORLD.allreduce(fs.dof_count, op=mpi4py.MPI.SUM) for fs in function_spaces]
    if rank == 0:
        with open(dofsfile, "a") as f:
            f.write("%d:%d:%s\n" % (poly_order, num_procs, ';'.join([str(i) for i in tot_dofs])))

    ### Print summary output to screen ###

    if rank == 0 and verbose:
        for i in range(num_procs):
            fs_info = ", ".join(["%s=%d" % (fs.name, fs.dof_count) for fs in function_spaces])
            tot_time = compute_times[i] + mpi_times[i]
            offC = (ends[i] - starts[i]) - tot_time
            offCperc = (offC / (ends[i] - starts[i]))*100
            mpiPerc = (mpi_times[i] / (ends[i] - starts[i]))*100
            print "Rank %d: comp=%.2fs, mpi=%.2fs -- tot=%.2fs (py=%.2fs, %.2f%%; mpi_oh=%.2f%%; fs=[%s])" % \
                (i, compute_times[i], mpi_times[i], tot_time, offC, offCperc, mpiPerc, fs_info)
        sys.stdout.flush()
    MPI.COMM_WORLD.barrier()

    # Clean up
    if rank == 0:
        os.remove(logfile)


def calculate_sdepth(num_solves, num_unroll, extra_halo):
    """The sdepth is calculated through the following formula:

        sdepth = 1 if sequential else 1 + num_solves*num_unroll + extra_halo

    Where:

    :arg num_solves: number of solves per loop chain iteration
    :arg num_unroll: unroll factor for the loop chain
    :arg extra_halo: to expose the nonexec region to the tiling engine
    """
    if MPI.COMM_WORLD.size > 1 and num_unroll > 0:
        return (int(math.ceil(num_solves/2.0)) or 1) + extra_halo
    else:
        return 1


class FusionSchemes(object):

    """
    The fusion schemes attempted in Seigen.

    The format of a fusion scheme is: ::

         (num_solves, [(first_loop_index, last_loop_index, tile_size_multiplier), ...])
    """

    modes = {
        2: (1, [(2, 4, 4), (6, 8, 4), (9, 12, 2), (15, 17, 4), (18, 20, 4), (22, 25, 1)]),
        3: (1, [(1, 3, 4), (4, 7, 4), (8, 12, 2), (13, 16, 4), (17, 19, 4), (20, 25, 1)]),
        4: (2, [(1, 7, 1), (8, 16, 1), (17, 25, 1)]),
        5: (4, [(1, 12, 1), (13, 25, 1)]),
        6: (8, [(1, 25, 1)])
    }

    @staticmethod
    def get(mode, part_mode, tile_size):
        num_solves, mode = FusionSchemes.modes[mode]
        mode = [(i, j, tile_size*(k if part_mode == 'chunk' else 1)) for i, j, k in mode]
        return num_solves, mode

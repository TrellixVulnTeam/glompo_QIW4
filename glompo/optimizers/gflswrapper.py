# Native Python
from typing import *
import numpy as np
import os
import warnings
from multiprocessing import Event, Queue
from multiprocessing.connection import Connection

# Optsam Package
from optsam.fwrap import ResidualsWrapper
from optsam.codec import VectorCodec, BoxTanh
from optsam.opt_gfls import GFLS
from optsam.driver import driver
from optsam.logger import Logger
from optsam.algo_base import AlgoBase

# This Package
from .baseoptimizer import BaseOptimizer, MinimizeResult
from ..common.namedtuples import IterationResult


__all__ = ("GFLSOptimizer",)


class GFLSOptimizer(BaseOptimizer):

    needscaler = False

    def __init__(
        self,
        opt_id: int = None,
        signal_pipe: Connection = None,
        results_queue: Queue = None,
        pause_flag: Event = None,
        tmax: Optional[int] = None,
        imax: Optional[int] = None,
        fmax: Optional[int] = None,
        verbose: int = 30,
        save_logger: Optional[str] = None,
        gfls_kwargs: Optional[dict] = None,
    ):
        """ Instance of the GFLS optimizer that can be used through GloMPO.

        Parameters
        ----------
        opt_id : int
            Unique ID of the optimizer.
        tmax
            Stopping condition for the wall time in seconds. The optimization will
            stop when the given time has passed after one of the iterations. The
            actual time spent may be a bit longer because an ongoing iteration
            will not be interrupted.
        imax
            Stopping condition for the number of iterations.
        fmax
            Stopping condition for the number of function calls to the wrapper.
            This condition is checked after an iteration has completed. Function
            calls needed for the initialization are not counted.
        verbose
            When zero, no screen output is printed. If non-zero, the integer
            determines the frequency of printing the header of the logger.
        save_logger
            An optional string which if provided saves the output of the logger to the filename given.
        gfls_kwargs
            Arguments passed to the setup of the GFLS class. See opt_gfls.py or documentation.
        """
        super().__init__(opt_id, signal_pipe, results_queue, pause_flag)
        self.tmax = tmax
        self.imax = imax
        self.fmax = fmax
        self.verbose = verbose
        self.save_logger = save_logger
        self.algorithm = GFLS(**gfls_kwargs) if gfls_kwargs else GFLS(tr_max=1)

    # noinspection PyMethodOverriding
    def minimize(
        self,
        function: Callable[[Sequence[float]], float],
        x0: Union[Sequence[float], Type[Logger]],
        bounds: Sequence[Tuple[float, float]],
        callbacks: Sequence[Callable[[Logger, AlgoBase, Union[str, None]], Any]] = None,
    ) -> MinimizeResult:

        """
        Executes the task of minimizing a function with GFLS.

        Parameters
        ----------
        function : Callable[[Sequence[float]], Sequence[float]]
            Function to be minimized.

            NB: GFLS is a unique class of optimizer that requires the function being
            minimized to return a sequence of residual errors between the function values evaluated at a trial set of
            parameters and some reference values. This means it is not generally applicable to all problems.

            function must include an implementation of function.resids() which returns these residuals.

        x0 : Union[Sequence[float], Type[Logger]]
            Initial set of starting parameters or an instance of optsam.Logger with a saved history of at least one
            iteration.
        bounds : Sequence[Tuple[float, float]]
            Sequence of tuples of the form (min, max) which bound the parameters.
        callbacks : Sequence[Callable[[Type[Logger, AlgoBase, Union[str, None]]], Any]]
            A list of functions called after every iteration. GFLS compatible callbacks can be found at the end of this
            file.

            If GFLS is being used through the GloMPO mp_manager calls to send iteration results to the mp_manager and
            check incoming signals from it are automatically added to this list. Only send functionality you want over
            and above this.

            Each callback takes three arguments: ``logger``, ``algorithm`` and ``stopcond``. The ``logger`` is the same
            as the return value of optsam.driver, except that it only contains information of iterations so far.
            The ``algorithm`` is the one given to the driver. ``stopcond`` is the stopping condition after the current
            iteration and is ``None`` when the driver should carry on. The callback returns an updated value for
            ``stopcond``. If the callback has no return value, i.e. equivalent to returning ``None``.
        """

        # noinspection PyUnresolvedReferences
        if not callable(function.__wrapped__.resids):
            raise NotImplementedError("GFLS requires function to include a resids() method.")

        gfls_bounds = []
        for bnd in bounds:
            if bnd[0] == bnd[1]:
                raise ValueError("Min and Max bounds cannot be equal. Rather fix the value and set the variable"
                                 "inactive through the interface.")
            else:
                gfls_bounds.append(BoxTanh(bnd[0], bnd[1]))
        vector_codec = VectorCodec(gfls_bounds)

        if not isinstance(x0, Logger):
            for i, x in enumerate(x0):
                if x < bounds[i][0] or x > bounds[i][1]:
                    raise ValueError("x0 values outside of bounds.")

        if callable(callbacks):
            callbacks = [callbacks]
        if self._results_queue:
            if callbacks:
                callbacks = [self.push_iter_result, self.check_messages, *callbacks]
            else:
                callbacks = [self.push_iter_result, self.check_messages]

        # noinspection PyUnresolvedReferences
        fw = ResidualsWrapper(function.__wrapped__.resids, vector_codec.decode)
        logger = driver(
            fw,
            vector_codec.encode(x0),
            self.algorithm,
            self.tmax,
            self.imax,
            self.fmax,
            self.verbose,
            callbacks
        )
        if self.save_logger:
            if "/" in self.save_logger:
                path, name = tuple(self.save_logger.rsplit("/", 1))
                os.makedirs(path)
            else:
                name = self.save_logger
            logger.save(name)

        cond = logger.aux["stopcond"]
        success = True if any(cond == k for k in ["xtol", "tr_min"]) else False
        fx = logger.get("func_best", -1)
        history = logger.get_tracks("func")[0]
        index = np.where(history == fx)[0][0]
        x = logger.get("pars", index)

        self.message_manager(0)
        result = MinimizeResult()
        result.success = success
        result.x = vector_codec.decode(x)
        result.fx = fx

        return result

    def push_iter_result(self, logger: Logger, algorithm, stopcond: str, *args):
        i = logger.current
        x = logger.get("pars")
        fx = logger.get("func")
        fin = False if stopcond is None else True
        self._results_queue.put(IterationResult(self._opt_id, i, 1, x, fx, fin))

    def check_messages(self, logger: Logger, algorithm, stopcond):
        conds = []
        while self._signal_pipe.poll():
            message = self._signal_pipe.recv()
            if isinstance(message, int):
                conds.append(self._FROM_MANAGER_SIGNAL_DICT[message](logger, algorithm, stopcond))
            elif isinstance(message, tuple):
                conds.append(self._FROM_MANAGER_SIGNAL_DICT[message[0]](logger, algorithm, stopcond, *message[1:]))
            else:
                warnings.warn("Cannot parse message, ignoring", RuntimeWarning)
        if any([cond is not None for cond in conds]):
            mess = ""
            for cond in conds:
                mess += f"{cond} AND "
            mess = mess[:-5]
            return mess

    def save_state(self, logger: Logger, algorithm, stopcond, file_name: str):
        if "/" in file_name:
            path, name = tuple(file_name.rsplit("/", 1))
            os.makedirs(path)
        else:
            name = file_name
        logger.save(name)

    def callstop(self, logger: Logger, *args):
        return "Manager Termination"

    def check_pause_flag(self):
        self._pause_signal.wait()

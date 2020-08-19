""" Contains classes which save log information for GloMPO and its optimizers. """

import os
from math import inf
from typing import Any, Dict, List, Optional, Sequence, Union

import yaml
from glompo.common.helpers import FileNameHandler, LiteralWrapper, literal_presenter

__all__ = ("OptimizerLogger",)


class OptimizerLogger:
    """ Stores progress of GloMPO optimizers. """

    def __init__(self):
        self._storage: Dict[int, _OptimizerLogger] = {}

    def __len__(self):
        return len(self._storage)

    def add_optimizer(self, opt_id: int, class_name: str, time_start: str):
        """ Adds a new optimizer data stream to the log. """
        self._storage[opt_id] = _OptimizerLogger(opt_id, class_name, time_start)

    def put_iteration(self, opt_id: int, i: int, f_call_overall: int, f_call_opt: int, x: Sequence[float], fx: float):
        """ Adds an iteration result to an optimizer data stream. """
        self._storage[opt_id].append(i, f_call_overall, f_call_opt, x, fx)

    def put_metadata(self, opt_id: int, key: str, value: str):
        """ Adds metadata about an optimizer. """
        self._storage[opt_id].update_metadata(key, value)

    def put_message(self, opt_id: int, message: str):
        """ Optimizers can signal special messages to the optimizer during the optimization which can be saved to
            the log.
        """
        self._storage[opt_id].append_message(message)

    def get_history(self, opt_id: int, track: Optional[str] = None) -> Union[List, Dict[int, Dict[str, float]]]:
        """ Returns a list of values for a given optimizer and track or returns the entire dictionary of all tracks
            if None.

            Parameters
            ----------
            opt_id: int
                Unique optimizer identifier.
            track: Optional[str] = None
                If specified returns only one series from the optimizer history. Available options:
                    - 'f_call_overall': The overall number of function evaluations used by all optimizers after each
                        iteration of opt_id,
                    - 'f_call_opt': The number of function evaluations used by opt_id after each of its iterations,
                    - 'fx': The function evaluations after each iteration,
                    - 'i_best': The iteration number at which the best function evaluation was located,
                    - 'fx_best': The best function evaluation value after each iteration,
                    - 'x': The task input values trialed at each iteration.
        """
        extract = []
        if track:
            for item in self._storage[opt_id].history.values():
                extract.append(item[track])
        else:
            extract = self._storage[opt_id].history
        return extract

    def get_metadata(self, opt_id, key: str) -> Any:
        """ Returns metadata of a given optimizer and key. """
        return self._storage[opt_id].metadata[key]

    def save_optimizer(self, name: str, opt_id: Optional[int] = None):
        """ Saves the contents of the logger into yaml files. If an opt_id is provided only that optimizer will be
            saved using the provided name. Else all optimizers are saved by their opt_id numbers and type in a directory
            called name.
        """
        with FileNameHandler(name) as filename:
            if opt_id:
                self._write_file(opt_id, filename)
            else:
                os.makedirs(filename, exist_ok=True)
                os.chdir(filename)

                digits = len(str(max(self._storage)))
                for optimizer in self._storage:
                    opt_id = int(self._storage[optimizer].metadata["Optimizer ID"])
                    opt_type = self._storage[optimizer].metadata["Optimizer Type"]
                    title = f"{opt_id:0{digits}}_{opt_type}"
                    self._write_file(optimizer, title)

    def save_summary(self, name: str):
        """ Generates a summary file containing the best found point of each optimizer and the reason for their
            termination. name is the path and filename of the summary file.
        """
        with FileNameHandler(name) as filename:
            sum_data = {}
            for optimizer in self._storage:
                opt_history = self.get_history(optimizer)

                i_tot = len(opt_history)
                x_best = None
                f_best = float('nan')
                f_calls = None
                if i_tot > 0 and opt_history[i_tot]['i_best'] > -1:
                    last = opt_history[i_tot]
                    i_best = last['i_best']
                    f_calls = last['f_call_opt']

                    best = opt_history[i_best]

                    x_best = best['x']
                    f_best = best['fx_best']
                sum_data[optimizer] = {'end_cond': self._storage[optimizer].metadata["End Condition"],
                                       'f_calls': f_calls,
                                       'f_best': f_best,
                                       'x_best': x_best}

            with open(filename, "w+") as file:
                yaml.dump(sum_data, file, default_flow_style=False, sort_keys=False)

    def _write_file(self, opt_id, filename):
        yaml.add_representer(LiteralWrapper, literal_presenter)
        with open(f"{filename}.yml", 'w') as file:
            data = {"DETAILS": self._storage[opt_id].metadata,
                    "MESSAGES": self._storage[opt_id].messages,
                    "ITERATION_HISTORY": self._storage[opt_id].history}
            yaml.dump(data, file, default_flow_style=False, sort_keys=False)


class _OptimizerLogger:
    """ Stores history and meta data of a single optimizer started by GloMPO. """

    def __init__(self, opt_id: int, class_name: str, time_start: str):
        self.metadata = {"Optimizer ID": str(opt_id),
                         "Optimizer Type": class_name,
                         "Start Time": time_start}
        self.history = {}
        self.messages = []
        self.exit_cond = None

        self.fx_best = inf
        self.i_best = -1

    def update_metadata(self, key: str, value: str):
        """ Appends or overwrites given key-value pair in the stored optimizer metadata. """
        self.metadata[key] = value

    def append(self, i: int, f_call_overall: int, f_call_opt: int, x: Sequence[float], fx: float):
        """ Adds an optimizer iteration to the optimizer log. """
        if fx < self.fx_best:
            self.fx_best = fx
            self.i_best = i

        ls = None
        try:
            iter(x)
            ls = [float(num) for num in x]
        except TypeError:
            ls = [float(num) for num in [x]]
        finally:
            self.history[i] = {'f_call_overall': int(f_call_overall),
                               'f_call_opt': int(f_call_opt),
                               'fx': float(fx),
                               'i_best': int(self.i_best),
                               'fx_best': float(self.fx_best),
                               'x': ls}

    def append_message(self, message):
        """ Adds message to the optimizer history. """
        self.messages.append(message)

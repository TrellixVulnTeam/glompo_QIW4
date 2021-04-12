""" Contains classes which save log information for GloMPO and its optimizers. """

from pathlib import Path
from typing import Any, Dict, Optional, Union

import tables as tb

from ..common.namedtuples import IterationResult

try:
    from yaml import CDumper as Dumper
except ImportError:
    from yaml import Dumper
try:
    import matplotlib.pyplot as plt
    import matplotlib.lines as lines

    HAS_MATPLOTLIB = True
except (ModuleNotFoundError, ImportError):
    HAS_MATPLOTLIB = False

__all__ = ("OptimizerLogger",)


class OptimizerLogger:
    """ Stores progress of GloMPO optimizers. """

    def __init__(self, path: Union[str, Path], checksum: str, n_parms: int, expected_rows: int):
        self.pytab_file = tb.open_file(str(path), 'a', filters=tb.Filters(1, 'blosc'))
        self.expected_rows = expected_rows
        self.n_task_dims = n_parms

        self._best_iter = {'opt_id': 0, 'x': [], 'fx': float('inf')}

        self.pytab_file.root.attr.checksum = checksum

    def __contains__(self, item) -> bool:
        return f'opt_{item}' in self.pytab_file

    def __getitem__(self, opt_id: int) -> tb.Group:
        """ Returns an individual optimizer log. """
        return self.pytab_file.root[f'optimizer_{opt_id}']

    def __len__(self) -> int:
        evals = 0
        for tab in self.pytab_file.iter_nodes('/'):
            evals += tab.nrows
        return evals

    @property
    def best_iter(self) -> Dict[str, Any]:
        return self._best_iter

    def add_optimizer(self, iter_res: IterationResult, extra_heads: Optional[Dict[str, tb.Col]] = None):
        headers = {'timestamp': tb.Float32Col(pos=-4),
                   'n_iter': tb.Int32Col(pos=-3),
                   'x': tb.Float64Col(shape=self.n_task_dims, pos=-2),
                   'fx': tb.Float64Col(pos=-1)}

        if extra_heads:
            headers = {**headers, **extra_heads}

        self.pytab_file.create_group(where='/',
                                     name=f'optimizer_{iter_res.opt_id}')
        self.pytab_file.create_vlarray(where=f'/optimizer_{iter_res.opt_id}',
                                       name='messages',
                                       atom=tb.VLUnicodeAtom(),
                                       title="Messages Generated by Optimizer",
                                       expectedrows=3)
        self.pytab_file.create_table(where=f'/optimizer_{iter_res.opt_id}',
                                     name='iter_hist',
                                     description=headers,
                                     title="Iteration History",
                                     expectedrows=self.expected_rows)

    def put_iteration(self, iter_res: IterationResult):
        if iter_res.fx < self._best_iter['fx']:
            self._best_iter = {'x': iter_res.x, 'fx': iter_res.fx}

        table = self[iter_res.opt_id]['iter_hist']
        table.append([(iter_res.timestamp, iter_res.n_iter, iter_res.x, iter_res.fx, *iter_res.extras)])
        table.flush()

    def put_metadata(self, opt_id: int, key: str, value: str):
        """ Adds metadata about an optimizer. """
        self[opt_id]._v_attrs[key] = value

    def put_message(self, opt_id: int, message: str):
        """ Optimizers can signal special messages to the optimizer during the optimization which can be saved to
            the log.
        """
        table = self[opt_id]['messages']
        table.append(message)
        table.flush()

    def get_metadata(self, opt_id, key: str) -> Any:
        """ Returns metadata of a given optimizer and key. """
        return self[opt_id]._v_attrs[key]

    def close(self):
        self.pytab_file.close()

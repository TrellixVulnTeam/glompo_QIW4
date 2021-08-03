import warnings
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple, Union

import numpy as np
import tables as tb
from scm.params.common.parallellevels import ParallelLevels
from scm.params.common.reaxff_converter import geo_to_params, trainset_to_params
from scm.params.core.dataset import DataSet, SSE
from scm.params.core.jobcollection import JobCollection
from scm.params.core.opt_components import LinearParameterScaler, _Step
from scm.params.optimizers.base import BaseOptimizer, MinimizeResult
from scm.params.parameterinterfaces.base import BaseParameters
from scm.params.parameterinterfaces.reaxff import ReaxParams  # Instead of ReaxFFParameters for backward compatibility
from scm.params.parameterinterfaces.xtb import XTBParams
from scm.plams.core.errors import ResultsError
from scm.plams.interfaces.adfsuite.reaxff import reaxff_control_to_settings

from ..core.manager import GloMPOManager
from ..opt_selectors.baseselector import BaseSelector

__all__ = ("GlompoParamsWrapper",
           "ReaxFFError",
           "XTBError",
           "setup_reax_from_classic",
           "setup_reax_from_params",
           "setup_xtb_from_params",)


class _FunctionWrapper:
    """ Wraps function produced by ParAMS internals (instance of :class:`!scm.params.core.opt_components._Step`) to
    match the API required by the :attr:`.GloMPOManager.task`. Can be modified to achieve compatibility with other
    optimizers.
    """

    def __init__(self, func: _Step):
        self.func = func
        if self.func.cbs:
            warnings.warn("Callbacks provided through the Optimization class are ignored. Callbacks to individual "
                          "optimizers can be passed to GloMPO through BaseSelector objects. Callbacks to control the "
                          "manager itself are passed using GloMPO BaseChecker objects, some conditions should be sent "
                          "as BaseHunter objects.", UserWarning)
            self.func.cbs = None

    def __call__(self, pars) -> float:
        return self.func(pars)


class GlompoParamsWrapper(BaseOptimizer):
    """ Wraps the GloMPO manager into a ParAMS :class:`!params.BaseOptimizer`.
    This is not the recommended way to make use of the GloMPO interface, it is preferable to make use of the
    :class:`.BaseParamsError` classes. This class is only applicable in cases where the ParAMS :class:`!Optimization`
    class interface is preferred.
    """

    def __init__(self, opt_selector: BaseSelector, **manager_kwargs):
        """ Accepts GloMPO configuration information.

            Parameters
            ----------
            opt_selector
                Initialised :class:`.BaseSelector` object which specifies how optimizers are selected and initialised.
            **manager_kwargs
                Optional arguments to the :class:`.GloMPOManager` initialisation function.

                .. note::

                   All arguments are accepted but required GloMPO arguments :attr:`~.GloMPOManager.task` and
                   :attr:`~.GloMPOManager.bounds` will be overwritten as they are passed by the :meth:`minimize`
                   function in accordance with ParAMS API.
        """
        self.manager = GloMPOManager()
        self.manager_kwargs = manager_kwargs
        for kw in ['task', 'bounds']:
            if kw in self.manager_kwargs:
                del self.manager_kwargs[kw]

        self.selector = opt_selector

    def minimize(self,
                 function: _Step,
                 x0: Sequence[float],
                 bounds: Sequence[Tuple[float, float]],
                 workers: int = 1) -> MinimizeResult:
        """
        Passes 'function' to GloMPO to be minimized. Returns an instance of MinimizeResult.

        Parameters
        ----------
        function
            Function to be minimized, this is passed as GloMPO's :attr:`~.GloMPOManager.task` parameter.
        x0
            Ignored by GloMPO, the correct way to control the optimizer starting points is by using GloMPO
            :class:`.BaseGenerator` objects.
        bounds
            Sequence of (min, max) pairs used to bound the search area for every parameter. The 'bounds' parameter is
            passed to GloMPO as its :attr:`~.GloMPOManager.bounds` parameter.

            .. note::

               By default ParAMS shifts and scales all parameters to the interval (0, 1). GloMPO will work in this space
               and be blind to the true bounds, thus results from the GloMPO logs cannot be applied directly to the
               function.

        workers
            Represents the maximum number of optimizers run in parallel. Passed to GloMPO as its
            :attr:`~.GloMPOManager.max_jobs` parameter if it has not been sent during initialisation via
            `manager_kwargs` otherwise ignored. If allowed to default this will usually result in the number of
            optimizers as there are cores available.

        Notes
        -----
        GloMPO is not currently compatible with using multiple DataSets and only the first one will be considered.
        """

        warnings.warn("The x0 parameter is ignored by GloMPO. To control the starting locations of optimizers within "
                      "GloMPO make use of its BaseGenerator objects.", RuntimeWarning)

        if 'max_jobs' not in self.manager_kwargs:
            self.manager_kwargs['max_jobs'] = workers

        # Silence function printing
        function.v = False

        self.manager.setup(task=_FunctionWrapper(function), bounds=bounds, opt_selector=self.selector,
                           **self.manager_kwargs)

        result = self.manager.start_manager()

        # Reshape glompo.common.namedtuples.Result into scm.params.optimizers.base.MinimizeResult
        params_res = MinimizeResult()
        params_res.x = result.x
        params_res.fx = result.fx
        params_res.success = self.manager.converged and len(result.x) > 0

        return params_res


class BaseParamsError:
    """ Base error function instance from which other classes derive depending on the engine used e.g. ReaxFF, xTB etc.
    Primarily initialized from ParAMS objects. To initialize from files see the class methods
    :meth:`~.ReaxFFError.from_classic_files` or :meth:`~.ReaxFFError.from_params_files`.

    Parameters
    ----------
    data_set
        Reference data used to compare against force field results.
    job_collection
        AMS jobs from which the data can be extracted for comparison to the :class:`!DataSet`
    parameters
        :class:`!BaseParameters` object which holds the force field values, ranges, engine and which
        parameters are active or not.
    validation_dataset
        If a validation set is being used and evaluated along with the training set, it may be added here.
        Jobs for the validation set are expected to be included in `job_collection`.
    scale_residuals
        See :attr:`scale_residuals`.

    Attributes
    ----------
    dat_set : DataSet
        Represents the training set.
    job_col : JobCollection
        Represents the jobs from which model results will be extracted and compared to the training set.
    loss : Union[str, Loss]
        Method by which individual errors are grouped into a single error function value.
    par_eng : BaseParameters
        Parameter engine interface representing the model and its parameters to tune.
    par_levels : ParallelLevels
        The layers of parallelism possible within the evaluation of the jobs.
    scale_residuals : bool
        If :obj:`True` then the raw residuals (i.e. the differences between engine evaluation and training data)
        will be scaled by the weight and sigma values in the datasets i.e. :code:`r_scaled = weight * r / sigma`.
        Otherwise the raw residual is returned. This setting effects :meth:`resids` and :meth:`detailed_call`.
    scaler : LinearParameterScaler
        Objects which can transform parameters from their actual values to between the values of 0 and 1 (the space in
        which the optimization is done) and back again.
    val_set : DataSet
        Optional validation set to evaluate in parallel to the training set.
    """

    def __init__(self, data_set: DataSet, job_collection: JobCollection, parameters: BaseParameters,
                 validation_dataset: Optional[DataSet] = None,
                 scale_residuals: bool = True):
        self.dat_set = data_set
        self.job_col = job_collection
        self.par_eng = parameters
        self.val_set = validation_dataset

        self.scale_residuals = scale_residuals

        self.loss = SSE()
        self.scaler = LinearParameterScaler(self.par_eng.active.range)
        self.par_levels = ParallelLevels(jobs=1)

    @property
    def n_parms(self) -> int:
        """ Returns the number of active parameters. """
        return len(self.par_eng.active.x)

    @property
    def bounds(self) -> Sequence[Tuple[float, float]]:
        """ Returns the min, max bounds in each dimension. """
        return [(0, 1)] * self.n_parms

    def __call__(self, x: Sequence[float]) -> float:
        """ Returns the error value between the the force field with the given parameters and the training values. """
        return self._calculate(x)[0][0]

    def detailed_call(self, x: Sequence[float]) -> Union[Tuple[float, np.ndarray],
                                                         Tuple[float, np.ndarray, float, np.ndarray]]:
        """ A full return of the error results. Returns a tuple of:

                :code:`training_set_error, [training_set_residual_1, ..., training_set_residual_N]`

            If a validation set is included then returned tuple is:

            .. code-block:: python

               training_set_error, [training_set_residual_1, ..., training_set_residual_N],
               validation_set_error, [validation_set_residual_1, ..., validation_set_residual_N]

        """
        calc = self._calculate(x)
        ts_fx = calc[0][0]
        ts_resids = calc[0][1]
        ts_resids = self._scale_residuals(ts_resids, self.dat_set) if self.scale_residuals else ts_resids

        if self.val_set is not None:
            vs_fx = calc[1][0]
            vs_resids = calc[1][1]
            vs_resids = self._scale_residuals(vs_resids, self.val_set) if self.scale_residuals else vs_resids
            return ts_fx, ts_resids, vs_fx, vs_resids

        return ts_fx, ts_resids

    def headers(self) -> Dict[str, tb.Col]:
        """ Returns a the column headers for the :meth:`detailed_call` return.
        See :meth:`.BaseFunction.headers`.
        """
        heads = {'fx': tb.Float64Col(pos=0),
                 'resids_ts': tb.Float64Col((1, len(self.dat_set)), pos=1)}

        if self.val_set:
            heads['fx_vs'] = tb.Float64Col(pos=2)
            heads['resids_vs'] = tb.Float64Col((1, len(self.val_set)), pos=3)

        return heads

    def resids(self, x: Sequence[float]) -> np.ndarray:
        """ Method for compatibility with GFLS optimizer.
        Returns the signed differences between the force field and training set residuals. Will be scaled by sigma and
        weight if :attr:`scale_residuals` is :obj:`True`, otherwise not.
        """
        residuals = self._calculate(x)[0][1]
        if self.scale_residuals:
            residuals = self._scale_residuals(residuals, self.dat_set)

        return residuals

    def save(self, path: Union[Path, str], filenames: Optional[Dict[str, str]] = None,
             parameters: Optional[Sequence[float]] = None):
        """ Writes the :attr:`dat_set` and :attr:`job_col` to YAML files.
        Writes the engine object to an appropriate parameter file.

        Parameters
        ----------
        path
            Path to directory in which files will be saved.
        filenames
            Custom filenames for the written files. The dictionary may include any/all of the keys in the example
            below. This example contains the default names used if not given::

                {'ds': 'data_set.yml', 'jc': 'job_collection.yml', 'ff': 'ffield'}

        parameters
            Optional parameters to be written into the force field file. If not given, the parameters currently
            therein will be used.
        """
        if not filenames:
            filenames = {}

        names = {'ds': filenames['ds'] if 'ds' in filenames else 'data_set.yml',
                 'jc': filenames['jc'] if 'jc' in filenames else 'job_collection.yml',
                 'ff': filenames['ff'] if 'ff' in filenames else 'ffield'}

        self.dat_set.store(str(Path(path, names['ds'])))
        self.job_col.store(str(Path(path, names['jc'])))
        self.par_eng.write(str(Path(path, names['ff'])), parameters)

    def _calculate(self, x: Sequence[float]) -> Sequence[Tuple[float, np.ndarray, np.ndarray]]:
        """ Core calculation function, returns both the error function value and the residuals. """
        default = (float('inf'), np.array([float('inf')]), np.array([float('inf')]))
        try:
            engine = self.par_eng.get_engine(self.scaler.scaled2real(x))
            ff_results = self.job_col.run(engine.settings, parallel=self.par_levels)
            ts_result = self.dat_set.evaluate(ff_results, self.loss, True)
            vs_result = self.val_set.evaluate(ff_results, self.loss, True) if self.val_set is not None else default
            return (ts_result[0], np.squeeze(ts_result[1]), np.squeeze(ts_result[2])), \
                   (vs_result[0], np.squeeze(vs_result[1]), np.squeeze(vs_result[2]))
        except ResultsError:
            return default, default

    @staticmethod
    def _scale_residuals(resids: np.ndarray, data_set: DataSet) -> np.ndarray:
        """ Scales a sequence of residuals by weight and sigma values in the associated DataSet"""
        return np.array(data_set.get('weight')) / np.array(data_set.get('sigma')) * resids


class ReaxFFError(BaseParamsError):
    """ ReaxFF error function. """

    @classmethod
    def from_classic_files(cls, path: Union[Path, str], **kwargs) -> 'ReaxFFError':
        """ Initializes the error function from classic ReaxFF files.

        Parameters
        ----------
        path
            Path to classic ReaxFF files, passed to :func:`.setup_reax_from_classic`.
        """
        dat_set, job_col, rxf_eng = setup_reax_from_classic(path)
        return cls(dat_set, job_col, rxf_eng, **kwargs)

    @classmethod
    def from_params_files(cls, path: Union[Path, str], **kwargs) -> 'ReaxFFError':
        """ Initializes the error function from ParAMS data files.

        Parameters
        ----------
        path
            Path to directory containing ParAMS data set, job collection and ReaxFF engine files (see
            :func:`.setup_reax_from_params`).
        """
        dat_set, job_col, rxf_eng = setup_reax_from_params(path)
        return cls(dat_set, job_col, rxf_eng, **kwargs)

    def checkpoint_save(self, path: Union[Path, str]):
        """ Used to store files into a GloMPO checkpoint (at path) suitable to reconstruct the task when the checkpoint
        is loaded.
        """
        self.dat_set.pickle_dump(str(Path(path, 'data_set.pkl')))
        self.job_col.pickle_dump(str(Path(path, 'job_collection.pkl')))
        self.par_eng.pickle_dump(str(Path(path, 'reax_params.pkl')))  # Method does not support Path


class XTBError(BaseParamsError):
    """ GFN-xTB error function. """

    @classmethod
    def from_params_files(cls, path: Union[Path, str], **kwargs) -> 'XTBError':
        """ Initializes the error function from ParAMS data files.

        Parameters
        ----------
        path
            Path to directory containing ParAMS data set, job collection and ReaxFF engine files (see
            :func:`setup_reax_from_params`).
        """
        dat_set, job_col, rxf_eng = setup_xtb_from_params(path)
        return cls(dat_set, job_col, rxf_eng, **kwargs)

    def checkpoint_save(self, path: Union[Path, str]):
        """ Used to store files into a GloMPO checkpoint (at path) suitable to reconstruct the task when the checkpoint
        is loaded.
        """
        self.dat_set.pickle_dump(Path(path, 'data_set.pkl'))
        self.job_col.pickle_dump(Path(path, 'job_collection.pkl'))
        self.par_eng.write(path)


def setup_reax_from_classic(path: Union[Path, str]) -> Tuple[DataSet, JobCollection, ReaxParams]:
    """ Parses classic ReaxFF force field and configuration files into instances which can be evaluated by AMS.

    Parameters
    ----------
    path
        Path to folder containing:

            trainset.in
                Contains the description of the items in the training set
            control
                Contains ReaxFF settings
            ffield_init
                A force field file which contains values for all the parameters
            ffield_bool
                A force field file with all parameters set to 0 or 1.
                1 indicates it will be adjusted during optimisation.
                0 indicates it will not be changed during optimisation.
            ffield_max
                A force field file where the active parameters are set to their maximum value (value of other parameters
                is ignored).
            ffield_min
                A force field file where the active parameters are set to their maximum value (value of other parameters
                is ignored).
            geo
                Contains the geometries of the items used in the training set.
    """

    dat_set = trainset_to_params(Path(path, 'trainset.in'))
    rxf_eng = ReaxParams(Path(path, 'ffield_bool'))
    vars_max = ReaxParams(Path(path, 'ffield_max'))
    vars_min = ReaxParams(Path(path, 'ffield_min'))

    # Update the job collection depending on the types of data in the training set
    settings = reaxff_control_to_settings(Path(path, 'control'))
    if dat_set.forces():
        settings.input.ams.properties.gradients = True
    job_col = geo_to_params(Path(path, 'geo'), settings)

    # Remove training set entries not in job collection
    remove_ids = dat_set.check_consistency(job_col)
    if remove_ids:
        print(
            'The following jobIDs are not in the JobCollection, their respective training set entries will be removed:')
        print('\n'.join({s for e in [dat_set[i] for i in remove_ids] for s in e.jobids}))
        del dat_set[remove_ids]

    rxf_eng.is_active = [bool(val) for val in rxf_eng.x]

    for i, parm in enumerate(rxf_eng):
        if parm.is_active:
            if vars_min[i].value != vars_max[i].value:
                parm.range = (vars_min[i].value, vars_max[i].value)
            else:
                parm.x = vars_min[i].value
                parm.is_active = False
                print(f"WARNING: {parm.name} deactivated due to bounds.")

    vars_values = ReaxParams(Path(path, 'ffield_init'))
    rxf_eng.x = vars_values.x
    for parm in rxf_eng.active:
        if not parm.range[0] < parm.value < parm.range[1]:
            parm.value = (parm.range[0] + parm.range[1]) / 2
            warnings.warn("Starting value out of bounds moving to midpoint.")

    return dat_set, job_col, rxf_eng


def _setup_collections_from_params(path: Union[Path, str]) -> Tuple[DataSet, JobCollection]:
    """ Loads ParAMS produced ReaxFF files into ParAMS objects.

    Parameters
    ----------
    path
        Path to folder containing:
            ``data_set.yml`` OR ``data_set.pkl``
                Contains the description of the items in the training set. A YAML file must be of the form produced by
                :meth:`!DataSet.store`, a pickle file must be of the form produced by :meth:`!DataSet.pickle_dump`. If
                both files are present, the pickle is given priority.
            ``job_collection.yml`` OR ``job_collection.pkl``
                Contains descriptions of the AMS jobs to evaluate. A YAML file must be of the form produced by
                :meth:`!JobCollection.store`, a pickle file must be of the form produced by
                :meth:`!JobCollection.pickle_dump`.  If both files are present, the pickle is given priority.
    """
    dat_set = DataSet()
    job_col = JobCollection()

    for name, params_obj in {'data_set': dat_set, 'job_collection': job_col}.items():
        built = False
        for suffix, loader in {'.pkl': 'pickle_load', '.yml': 'load'}.items():
            file = Path(path, name + suffix)
            if file.exists():
                getattr(params_obj, loader)(str(file))
                built = True
        if not built:
            raise FileNotFoundError(f"No {name.replace('_', ' ')} data found")

    return dat_set, job_col


def setup_reax_from_params(path: Union[Path, str]) -> Tuple[DataSet, JobCollection, ReaxParams]:
    """ Loads ParAMS produced ReaxFF files into ParAMS objects.

    Parameters
    ----------
    path
        Path to folder containing:
            ``data_set.yml`` OR ``data_set.pkl``
                Contains the description of the items in the training set. A YAML file must be of the form produced by
                :meth:`!DataSet.store`, a pickle file must be of the form produced by :meth:`!DataSet.pickle_dump`. If
                both files are present, the pickle is given priority.
            ``job_collection.yml`` OR ``job_collection.pkl``
                Contains descriptions of the AMS jobs to evaluate. A YAML file must be of the form produced by
                :meth:`!JobCollection.store`, a pickle file must be of the form produced by
                :meth:`!JobCollection.pickle_dump`.  If both files are present, the pickle is given priority.
            ``reax_params.pkl``
                Pickle produced by :meth:`!ReaxParams.pickle_dump`, representing the force field, active parameters and
                their ranges.
    """
    dat_set, job_col = _setup_collections_from_params(path)
    rxf_eng = ReaxParams.pickle_load(Path(path, 'reax_params.pkl'))

    return dat_set, job_col, rxf_eng


def setup_xtb_from_params(path: Union[Path, str]) -> Tuple[DataSet, JobCollection, XTBParams]:
    """ Loads ParAMS produced ReaxFF files into ParAMS objects.

    Parameters
    ----------
    path
        Path to folder containing:
            ``data_set.yml`` OR ``data_set.pkl``
                Contains the description of the items in the training set. A YAML file must be of the form produced by
                :meth:`!DataSet.store`, a pickle file must be of the form produced by :meth:`!DataSet.pickle_dump`.
                If both files are present, the pickle is given priority.
            ``job_collection.yml`` OR ``job_collection.pkl``
                Contains descriptions of the AMS jobs to evaluate. A YAML file must be of the form produced by
                :meth:`!JobCollection.store`, a pickle file must be of the form produced by
                :meth:`!JobCollection.pickle_dump`.  If both files are present, the pickle is given priority.
            ``elements.xtbpar``, ``basis.xtbpar``, ``globals.xtbpar``, ``additional_parameters.yaml``, ``metainfo.yaml``, ``atomic_configurations.xtbpar``, ``metals.xtbpar``
                Classic xTB parameter files.

    """
    dat_set, job_col = _setup_collections_from_params(path)
    xtb_eng = XTBParams(path)

    return dat_set, job_col, xtb_eng

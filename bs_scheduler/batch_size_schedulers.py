# Inspired from https://pytorch.org/docs/stable/_modules/torch/optim/lr_scheduler.html.
import math
import torch
import types
from bisect import bisect_right
from collections import Counter
from typing import Callable, Union, Sequence, Tuple

from torch.utils.data import DataLoader, Dataset

__all__ = ['LambdaBS', 'MultiplicativeBS', 'StepBS', 'MultiStepBS', 'ConstantBS', 'LinearBS', 'ExponentialBS',
           'SequentialBS', 'PolynomialBS', 'CosineAnnealingBS', 'ChainedBSScheduler', 'BSScheduler', 'BatchSizeManager']


def rint(x: float) -> int:
    """ Rounds to the nearest int and returns the value as int.
    """
    return int(round(x))


def clip(x: int, min: int, max: int) -> int:
    """ Clips x to [min, max] interval.
    """
    if x < min:
        return min
    if x > max:
        return max
    return x


def check_isinstance(x, instance: type):
    if not isinstance(x, instance):
        raise TypeError(f"{type(x).__name__} is not a {x.__name__}.")


class BatchSizeManager:
    """ Base class for all batch size managers, used for getting and setting the batch size. It is not mandatory to
    inherit from this, but users must implement :meth:`get_current_batch_size` and :meth:`set_batch_size`.
    """

    def get_current_batch_size(self) -> int:
        """ Returns the current batch size used by the dataloader as an :class:`int`.
        """
        raise NotImplementedError

    def set_batch_size(self, new_bs: int):
        """ Sets the new value of the batch size.

        Args:
            new_bs (int): The new batch sizes that needs to be set.
        """
        raise NotImplementedError


class DefaultBatchSizeManager(BatchSizeManager):
    """ The default batch size manager used when the dataloader has a batch sampler. The batch sampler controls the
    batch size used by the dataloader, and it can be queried and changed. Changes are reflected in the number of samples
    given to the dataloader. See
    https://github.com/pytorch/pytorch/blob/772e104dfdfd70c74cbc9600cfc946dc7c378f68/torch/utils/data/sampler.py#L241.
    """

    def __init__(self, dataloader: DataLoader):
        check_isinstance(dataloader, DataLoader)
        if dataloader.batch_sampler is None:
            raise ValueError(f"Dataloader must have a batch sampler.")
        self.dataloader: DataLoader = dataloader

    def get_current_batch_size(self) -> int:
        """ Returns the current batch size used by the dataloader as an :class:`int`, which is the owned by the batch
        sampler.
        """
        return self.dataloader.batch_sampler.batch_size

    def set_batch_size(self, new_bs: int):
        """ Sets the new value of the batch size, which is owned by the batch sampler.

        Args:
            new_bs (int): The new batch sizes that needs to be set.
        """
        self.dataloader.batch_sampler.batch_size = new_bs


class CustomBatchSizeManager(BatchSizeManager):
    """ Custom batch size manager, used when the dataloader does not use a batch sampler. In this case, the batch size
    is controlled by the dataset wrapped by the dataloader, so this class expects the dataset to provide a getter and
    a setter for the batch size, named :meth:`get_batch_size` and :meth:`change_batch_size` respectively.
    """

    def __init__(self, dataset: Dataset):
        check_isinstance(dataset, Dataset)
        if not hasattr(dataset, "change_batch_size"):
            raise KeyError("Because the dataloader does not have a batch sampler, the dataset owns and controls the "
                           "batch size. In order to change the batch size after dataloader creation we require our "
                           "users to implement a Callable[[int],None] method named `change_batch_size` in their "
                           "dataset which changes the batch size. Please see TODO for examples.")
        if not hasattr(dataset, "get_batch_size"):
            raise KeyError("We require our users to implement a Callable[[], int] method named `get_batch_size` in "
                           "their dataset which returns the current batch size. Please see TODO for examples. ")
        self.dataset = dataset

    def get_current_batch_size(self) -> int:
        """ Returns the current batch size used by the dataset as an :class:`int`.

        In this case, the dataset controls the batch size, so we require our users to implement a
        :class:`Callable[[], int]` method named :meth:`get_batch_size` in their dataset which returns the current value
        of the batch size.
        """
        return self.dataset.get_batch_size()

    def set_batch_size(self, new_bs: int):
        """ Sets the new value of the batch size.

        In this case, the dataset controls the batch size, so we require our users to implement a
        :class:`Callable[[int],None]` method named :meth:`change_batch_size` in their dataset which modifies the batch
        size to the given value.

        Args:
            new_bs (int): The new batch sizes that needs to be set.
        """
        self.dataset.change_batch_size(new_bs)


class BSScheduler:
    def __init__(self, dataloader: DataLoader, batch_size_manager: Union[BatchSizeManager, None],
                 max_batch_size: Union[int, None], min_batch_size: int, verbose: bool):
        try:
            # Should we allow our users to use us with dataloader == None and just use the batch size managers they
            # provide us with?
            check_isinstance(dataloader, DataLoader)
        except TypeError:
            print("If you really need this feature, please open an issue at "
                  "https://github.com/ancestor-mithril/bs_scheduler/issues and describe your use case.")
            raise
        self.dataloader: DataLoader = dataloader
        self.verbose: bool = verbose

        assert max_batch_size is None or isinstance(max_batch_size, int)
        assert isinstance(min_batch_size, int)
        if max_batch_size is None:
            self.max_batch_size: int = len(self.dataloader.dataset)
        else:
            if max_batch_size < 0:
                raise ValueError(f"Maximum batch size must be greater than 0, but is {max_batch_size}.")
            self.max_batch_size: int = min(len(self.dataloader.dataset), max_batch_size)

        if min_batch_size < 0:
            raise ValueError(f"Minimum batch size must be greater than 0, but is {min_batch_size}.")
        if min_batch_size > self.max_batch_size:
            raise ValueError(f"Minimum batch size must be smaller than or equal to the maximum batch size "
                             f"({max_batch_size}), but is {min_batch_size}.")
        self.min_batch_size: int = min_batch_size

        if batch_size_manager is not None:
            self.batch_size_manager: BatchSizeManager = batch_size_manager
        elif self.dataloader.batch_sampler is not None:
            self.batch_size_manager: BatchSizeManager = DefaultBatchSizeManager(self.dataloader)
        else:
            # We require the client to implement a "change_batch_size" method and a "get_batch_size" method for their
            # dataset.
            self.batch_size_manager: BatchSizeManager = CustomBatchSizeManager(self.dataloader.dataset)

        # Taking over the batch size manager methods for easier batch size getting.
        self.get_current_batch_size: Callable[[], int] = self.batch_size_manager.get_current_batch_size

        # See https://pytorch.org/docs/stable/_modules/torch/optim/lr_scheduler.html for "with_counter".
        self.last_epoch: int = -1
        if not hasattr(self.dataloader, '_base_batch_size'):
            self.dataloader._base_batch_size = self.get_current_batch_size()
        self._last_bs: int = self.dataloader._base_batch_size
        self._finished: bool = False
        self.step()
        # The initial step may make the scheduler to finish during initialization. So we reinitialize self._finished.
        self._finished = False

    def set_batch_size(self, new_bs: int):
        """ Forwards the call for setting the new batch size to the batch size manager. If the dataloader batch_size
        member variable is not None, it also modifies it to reflect the change in batch size.

        Args:
            new_bs (int): The new batch sizes that needs to be set.
        """
        if self.dataloader.batch_size is not None:
            # We can't directly do `dataloader.batch_size` = new_bs because the dataloader raises an error if we change
            # the batch size after initialization. But we are still hacking around it.
            self.dataloader.__dict__['batch_size'] = new_bs
        self.batch_size_manager.set_batch_size(new_bs)

    def finished(self) -> bool:
        """ Returns True if the scheduler has already finished its job or has exceeded the minimum or maximum batch
        size. Otherwise, returns False.
        """
        return self._finished

    def state_dict(self) -> dict:
        """ Returns the state of the scheduler as a :class:`dict`.

        It contains an entry for every variable in self.__dict__ which is not the dataloader.
        """
        return {key: value for key, value in self.__dict__.items() if key != 'dataloader'}

    def load_state_dict(self, state_dict: dict):
        """ Loads the schedulers state.

        Args:
            state_dict (dict): scheduler state. Should be an object returned from a call to :meth:`state_dict`.
        """
        self.__dict__.update(state_dict)
        # TODO: Test training, saving, loading and resuming scheduler. Ensure that the batch size is set correctly.
        self.set_batch_size(self.get_last_bs())  # Setting the batch size to the last computed batch size.

    def get_last_bs(self) -> int:
        """ Returns the last computed batch size by current scheduler. If called before the first call to :meth:`step`
        returns the base batch size.
        """
        return self._last_bs

    def get_bs(self) -> int:
        """ Computes the next batch size. Should not be called explicitly in client code, but it doesn't really matter
        if the client does so.
        """
        raise NotImplementedError

    def print_bs(self, new_bs):
        if self.verbose:
            print(f'Adjusting batch size to {new_bs}')

    def step(self):
        # TODO: Documentation
        # TODO: Check how the dataloader behaves if we change the batch size mid epoch. Write a guideline for this.
        #  Changing the batch size does not impact batch sizes loaded by workers before the change.
        # TODO: Check if changing the batch size needs locking. Because of multiprocessing. Normally it should not.
        if self.finished():
            return  # Stops doing work if already finished.

        self.last_epoch += 1
        new_bs = self.get_bs()
        if not self.min_batch_size <= new_bs <= self.max_batch_size:
            self._finished = True
            new_bs = clip(new_bs, min=self.min_batch_size, max=self.max_batch_size)
        self.set_batch_size(new_bs)
        self.print_bs(new_bs)
        self._last_bs = new_bs


class LambdaBS(BSScheduler):
    """ Sets the batch size to the initial batch size times a given function. Unlike torch.optim.lr_scheduler.LambdaLR,
    there is a single batch size for a given dataloader so only one function should be passed as a parameter.

    Args:
        dataloader (DataLoader): Wrapped dataloader.
        bs_lambda (Callable[[int], float]): A function which computes a multiplicative factor given an integer
            parameter epoch.
        batch_size_manager (Union[BatchSizeManager, None]): If not None, a custom class which manages the batch size,
            which provides a getter and setter for the batch size. Default: None.
        max_batch_size (Union[int, None]): Upper limit for the batch size so that a batch of size max_batch_size fits
            in the memory. If None or greater than the lenght of the dataset wrapped by the dataloader, max_batch_size
            is set to `len(self.dataloader.dataset)`. Default: None.
        min_batch_size (int): Lower limit for the batch size which must be greater than 0. Default: 1.
        verbose (bool): If ``True``, prints a message to stdout for each update. Default: ``False``.

    Example:
        >>> dataloader = ...
        >>> func = lambda epoch: 1.05 ** epoch
        >>> scheduler = LambdaBS(dataloader, bs_lambda=func)
        >>> for epoch in range(100):
        >>>     train(...)
        >>>     validate(...)
        >>>     scheduler.step()
    """

    def __init__(self, dataloader: DataLoader, bs_lambda: Callable[[int], int],
                 batch_size_manager: Union[BatchSizeManager, None] = None, max_batch_size: Union[int, None] = None,
                 min_batch_size: int = 1, verbose: bool = False):
        assert callable(bs_lambda)
        self.bs_lambda: Callable[[int], int] = bs_lambda
        super().__init__(dataloader, batch_size_manager, max_batch_size, min_batch_size, verbose)

    def state_dict(self) -> dict:
        """ Returns the state of the scheduler as a :class:`dict`.

        It contains an entry for every variable in self.__dict__ which is not the dataloader. The batch size lambda
        function will only be saved if they are callable objects and not if they are functions or lambdas.
        """
        state_dict = {key: value for key, value in self.__dict__.items() if key not in ('dataloader', 'bs_lambda')}
        state_dict['bs_lambda'] = None
        if not isinstance(self.bs_lambda, types.FunctionType):
            self.bs_lambda.__dict__.copy()
        return state_dict

    def load_state_dict(self, state_dict: dict):
        """Loads the schedulers state.

        Args:
            state_dict (dict): scheduler state. Should be an object returned from a call to :meth:`state_dict`.
        """
        bs_lambda = state_dict.pop('bs_lambda')
        self.__dict__.update(state_dict)
        self.set_batch_size(self.get_last_bs())  # Setting the batch size to the last computed batch size.
        if bs_lambda is not None:
            self.bs_lambda.__dict__.update(bs_lambda)

    def get_bs(self) -> int:
        """ Returns the next batch size as an :class:`int`.

        It is calculated as the initial value of the batch size times the factor returned by `bs_lambda`.
        """
        return rint(self.dataloader._base_batch_size * self.bs_lambda(self.last_epoch))


class MultiplicativeBS(BSScheduler):
    """ Multiply the batch size by a factor given in the specified function. Unlike
    torch.optim.lr_scheduler.MultiplicativeLR, there is a single batch size for a given dataloader so only one function
    should be passed as a parameter.

    Args:
        dataloader (DataLoader): Wrapped dataloader.
        bs_lambda: (Callable[[int], float]): A function which computes a multiplicative factor given an integer
            parameter epoch.
        batch_size_manager (Union[BatchSizeManager, None]): If not None, a custom class which manages the batch size,
            which provides a getter and setter for the batch size. Default: None.
        max_batch_size (Union[int, None]): Upper limit for the batch size so that a batch of size max_batch_size fits
            in the memory. If None or greater than the lenght of the dataset wrapped by the dataloader, max_batch_size
            is set to `len(self.dataloader.dataset)`. Default: None.
        min_batch_size (int): Lower limit for the batch size which must be greater than 0. Default: 1.
        verbose (bool): If ``True``, prints a message to stdout for each update. Default: ``False``.

    Example:
        >>> dataloader = ...
        >>> func = lambda epoch: 1.05
        >>> scheduler = MultiplicativeBS(dataloader, bs_lambda=func)
        >>> for epoch in range(100):
        >>>     train(...)
        >>>     validate(...)
        >>>     scheduler.step()
    """

    def __init__(self, dataloader: DataLoader, bs_lambda: Callable[[int], int],
                 batch_size_manager: Union[BatchSizeManager, None] = None, max_batch_size: Union[int, None] = None,
                 min_batch_size: int = 1, verbose: bool = False):
        assert callable(bs_lambda)
        self.bs_lambda: Callable[[int], int] = bs_lambda
        super().__init__(dataloader, batch_size_manager, max_batch_size, min_batch_size, verbose)

    def state_dict(self) -> dict:
        """ Returns the state of the scheduler as a :class:`dict`.

        It contains an entry for every variable in self.__dict__ which is not the dataloader. The batch size lambda
        function will only be saved if they are callable objects and not if they are functions or lambdas.
        """
        state_dict = {key: value for key, value in self.__dict__.items() if key not in ('dataloader', 'bs_lambda')}
        state_dict['bs_lambda'] = None
        if not isinstance(self.bs_lambda, types.FunctionType):
            self.bs_lambda.__dict__.copy()
        return state_dict

    def load_state_dict(self, state_dict: dict):
        """Loads the schedulers state.

        Args:
            state_dict (dict): scheduler state. Should be an object returned from a call to :meth:`state_dict`.
        """
        bs_lambda = state_dict.pop('bs_lambda')
        self.__dict__.update(state_dict)
        self.set_batch_size(self.get_last_bs())  # Setting the batch size to the last computed batch size.
        if bs_lambda is not None:
            self.bs_lambda.__dict__.update(bs_lambda)

    def get_bs(self) -> int:
        """ Returns the next batch size as an :class:`int`.

        It is calculated as the current value of the batch size times the factor returned by `bs_lambda`.
        """
        return rint(self.get_current_batch_size() * self.bs_lambda(self.last_epoch))


class StepBS(BSScheduler):
    """ Multiplies the batch size by gamma every step_size epochs.

    Args:
        dataloader (DataLoader): Wrapped dataloader.
        step_size (int): Period of batch size growth.
        gamma (float): Multiplicative factor of batch size growth. Default: 2.0.
        batch_size_manager (Union[BatchSizeManager, None]): If not None, a custom class which manages the batch size,
            which provides a getter and setter for the batch size. Default: None.
        max_batch_size (Union[int, None]): Upper limit for the batch size so that a batch of size max_batch_size fits
            in the memory. If None or greater than the lenght of the dataset wrapped by the dataloader, max_batch_size
            is set to `len(self.dataloader.dataset)`. Default: None.
        min_batch_size (int): Lower limit for the batch size which must be greater than 0. Default: 1.
        verbose (bool): If ``True``, prints a message to stdout for each update. Default: ``False``.

    Example:
        >>> dataloader = ...
        >>> # Assuming the base batch size is 10.
        >>> # bs = 10 if epoch < 30
        >>> # bs = 20 if 30 <= epoch < 60
        >>> # bs = 40 if 60 <= epoch < 90
        >>> # ...
        >>> scheduler = StepBS(dataloader, step_size=30, gamma=2.0)
        >>> for epoch in range(100):
        >>>     train(...)
        >>>     validate(...)
        >>>     scheduler.step()
    """

    def __init__(self, dataloader: DataLoader, step_size: int, gamma: float = 2.0,
                 batch_size_manager: Union[BatchSizeManager, None] = None, max_batch_size: Union[int, None] = None,
                 min_batch_size: int = 1, verbose: bool = False):
        assert isinstance(step_size, int) and step_size > 0
        assert gamma > 0.0
        # Gamma is expected to be greater than 1, but we do not forbid batch size decay.
        self.step_size: int = step_size
        self.gamma: float = gamma
        super().__init__(dataloader, batch_size_manager, max_batch_size, min_batch_size, verbose)

    def get_bs(self) -> int:
        """ Returns the next batch size as an :class:`int`.

        It returns the current batch size times gamma each step_size epochs, otherwise it returns the current batch
        size.
        """
        if self.last_epoch == 0 or self.last_epoch % self.step_size != 0:
            return self.get_current_batch_size()
        return rint(self.get_current_batch_size() * self.gamma)


class MultiStepBS(BSScheduler):
    """ Multiplies the batch size by gamma once the number of epochs reaches one of the milestones.

    Args:
        dataloader (DataLoader): Wrapped dataloader.
        milestones (Sequence[int]): Sequence of epoch indices.
        batch_size_manager (Union[BatchSizeManager, None]): If not None, a custom class which manages the batch size,
            which provides a getter and setter for the batch size. Default: None.
        max_batch_size (Union[int, None]): Upper limit for the batch size so that a batch of size max_batch_size fits
            in the memory. If None or greater than the lenght of the dataset wrapped by the dataloader, max_batch_size
            is set to `len(self.dataloader.dataset)`. Default: None.
        min_batch_size (int): Lower limit for the batch size which must be greater than 0. Default: 1.
        verbose (bool): If ``True``, prints a message to stdout for each update. Default: ``False``.

    Example:
        >>> dataloader = ...
        >>> # Assuming the base batch size is 10.
        >>> # bs = 10 if epoch < 30
        >>> # bs = 20 if 25 <= epoch < 80
        >>> # bs = 40 if 80 <= epoch
        >>> scheduler = MultiStepBS(dataloader, milestones=[25, 80], gamma=2.0)
        >>> for epoch in range(100):
        >>>     train(...)
        >>>     validate(...)
        >>>     scheduler.step()
    """

    def __init__(self, dataloader: DataLoader, milestones: Sequence[int], gamma: float = 2.0,
                 batch_size_manager: Union[BatchSizeManager, None] = None, max_batch_size: Union[int, None] = None,
                 min_batch_size: int = 1, verbose: bool = False):
        assert isinstance(milestones, (tuple, list))
        assert len(milestones) > 0 and all([x > 0 and isinstance(x, int) for x in milestones])
        assert gamma > 0.0
        # Gamma is expected to be greater than 1, but we do not forbid batch size decay.
        # We do not require milestones to be sorted. However, sorted looks better.
        self.milestones: Counter[int, int] = Counter(milestones)
        self.gamma: float = gamma
        super().__init__(dataloader, batch_size_manager, max_batch_size, min_batch_size, verbose)

    def get_bs(self) -> int:
        """ Returns the next batch size as an :class:`int`.

        It returns the current batch size times gamma each epoch a milestone is reached, otherwise it returns the
        current batch size. Beware that in the event of multiple milestones with the same value, the current batch size
        is multiplied with gamma multiple times.
        """
        if self.last_epoch not in self.milestones:
            return self.get_current_batch_size()
        return rint(self.get_current_batch_size() * self.gamma ** self.milestones[self.last_epoch])


class ConstantBS(BSScheduler):
    """ Increases the batch size by a constant multiplicative factor until the number of epochs reaches a pre-defined
    milestone. The batch size is multiplied by the constant factor during initialization and is multiplied again with
    the inverse of the constant factor when the milestone is reached.
    If the constant factor makes the batch size increase the image out of bounds, the constant factor is changed
    automatically such that the batch size remains within bounds.

    Args:
        dataloader (DataLoader): Wrapped dataloader.
        factor (float): The number we multiply the batch size until the milestone.
        milestone (int): The number of steps that the scheduler increases the learning rate. Default: 5.
        batch_size_manager (Union[BatchSizeManager, None]): If not None, a custom class which manages the batch size,
            which provides a getter and setter for the batch size. Default: None.
        max_batch_size (Union[int, None]): Upper limit for the batch size so that a batch of size max_batch_size fits
            in the memory. If None or greater than the lenght of the dataset wrapped by the dataloader, max_batch_size
            is set to `len(self.dataloader.dataset)`. Default: None.
        min_batch_size (int): Lower limit for the batch size which must be greater than 0. Default: 1.
        verbose (bool): If ``True``, prints a message to stdout for each update. Default: ``False``.

    Example:
        >>> dataloader = ...
        >>> # Assuming the base batch size is 10.
        >>> # bs = 50 if epoch == 0
        >>> # bs = 50 if epoch == 1
        >>> # bs = 50 if epoch == 2
        >>> # bs = 10 if epoch >= 3
        >>> scheduler = ConstantBS(dataloader, factor=5, milestone=3)
        >>> for epoch in range(100):
        >>>     train(...)
        >>>     validate(...)
        >>>     scheduler.step()
    """

    def __init__(self, dataloader: DataLoader, factor: float, milestone: int = 5,
                 batch_size_manager: Union[BatchSizeManager, None] = None, max_batch_size: Union[int, None] = None,
                 min_batch_size: int = 1, verbose: bool = False):
        assert isinstance(milestone, int) and milestone > 0
        assert factor > 0.0
        # Factor is expected to be greater than 1.0, as this should be a warmup process.
        self.factor: float = factor
        self.milestone: int = milestone
        super().__init__(dataloader, batch_size_manager, max_batch_size, min_batch_size, verbose)

    def get_bs(self) -> int:
        """ Returns the next batch size as an :class:`int`.

        The value of the batch size is changed once at initialization, when the batch size is multiplied with the given
        factor, and twice when the milestone is reached and the batch size is multiplied with the inverse of the given
        factor. The factor is adjusted during initialization such that it does not return a batch size out of bounds.
        """
        current_batch_size = self.get_current_batch_size()

        if self.last_epoch == 0:
            max_factor = self.max_batch_size / current_batch_size
            min_factor = self.min_batch_size / current_batch_size
            if self.factor > max_factor:
                self.factor = max_factor
            elif self.factor < min_factor:
                self.factor = min_factor
            return rint(current_batch_size * self.factor)

        if self.last_epoch != self.milestone:
            return current_batch_size

        self._finished = True  # My job is done.
        return rint(current_batch_size * (1.0 / self.factor))


class LinearBS(BSScheduler):
    """ Increases the batch size by a linearly changing small multiplicative factor until the number of epochs reaches
    a pre-defined milestone.

    Args:
        dataloader (DataLoader): Wrapped dataloader.
        start_factor (float): The number we multiply the batch size in the first epoch. The multiplication factor
            changes towards end_factor in the following epochs. Default: 3.0.
        end_factor (float): The number we multiply the batch size at the end of the linear changing process.
                Default: 1.0.
        milestone (int): The number of steps that the scheduler increases the learning rate. Default: 5.
        batch_size_manager (Union[BatchSizeManager, None]): If not None, a custom class which manages the batch size,
            which provides a getter and setter for the batch size. Default: None.
        max_batch_size (Union[int, None]): Upper limit for the batch size so that a batch of size max_batch_size fits
            in the memory. If None or greater than the lenght of the dataset wrapped by the dataloader, max_batch_size
            is set to `len(self.dataloader.dataset)`. Default: None.
        min_batch_size (int): Lower limit for the batch size which must be greater than 0. Default: 1.
        verbose (bool): If ``True``, prints a message to stdout for each update. Default: ``False``.

    Example:
        >>> dataloader = ...
        >>> # Assuming the base batch size is 10.
        >>> # bs = 60 if epoch == 0
        >>> # bs = 50 if epoch == 1
        >>> # bs = 40 if epoch == 2
        >>> # bs = 30 if epoch == 3
        >>> # bs = 20 if epoch == 4
        >>> # bs = 10 if epoch >= 5
        >>> scheduler = LinearBS(dataloader, start_factor=6.0, end_factor=1.0, milestone=5)
        >>> for epoch in range(100):
        >>>     train(...)
        >>>     validate(...)
        >>>     scheduler.step()
    """

    def __init__(self, dataloader: DataLoader, start_factor: float = 3.0, end_factor: float = 1.0, milestone: int = 5,
                 batch_size_manager: Union[BatchSizeManager, None] = None, max_batch_size: Union[int, None] = None,
                 min_batch_size: int = 1, verbose: bool = False):
        assert isinstance(milestone, int) and milestone > 0
        assert start_factor > 0.0 and end_factor > 0.0
        # Both start_factor and end_factor are expected to be greater than 1.0, with start_factor > end_factor, as this
        # should be a warmup process. But we do not forbid any other sound combinations.
        self.start_factor: float = start_factor
        self.end_factor: float = end_factor
        self.milestone: int = milestone
        super().__init__(dataloader, batch_size_manager, max_batch_size, min_batch_size, verbose)

    def get_bs(self) -> int:
        """ Returns the next batch size as an :class:`int`.

        The current batch size is multiplied by the linear changing factor, starting from start_factor to end_factor.
        After the milestone is reached, the batch size is not changed anymore.
        """
        current_batch_size = self.get_current_batch_size()

        if self.last_epoch > self.milestone:
            self._finished = True  # My job is done.
            return current_batch_size

        if self.last_epoch == 0:
            return rint(current_batch_size * self.start_factor)

        value_range = self.end_factor - self.start_factor
        return rint(current_batch_size * (
                1.0 + value_range / (self.milestone * self.start_factor + (self.last_epoch - 1) * value_range)))


class ExponentialBS(BSScheduler):
    """ Increases the batch size by a gamma every epoch.

    Args:
        dataloader (DataLoader): Wrapped dataloader.
        gamma (float): Multiplicative factor of batch size growth.
        batch_size_manager (Union[BatchSizeManager, None]): If not None, a custom class which manages the batch size,
            which provides a getter and setter for the batch size. Default: None.
        max_batch_size (Union[int, None]): Upper limit for the batch size so that a batch of size max_batch_size fits
            in the memory. If None or greater than the lenght of the dataset wrapped by the dataloader, max_batch_size
            is set to `len(self.dataloader.dataset)`. Default: None.
        min_batch_size (int): Lower limit for the batch size which must be greater than 0. Default: 1.
        verbose (bool): If ``True``, prints a message to stdout for each update. Default: ``False``.

    Example:
        >>> dataloader = ...
        >>> # Assuming the base batch size is 10.
        >>> # bs = 10 if epoch == 0
        >>> # bs = 11 if epoch == 1
        >>> # bs = 12 if epoch == 2
        >>> # bs = 13 if epoch == 3
        >>> # ...
        >>> scheduler = ExponentialBS(dataloader, gamma=1.1)
        >>> for epoch in range(100):
        >>>     train(...)
        >>>     validate(...)
        >>>     scheduler.step()
    """

    def __init__(self, dataloader: DataLoader, gamma: float, batch_size_manager: Union[BatchSizeManager, None] = None,
                 max_batch_size: Union[int, None] = None, min_batch_size: int = 1, verbose: bool = False):
        assert gamma > 0.0
        # Gamma is expected to be greater than 1.0 for batch size growth. It can be lower than 1.0 for batch size decay.
        self.gamma: float = gamma
        super().__init__(dataloader, batch_size_manager, max_batch_size, min_batch_size, verbose)

    def get_bs(self) -> int:
        """ Returns the next batch size as an :class:`int`.
        The current batch size is multiplied by gamma each epoch except the first one.
        """
        current_batch_size = self.get_current_batch_size()

        if self.last_epoch == 0:
            return current_batch_size

        return rint(current_batch_size * self.gamma)


class SequentialBS(BSScheduler):
    """ Similar to torch.optim.lr_scheduler.SequentialLR. Receives a sequence of schedulers and calls them sequentially
    given the milestone points that reflect which scheduler is supposed to be called at a fiven epoch

    Args:
        dataloader (DataLoader): Wrapped dataloader.
        schedulers (Sequence[BSScheduler]): Sequence of batch size schedulers. We expect the first scheduler to have
            been initialized first.
        milestones (Sequence[int]): Sequence of integers that reflects the milestone points. Must be sorted in a
            non-descending order.
        batch_size_manager (Union[BatchSizeManager, None]): If not None, a custom class which manages the batch size,
            which provides a getter and setter for the batch size. Default: None.
        max_batch_size (Union[int, None]): Upper limit for the batch size so that a batch of size max_batch_size fits
            in the memory. If None or greater than the lenght of the dataset wrapped by the dataloader, max_batch_size
            is set to `len(self.dataloader.dataset)`. Default: None.
        min_batch_size (int): Lower limit for the batch size which must be greater than 0. Default: 1.
        verbose (bool): Does nothing.

    Example:
        >>> dataloader = ...
        >>> # Assuming the base batch size is 10.
        >>> # bs = 100 if epoch == 0
        >>> # bs = 100 if epoch == 1
        >>> # bs = 100 if epoch == 2
        >>> # bs = 100 if epoch == 3
        >>> # bs = 10 if epoch == 4
        >>> # bs = 11 if epoch == 5
        >>> # ...
        >>> scheduler1 = ConstantBS(dataloader, factor=10, milestone=4)
        >>> scheduler2 = ExponentialBS(dataloader, gamma=1.1)
        >>> scheduler = SequentialBS(dataloader, schedulers=[scheduler1, scheduler2], milestones=[5])
        >>> for epoch in range(100):
        >>>     train(...)
        >>>     validate(...)
        >>>     scheduler.step()
    """

    def __init__(self, dataloader: DataLoader, schedulers: Sequence[BSScheduler], milestones=Sequence[int],
                 batch_size_manager: Union[BatchSizeManager, None] = None, max_batch_size: Union[int, None] = None,
                 min_batch_size: int = 1, verbose: bool = False):
        # Doing the initialization first, all checks later. In the initial step called in constructor we do nothing.
        super().__init__(dataloader, batch_size_manager, max_batch_size, min_batch_size, verbose)

        assert isinstance(schedulers, (tuple, list)) and len(schedulers) >= 2 and all(
            [isinstance(x, BSScheduler) for x in schedulers])
        assert isinstance(milestones, (tuple, list)) and len(milestones) >= 1 and all(
            [isinstance(x, int) for x in milestones]) and milestones[0] > 0
        assert all([milestones[i] >= milestones[i - 1] for i in range(1, len(milestones))]), \
            f"Milestones must be sorted, are {milestones}"

        if len(milestones) != len(schedulers) - 1:
            raise ValueError(f"SequentialBS expects the number of schedulers provided to be one more than the number "
                             f"of milestone points, but got {len(schedulers)} and the number of milestones is "
                             f"{len(milestones)}")
        for i in range(len(schedulers)):
            if schedulers[i].dataloader != self.dataloader:
                raise ValueError(f"SequentialBS expects all schedulers to belong to the same dataloader, but got "
                                 f"scheduler at index {i} to be different than the dataloader passed in.")
            if not isinstance(schedulers[i].batch_size_manager, type(self.batch_size_manager)):
                raise ValueError(f"SequentialBS expects all schedulers to have the same batch size manager, but got "
                                 f"scheduler at index {i} to have a different batch size manager. Expected type of "
                                 f"batch size manager: {type(self.batch_size_manager).__name__}, got: "
                                 f"{type(schedulers[i].batch_size_manager).__name__}.")
            if schedulers[i].max_batch_size != self.max_batch_size:
                raise ValueError(f"SequentialBS expects all schedulers to have the same maximum batch size, but got "
                                 f"scheduler at index {i} to have a different maximum batch size. Expected "
                                 f"{self.max_batch_size}, got {schedulers[i].max_batch_size}.")
            if schedulers[i].min_batch_size != self.min_batch_size:
                raise ValueError(f"SequentialBS expects all schedulers to have the same minimum batch size, but got "
                                 f"scheduler at index {i} to have a different minimum batch size. Expected "
                                 f"{self.min_batch_size}, got {schedulers[i].min_batch_size}.")
            # Undoing the steps done by the schedulers.
            schedulers[i]._last_bs = self.dataloader._base_batch_size
            schedulers[i].last_epoch -= 1

        self.set_batch_size(self.dataloader._base_batch_size)  # Set the batch size back to initial value.

        self.schedulers: Tuple[BSScheduler, ...] = tuple(schedulers)
        self.milestones: Tuple[int, ...] = tuple(milestones)
        # Do the initial step again, but only for the first scheduler.
        self.schedulers[0].step()

    def finished(self) -> bool:
        """ Returns True if all the schedulers have already finished their job or have exceeded the minimum or maximum
        batch size. Otherwise, returns False.
        """
        # The last milestone was reached and the last scheduler is finished.
        self._finished = self.last_epoch > self.milestones[-1] and self.schedulers[-1].finished()
        return self._finished

    def step(self):
        """ Performs the step method for each scheduler until a milestone point is reached and a new scheduler is to be
        used. The new scheduler is used as if it is called for the first time.
        """
        self.last_epoch += 1  # We still increase last_epoch, even though the scheduler has finished its job. It should
        # not really matter.
        if self.last_epoch == 0 or self.finished():
            return
        i = bisect_right(self.milestones, self.last_epoch)
        scheduler = self.schedulers[i]
        if i > 0 and self.milestones[i - 1] == self.last_epoch:
            scheduler.last_epoch = 0
        if not scheduler.finished():
            scheduler.step()
            self._last_bs = scheduler.get_last_bs()

    def state_dict(self) -> dict:
        """ Returns the state of the scheduler as a :class:`dict`.

        It contains an entry for every variable in self.__dict__ which is not the dataloader. The wrapped scheduler
        stares will also be saved.
        """
        state_dict = {key: value for key, value in self.__dict__.items() if key not in ('dataloader', 'schedulers')}
        state_dict['schedulers'] = [None] * len(self.schedulers)

        for i, s in enumerate(self.schedulers):
            state_dict['schedulers'][i] = s.state_dict()

        return state_dict

    def load_state_dict(self, state_dict: dict):
        """ Loads the schedulers state.

        Args:
            state_dict (dict): scheduler state. Should be an object returned from a call to :meth:`state_dict`.
        """
        schedulers = state_dict.pop('schedulers')
        self.__dict__.update(state_dict)

        state_dict['schedulers'] = schedulers
        for i, s in enumerate(schedulers):
            self.schedulers[i].load_state_dict(s)


class PolynomialBS(BSScheduler):
    """ Increases the batch size using a polynomial function in the given total_iters. Unlike
    torch.optim.lr_scheduler.PolynomialLR whose polynomial factor decays from 1.0 to 0.0, in this case the polynomial
    factor increases from 1.0 to 2.0 ** power.

    Args:
        dataloader (DataLoader): Wrapped dataloader.
        total_iters (int): The number of steps that the scheduler increases the batch size.
        power (float): The power of the polynomial. Default: 1.0.
        batch_size_manager (Union[BatchSizeManager, None]): If not None, a custom class which manages the batch size,
            which provides a getter and setter for the batch size. Default: None.
        max_batch_size (Union[int, None]): Upper limit for the batch size so that a batch of size max_batch_size fits
            in the memory. If None or greater than the lenght of the dataset wrapped by the dataloader, max_batch_size
            is set to `len(self.dataloader.dataset)`. Default: None.
        min_batch_size (int): Lower limit for the batch size which must be greater than 0. Default: 1.
        verbose (bool): If ``True``, prints a message to stdout for each update. Default: ``False``.

    Example:
        >>> dataloader = ...
        >>> # Assuming the base batch size is 10.
        >>> # bs = 10 if epoch == 0
        >>> # bs = 10 * 1.25 if epoch == 1
        >>> # bs = 12 * 1.33 if epoch == 2
        >>> # bs = 16 * 1.50 if epoch == 3
        >>> # bs = 24 * 2.00 if epoch == 4
        >>> # bs = 48 if epoch >= 5
        >>> scheduler = PolynomialBS(dataloader, total_iters=5, power=1.0)
        >>> for epoch in range(100):
        >>>     train(...)
        >>>     validate(...)
        >>>     scheduler.step()
    """

    def __init__(self, dataloader: DataLoader, total_iters: int, power: float = 1.0,
                 batch_size_manager: Union[BatchSizeManager, None] = None, max_batch_size: Union[int, None] = None,
                 min_batch_size: int = 1, verbose: bool = False):
        assert isinstance(total_iters, int) and total_iters > 1

        self.total_iters: int = total_iters
        self.power: float = power
        super().__init__(dataloader, batch_size_manager, max_batch_size, min_batch_size, verbose)

    def get_bs(self) -> int:
        """ Returns the next batch size as an :class:`int`.
        From epoch 1 to total_iters - 1, the current batch size is multiplied by an increasing polynomial factor.
        """
        current_batch_size = self.get_current_batch_size()

        if self.last_epoch == 0 or self.last_epoch >= self.total_iters:
            self._finished = self.last_epoch > self.total_iters
            return current_batch_size

        factor = ((1.0 - (self.last_epoch - 1) / self.total_iters) / (
                1.0 - self.last_epoch / self.total_iters)) ** self.power
        return rint(current_batch_size * factor)


class CosineAnnealingBS(BSScheduler):
    """ Similar to torch.optim.lr_scheduler.CosineAnnealingLR which implements the cosine annealing part of
    `SGDR: Stochastic Gradient Descent with Warm Restarts`_. For batch size, we perform reverse annealing and instead
    of decreasing the batch size to min_batch_size we increase it to max_batch_size.

    Args:
        dataloader (DataLoader): Wrapped dataloader.
        total_iters (int): The number of steps that the scheduler increases the batch size.
        batch_size_manager (Union[BatchSizeManager, None]): If not None, a custom class which manages the batch size,
            which provides a getter and setter for the batch size. Default: None.
        max_batch_size (Union[int, None]): Upper limit for the batch size so that a batch of size max_batch_size fits
            in the memory. If None or greater than the lenght of the dataset wrapped by the dataloader, max_batch_size
            is set to `len(self.dataloader.dataset)`. Default: None.
        min_batch_size (int): Lower limit for the batch size which must be greater than 0. Default: 1.
        verbose (bool): If ``True``, prints a message to stdout for each update. Default: ``False``.

    .. _SGDR\: Stochastic Gradient Descent with Warm Restarts: https://arxiv.org/abs/1608.03983

    Example:
        >>> dataloader = ...
        >>> # Assuming the base batch size is 10.
        >>> # bs = 10 if epoch % 10 == 0
        >>> # bs = 19 if epoch % 10 == 1
        >>> # bs = 41 if epoch % 10 == 2
        >>> # bs = 69 if epoch % 10 == 3
        >>> # bs = 91 if epoch % 10 == 4
        >>> # bs = 100 if epoch % 10 == 5
        >>> # bs = 91 if epoch % 10 == 6
        >>> # bs = 67 if epoch % 10 == 7
        >>> # bs = 37 if epoch % 10 == 8
        >>> # bs = 13 if epoch % 10 == 9
        >>> scheduler = CosineAnnealingBS(dataloader, total_iters=5, power=1.0)
        >>> for epoch in range(100):
        >>>     train(...)
        >>>     validate(...)
        >>>     scheduler.step()
    """

    def __init__(self, dataloader: DataLoader, total_iters: int, power: float = 1.0,
                 batch_size_manager: Union[BatchSizeManager, None] = None, max_batch_size: Union[int, None] = None,
                 min_batch_size: int = 1, verbose: bool = False):
        assert isinstance(total_iters, int) and total_iters > 1

        self.total_iters: int = total_iters
        super().__init__(dataloader, batch_size_manager, max_batch_size, min_batch_size, verbose)

    def get_bs(self) -> int:
        """ Returns the next batch size as an :class:`int`.

        Increases the batch size from base batch size to maximum batch size following a cyclic cosine curve. The
        implementation is equivalent to torch.optim.lr_scheduler.CosineAnnealingLR.get_lr() and instead of `eta_min` we
        use `self.max_batch_size` and we clip the values to be within bounds.
        """
        current_batch_size = self.get_current_batch_size()
        base_batch_size = self.dataloader._base_batch_size

        if self.last_epoch == 0:
            return current_batch_size

        if self.last_epoch == 1 and base_batch_size == current_batch_size:
            new_bs = self.max_batch_size + (base_batch_size - self.max_batch_size) * (
                    1 + math.cos(self.last_epoch * math.pi / self.total_iters)) / 2
        elif (self.last_epoch - 1 - self.total_iters) % (2 * self.total_iters) == 0:
            new_bs = current_batch_size + (base_batch_size - self.max_batch_size) * (
                    1 - math.cos(math.pi / self.total_iters)) / 2
        else:
            new_bs = (1 + math.cos(math.pi * self.last_epoch / self.total_iters)) / (
                    1 + math.cos(math.pi * (self.last_epoch - 1) / self.total_iters)) * (
                             current_batch_size - self.max_batch_size) + self.max_batch_size

        return clip(rint(new_bs), min=base_batch_size, max=self.max_batch_size)


class ChainedBSScheduler(BSScheduler):
    """ Similar to torch.optim.lr_scheduler.ChainedScheduler.
    Chains a list of batch size schedulers. It takes the list of batch size schedulers and performs consucutive
    step() functions belonging to them by just one call

    Args:
        schedulers (Sequence[BSScheduler]): List of chained schedulers.
    Example:
        >>> dataloader = ...
        >>> # Assuming the base batch size is 10.
        >>> # bs = 100 if epoch == 0
        >>> # bs = 110 if epoch == 1
        >>> # bs = 121 if epoch == 2
        >>> # bs = 133 if epoch == 3
        >>> # bs = 14 if epoch == 4
        >>> # bs = 15 if epoch == 5
        >>> # bs = 16 if epoch == 6
        >>> # bs = 18 if epoch == 7
        >>> # ...
        >>> scheduler1 = ConstantBS(dataloader, factor=10, milestone=4)
        >>> scheduler2 = ExponentialBS(dataloader, gamma=1.1)
        >>> scheduler = ChainedBSScheduler([scheduler1, scheduler2])
        >>> for epoch in range(100):
        >>>     train(...)
        >>>     validate(...)
        >>>     scheduler.step()
    """

    def __init__(self, schedulers: Sequence[BSScheduler]):
        assert isinstance(schedulers, (tuple, list)) and len(schedulers) > 1 and all(
            [isinstance(x, BSScheduler) for x in schedulers])

        dataloader: DataLoader = schedulers[0].dataloader
        batch_size_manger: BatchSizeManager = schedulers[0].batch_size_manager
        for i in range(1, len(schedulers)):
            if schedulers[i].dataloader != dataloader:
                raise ValueError(f"ChainedBSScheduler expects all schedulers to belong to the same dataloader, but got "
                                 f"scheduler at index {i} to be different than the scheduler at index 0.")
            if not isinstance(schedulers[i].batch_size_manager, type(batch_size_manger)):
                raise ValueError(
                    f"ChainedBSScheduler expects all schedulers to have the same batch size manager, but got "
                    f"scheduler at index {i} to have a different batch size manager. Expected type of "
                    f"batch size manager: {type(batch_size_manger).__name__}, got: "
                    f"{type(schedulers[i].batch_size_manager).__name__}.")
            # We do not require equality for min_batch_size and max_batch_size, but maybe we should.

        self.dataloader: DataLoader = dataloader
        self.batch_size_manager: BatchSizeManager = batch_size_manger
        self.schedulers: Tuple[BSScheduler, ...] = tuple(schedulers)
        self._last_bs: int = self.schedulers[-1].get_last_bs()
        self.max_batch_size: int = self.schedulers[-1].max_batch_size
        self.min_batch_size: int = self.schedulers[-1].min_batch_size
        self.get_current_batch_size: Callable[[], int] = self.schedulers[-1].get_current_batch_size
        self._finished: bool = False

    def step(self):
        for scheduler in self.schedulers:
            scheduler.step()
        self._last_bs = self.schedulers[-1].get_last_bs()

    def finished(self) -> bool:
        """ Returns True if all the schedulers have already finished their job or have exceeded the minimum or maximum
        batch size. Otherwise, returns False.
        """
        self._finished = all([x.finished() for x in self.schedulers])
        return self._finished

    def state_dict(self) -> dict:
        """ Returns the state of the scheduler as a :class:`dict`.

        It contains an entry for every variable in self.__dict__ which is not the dataloader. The wrapped scheduler
        stares will also be saved.
        """
        state_dict = {key: value for key, value in self.__dict__.items() if key not in ('dataloader', 'schedulers')}
        state_dict['schedulers'] = [None] * len(self.schedulers)

        for i, s in enumerate(self.schedulers):
            state_dict['schedulers'][i] = s.state_dict()

        return state_dict

    def load_state_dict(self, state_dict: dict):
        """ Loads the schedulers state.

        Args:
            state_dict (dict): scheduler state. Should be an object returned from a call to :meth:`state_dict`.
        """
        schedulers = state_dict.pop('schedulers')
        self.__dict__.update(state_dict)

        state_dict['schedulers'] = schedulers
        for i, s in enumerate(schedulers):
            self.schedulers[i].load_state_dict(s)

class IncreaseBSOnPlateau(BSScheduler):
    """ The inverse of torch.optim.lr_scheduler.ReduceLROnPlateau.
    Increases the batch size when a metric has stopped improving. Models often benefit from increasing the batch size
    by a factor once the learning stagnates. This scheduler receives a metric value and if no improvement is seen for a
    given number of epochs, the batch size is increased.
    Unfortunately, this class is not compatible with the other batch size schedulers as its step() function needs to
    receive the metric value.
    TODO: make IncreaseBSOnPlateau combine well with the other BS schedulers.

    Args:
        dataloader (DataLoader): Wrapped dataloader.
        mode (str): One of `min`, `max`. In `min` mode, the batch size will be increased when the metric value has
            stopped decreasing; in `max` mode, the batch size will be increased when the metric value has stopped
            increasing. Default: 'min'.
        factor (float): Factor by which the batch size will be increased. Default: 2.0.
        patience (int): Number of epochs with no improvement after which the batch size will be increased. Default: 10.
        threshold (float): Threshold for measuring the new metric value, to only focus on significant changes.
            Default: 1e-4.
        threshold_mode (str): One of `rel`, `abs`. In `rel` mode, dynamic_threshold = best * ( 1 + threshold ) in 'max'
            mode or best * ( 1 - threshold ) in `min` mode. In `abs` mode, dynamic_threshold = best + threshold in 'max'
            mode or best - threshold in `min` mode. Default: 'rel'.
        cooldown (int): Number of epochs to wait before resuming normal operation after the batch size has been reduced.
            Default: 0.
        batch_size_manager (Union[BatchSizeManager, None]): If not None, a custom class which manages the batch size,
            which provides a getter and setter for the batch size. Default: None.
        max_batch_size (Union[int, None]): Upper limit for the batch size so that a batch of size max_batch_size fits
            in the memory. If None or greater than the lenght of the dataset wrapped by the dataloader, max_batch_size
            is set to `len(self.dataloader.dataset)`. Default: None.
        min_batch_size (int): Lower limit for the batch size which must be greater than 0. Default: 1.
        verbose (bool): If ``True``, prints a message to stdout for each update. Default: ``False``.

    Example:
        >>> dataloader = ...
        >>> scheduler = IncreaseBSOnPlateau(dataloader)
        >>> for epoch in range(100):
        >>>     train(...)
        >>>     val_loss = validate(...)
        >>>     scheduler.step(val_loss)
    """
    def __init__(self, dataloader: DataLoader, mode: str = 'min', factor: float = 2.0, patience: int = 10,
                 threshold: float = 1e-4, threshold_mode: str = 'rel', cooldown: int = 0,
                 batch_size_manager: Union[BatchSizeManager, None] = None, max_batch_size: Union[int, None] = None,
                 min_batch_size: int = 1, verbose: bool = False):
        super().__init__(dataloader, batch_size_manager, max_batch_size, min_batch_size, verbose)
        assert isinstance(factor, float) and factor != 1.0 and factor >= 0.0
        # Factor is expected to be greater than 1, but we do not forbid batch size decay.
        assert isinstance(patience, int) and patience >= 0
        assert isinstance(threshold, float) and threshold > 0.0
        assert isinstance(cooldown, int) and cooldown >= 0

        self.mode: str = mode
        self.factor: float = factor
        self.patience: int = patience
        self.threshold: float = threshold
        self.threshold_mode: str = threshold_mode
        self.cooldown: int = cooldown
        self.best = None
        self.num_bad_epochs = None
        self.mode_worse = torch.inf if mode == 'min' else -torch.inf
        self.last_epoch = 0

        self._init_is_better(mode, threshold_mode)
        self._reset()

    def in_cooldown(self):
        return self.cooldown_counter > 0

    def _reset(self):
        """ Resets num_bad_epochs counter and cooldown counter."""
        self.best = self.mode_worse
        self.cooldown_counter = 0
        self.num_bad_epochs = 0

    @staticmethod
    def is_better_min_rel(a, best, threshold):
        return a < best * (1.0 - threshold)
    @staticmethod
    def is_better_min_abs(a, best, threshold):
        return a < best - threshold

    @staticmethod
    def is_better_max_rel(a, best, threshold):
        return a > best * (1.0 + threshold)

    @staticmethod
    def is_better_max_abs(a, best, threshold):
        return a > best + threshold


    def _init_is_better(self, mode, threshold_mode):
        if mode not in ('min', 'max'):
            raise ValueError(f'Mode {mode} is unknown!')
        if threshold_mode not in ('rel', 'abs'):
            raise ValueError(f'Threshold mode {mode} is unknown!')

        if mode == 'min' and threshold_mode == 'rel':
            self.is_better = self.is_better_min_rel
        elif mode == 'min' and threshold_mode == 'abs':
            self.is_better = self.is_better_min_abs
        elif mode == 'max' and threshold_mode == 'rel':
            self.is_better = self.is_better_max_rel
        else:  # mode == 'min' and threshold_mode == 'abs':
            self.is_better = self.is_better_max_abs


    def get_bs(self, metric) -> int:
        """
        TODO
        """
        current = float(metric)
        current_batch_size = self.get_current_batch_size()
        if self.is_better(current, self.best, self.threshold):
            self.best = current
            self.num_bad_epochs = 0
        else:
            self.num_bad_epochs += 1

        if self.in_cooldown():
            self.cooldown_counter -= 0
            self.num_bad_epochs = 0  # ignore any bad epochs in cooldown.

        if self.num_bad_epochs > self.patience:
            self.cooldown_counter = self.cooldown
            self.num_bad_epochs = 0
            return rint(current_batch_size * self.factor)

        return current_batch_size


    def step(self, metric: float):
        if self.finished():
            return  # Stops doing work if already finished.

        previous_batch_size = self.get_current_batch_size()
        self.last_epoch += 1
        new_bs = self.get_bs(metric)
        if not self.min_batch_size <= new_bs <= self.max_batch_size:
            self._finished = True
            new_bs = clip(new_bs, min=self.min_batch_size, max=self.max_batch_size)
        self.set_batch_size(new_bs)
        if new_bs != previous_batch_size:
            self.print_bs(new_bs)
        self._last_bs = new_bs

    def load_state_dict(self, state_dict: dict):
        self.__dict__.update(state_dict)
        self._init_is_better(self.mode, self.threshold_mode)

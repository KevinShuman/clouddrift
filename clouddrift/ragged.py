"""
Transformational and inquiry functions for ragged arrays.
"""

import warnings
from collections.abc import Callable, Iterable
from concurrent import futures
from datetime import timedelta

import numpy as np
import pandas as pd
import xarray as xr


def apply_ragged(
    func: callable,
    arrays: list[np.ndarray | xr.DataArray] | np.ndarray | xr.DataArray,
    rowsize: list[int] | np.ndarray[int] | xr.DataArray,
    *args: tuple,
    rows: int | Iterable[int] = None,
    axis: int = 0,
    executor: futures.Executor = futures.ThreadPoolExecutor(max_workers=None),
    **kwargs: dict,
) -> tuple[np.ndarray] | np.ndarray:
    """Apply a function to a ragged array.

    The function ``func`` will be applied to each contiguous row of ``arrays`` as
    indicated by row sizes ``rowsize``. The output of ``func`` will be
    concatenated into a single ragged array.

    You can pass ``arrays`` as NumPy arrays or xarray DataArrays, however,
    the result will always be a NumPy array. Passing ``rows`` as an integer or
    a sequence of integers will make ``apply_ragged`` process and return only
    those specific rows, and otherwise, all rows in the input ragged array will
    be processed. Further, you can use the ``axis`` parameter to specify the
    ragged axis of the input array(s) (default is 0).

    By default this function uses ``concurrent.futures.ThreadPoolExecutor`` to
    run ``func`` in multiple threads. The number of threads can be controlled by
    passing the ``max_workers`` argument to the executor instance passed to
    ``apply_ragged``. Alternatively, you can pass the ``concurrent.futures.ProcessPoolExecutor``
    instance to use processes instead. Passing alternative (3rd party library)
    concurrent executors may work if they follow the same executor interface as
    that of ``concurrent.futures``, however this has not been tested yet.

    Parameters
    ----------
    func : callable
        Function to apply to each row of each ragged array in ``arrays``.
    arrays : list[np.ndarray] or np.ndarray or xr.DataArray
        An array or a list of arrays to apply ``func`` to.
    rowsize : list[int] or np.ndarray[int] or xr.DataArray[int]
        List of integers specifying the number of data points in each row.
    *args : tuple
        Additional arguments to pass to ``func``.
    rows : int or Iterable[int], optional
        The row(s) of the ragged array to apply ``func`` to. If ``rows`` is
        ``None`` (default), then ``func`` will be applied to all rows.
    axis : int, optional
        The ragged axis of the input arrays. Default is 0.
    executor : concurrent.futures.Executor, optional
        Executor to use for concurrent execution. Default is ``ThreadPoolExecutor``
        with the default number of ``max_workers``.
        Another supported option is ``ProcessPoolExecutor``.
    **kwargs : dict
        Additional keyword arguments to pass to ``func``.

    Returns
    -------
    out : tuple[np.ndarray] or np.ndarray
        Output array(s) from ``func``.

    Examples
    --------

    Using ``velocity_from_position`` with ``apply_ragged``, calculate the velocities of
    multiple particles, the coordinates of which are found in the ragged arrays x, y, and t
    that share row sizes 2, 3, and 4:

    >>> from clouddrift.kinematics import velocity_from_position
    >>> rowsize = [2, 3, 4]
    >>> x = np.array([1, 2, 10, 12, 14, 30, 33, 36, 39])
    >>> y = np.array([0, 1, 2, 3, 4, 5, 6, 7, 8])
    >>> t = np.array([1, 2, 1, 2, 3, 1, 2, 3, 4])
    >>> u1, v1 = apply_ragged(velocity_from_position, [x, y, t], rowsize, coord_system="cartesian")
    >>> u1
    array([1., 1., 2., 2., 2., 3., 3., 3., 3.])
    >>> v1
    array([1., 1., 1., 1., 1., 1., 1., 1., 1.])

    To apply ``func`` to only a subset of rows, use the ``rows`` argument:

    >>> u1, v1 = apply_ragged(velocity_from_position, [x, y, t], rowsize, rows=0, coord_system="cartesian")
    >>> u1
    array([1., 1.])
    >>> v1
    array([1., 1.])
    >>> u1, v1 = apply_ragged(velocity_from_position, [x, y, t], rowsize, rows=[0, 1], coord_system="cartesian")
    >>> u1
    array([1., 1., 2., 2., 2.])
    >>> v1
    array([1., 1., 1., 1., 1.])

    Raises
    ------
    ValueError
        If the sum of ``rowsize`` does not equal the length of ``arrays``.
    IndexError
        If empty ``arrays``.
    """
    # make sure the arrays is iterable
    if not isinstance(arrays, (list, tuple)):
        arrays = [arrays]
    # validate rowsize
    for arr in arrays:
        if not np.sum(rowsize) == arr.shape[axis]:
            raise ValueError("The sum of rowsize must equal the length of arr.")

    # split the array(s) into rows
    arrays = [unpack(np.array(arr), rowsize, rows, axis) for arr in arrays]
    iter = [[arrays[i][j] for i in range(len(arrays))] for j in range(len(arrays[0]))]

    # parallel execution
    res = [executor.submit(func, *x, *args, **kwargs) for x in iter]
    res = [r.result() for r in res]

    # Concatenate the outputs.

    # The following wraps items in a list if they are not already iterable.
    res = [item if isinstance(item, Iterable) else [item] for item in res]

    # np.concatenate can concatenate along non-zero axis iff the length of
    # arrays to be concatenated is > 1. If the length is 1, for example in the
    # case of func that reduces over the non-ragged axis, we can only
    # concatenate along axis 0.
    if isinstance(res[0], tuple):  # more than 1 parameter
        outputs = []
        for i in range(len(res[0])):  # iterate over each result variable
            # If we have multiple outputs and func is a reduction function,
            # we now here have a list of scalars. We need to wrap them in a
            # list to concatenate them.
            result = [r[i] if isinstance(r[i], Iterable) else [r[i]] for r in res]
            if len(result[0]) > 1:
                # Arrays to concatenate are longer than 1 element, so we can
                # concatenate along the non-zero axis.
                outputs.append(np.concatenate(result, axis=axis))
            else:
                # Arrays to concatenate are 1 element long, so we can only
                # concatenate along axis 0.
                outputs.append(np.concatenate(result))
        return tuple(outputs)
    else:
        if len(res[0]) > 1:
            # Arrays to concatenate are longer than 1 element, so we can
            # concatenate along the non-zero axis.
            return np.concatenate(res, axis=axis)
        else:
            # Arrays to concatenate are 1 element long, so we can only
            # concatenate along axis 0.
            return np.concatenate(res)


def chunk(
    x: list | np.ndarray | xr.DataArray | pd.Series,
    length: int,
    overlap: int = 0,
    align: str = "start",
) -> np.ndarray:
    """Divide an array ``x`` into equal chunks of length ``length``. The result
    is a 2-dimensional NumPy array of shape ``(num_chunks, length)``. The resulting
    number of chunks is determined based on the length of ``x``, ``length``,
    and ``overlap``.

    ``chunk`` can be combined with :func:`apply_ragged` to chunk a ragged array.

    Parameters
    ----------
    x : list or array-like
        Array to divide into chunks.
    length : int
        The length of each chunk.
    overlap : int, optional
        The number of overlapping array elements across chunks. The default is 0.
        Must be smaller than ``length``. For example, if ``length`` is 4 and
        ``overlap`` is 2, the chunks of ``[0, 1, 2, 3, 4, 5]`` will be
        ``np.array([[0, 1, 2, 3], [2, 3, 4, 5]])``. Negative overlap can be used
        to offset chunks by some number of elements. For example, if ``length``
        is 2 and ``overlap`` is -1, the chunks of ``[0, 1, 2, 3, 4, 5]`` will
        be ``np.array([[0, 1], [3, 4]])``.
    align : str, optional ["start", "middle", "end"]
        If the remainder of the length of ``x`` divided by the chunk ``length`` is a number
        N different from zero, this parameter controls which part of the array will be kept
        into the chunks. If ``align="start"``, the elements at the beginning of the array
        will be part of the chunks and N points are discarded at the end. If `align="middle"`,
        floor(N/2) and ceil(N/2) elements will be discarded from the beginning and the end
        of the array, respectively. If ``align="end"``, the elements at the end of the array
        will be kept, and the `N` first elements are discarded. The default is "start".

    Returns
    -------
    np.ndarray
        2-dimensional array of shape ``(num_chunks, length)``.

    Examples
    --------

    Chunk a simple list; this discards the end elements that exceed the last chunk:

    >>> chunk([1, 2, 3, 4, 5], 2)
    array([[1, 2],
           [3, 4]])

    To discard the starting elements of the array instead, use ``align="end"``:

    >>> chunk([1, 2, 3, 4, 5], 2, align="end")
    array([[2, 3],
           [4, 5]])

    To center the chunks by discarding both ends of the array, use ``align="middle"``:

    >>> chunk([1, 2, 3, 4, 5, 6, 7, 8], 3, align="middle")
    array([[2, 3, 4],
           [5, 6, 7]])

    Specify ``overlap`` to get overlapping chunks:

    >>> chunk([1, 2, 3, 4, 5], 2, overlap=1)
    array([[1, 2],
           [2, 3],
           [3, 4],
           [4, 5]])

    Use ``apply_ragged`` to chunk a ragged array by providing the row sizes;
    notice that you must pass the array to chunk as an array-like, not a list:

    >>> x = np.array([1, 2, 3, 4, 5])
    >>> rowsize = [2, 1, 2]
    >>> apply_ragged(chunk, x, rowsize, 2)
    array([[1, 2],
           [4, 5]])

    Raises
    ------
    ValueError
        If ``length < 0``.
    ValueError
        If ``align not in ["start", "middle", "end"]``.
    ZeroDivisionError
        if ``length == 0``.
    """
    num_chunks = (len(x) - length) // (length - overlap) + 1 if len(x) >= length else 0
    remainder = len(x) - num_chunks * length + (num_chunks - 1) * overlap
    res = np.empty((num_chunks, length), dtype=np.array(x).dtype)

    if align == "start":
        start = 0
    elif align == "middle":
        start = remainder // 2
    elif align == "end":
        start = remainder
    else:
        raise ValueError("align must be one of 'start', 'middle', or 'end'.")

    for n in range(num_chunks):
        end = start + length
        res[n] = x[start:end]
        start = end - overlap

    return res


def prune(
    ragged: list | np.ndarray | pd.Series | xr.DataArray,
    rowsize: list | np.ndarray | pd.Series | xr.DataArray,
    min_rowsize: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Within a ragged array, removes arrays less than a specified row size.

    Parameters
    ----------
    ragged : np.ndarray or pd.Series or xr.DataArray
        A ragged array.
    rowsize : list or np.ndarray[int] or pd.Series or xr.DataArray[int]
        The size of each row in the input ragged array.
    min_rowsize :
        The minimum row size that will be kept.

    Returns
    -------
    tuple[np.ndarray, np.ndarray]
        A tuple of ragged array and size of each row.

    Examples
    --------
    >>> from clouddrift.ragged import prune
    >>> import numpy as np
    >>> prune(np.array([1, 2, 3, 0, -1, -2]), np.array([3, 1, 2]),2)
    (array([ 1,  2,  3, -1, -2]), array([3, 2]))

    Raises
    ------
    ValueError
        If the sum of ``rowsize`` does not equal the length of ``arrays``.
    IndexError
        If empty ``ragged``.

    See Also
    --------
    :func:`segment`, `chunk`
    """

    ragged = apply_ragged(
        lambda x, min_len: x if len(x) >= min_len else np.empty(0, dtype=x.dtype),
        np.array(ragged),
        rowsize,
        min_len=min_rowsize,
    )
    rowsize = apply_ragged(
        lambda x, min_len: x if x >= min_len else np.empty(0, dtype=x.dtype),
        np.array(rowsize),
        np.ones_like(rowsize),
        min_len=min_rowsize,
    )

    return ragged, rowsize


def ragged_to_regular(
    ragged: np.ndarray | pd.Series | xr.DataArray,
    rowsize: list | np.ndarray | pd.Series | xr.DataArray,
    fill_value: float = np.nan,
) -> np.ndarray:
    """Convert a ragged array to a two-dimensional array such that each contiguous segment
    of a ragged array is a row in the two-dimensional array. Each row of the two-dimensional
    array is padded with NaNs as needed. The length of the first dimension of the output
    array is the length of ``rowsize``. The length of the second dimension is the maximum
    element of ``rowsize``.

    Note: Although this function accepts parameters of type ``xarray.DataArray``,
    passing NumPy arrays is recommended for performance reasons.

    Parameters
    ----------
    ragged : np.ndarray or pd.Series or xr.DataArray
        A ragged array.
    rowsize : list or np.ndarray[int] or pd.Series or xr.DataArray[int]
        The size of each row in the ragged array.
    fill_value : float, optional
        Fill value to use for the trailing elements of each row of the resulting
        regular array.

    Returns
    -------
    np.ndarray
        A two-dimensional array.

    Examples
    --------
    By default, the fill value used is NaN:

    >>> ragged_to_regular(np.array([1, 2, 3, 4, 5]), np.array([2, 1, 2]))
    array([[ 1.,  2.],
           [ 3., nan],
           [ 4.,  5.]])

    You can specify an alternative fill value:

    >>> ragged_to_regular(np.array([1, 2, 3, 4, 5]), np.array([2, 1, 2]), fill_value=999)
    array([[  1,   2],
           [  3, 999],
           [  4,   5]])

    See Also
    --------
    :func:`regular_to_ragged`
    """
    res = fill_value * np.ones((len(rowsize), int(max(rowsize))), dtype=ragged.dtype)
    unpacked = unpack(ragged, rowsize)
    for n in range(len(rowsize)):
        res[n, : int(rowsize[n])] = unpacked[n]
    return res


def regular_to_ragged(
    array: np.ndarray, fill_value: float = np.nan
) -> tuple[np.ndarray, np.ndarray]:
    """Convert a two-dimensional array to a ragged array. Fill values in the input array are
    excluded from the output ragged array.

    Parameters
    ----------
    array : np.ndarray
        A two-dimensional array.
    fill_value : float, optional
        Fill value used to determine the bounds of contiguous segments.

    Returns
    -------
    tuple[np.ndarray, np.ndarray]
        A tuple of the ragged array and the size of each row.

    Examples
    --------
    By default, NaN values found in the input regular array are excluded from
    the output ragged array:

    >>> regular_to_ragged(np.array([[1, 2], [3, np.nan], [4, 5]]))
    (array([1., 2., 3., 4., 5.]), array([2, 1, 2]))

    Alternatively, a different fill value can be specified:

    >>> regular_to_ragged(np.array([[1, 2], [3, -999], [4, 5]]), fill_value=-999)
    (array([1, 2, 3, 4, 5]), array([2, 1, 2]))

    See Also
    --------
    :func:`ragged_to_regular`
    """
    if np.isnan(fill_value):
        valid = ~np.isnan(array)
    else:
        valid = array != fill_value
    return array[valid], np.sum(valid, axis=1)


def rowsize_to_index(rowsize: list | np.ndarray | xr.DataArray) -> np.ndarray:
    """Convert a list of row sizes to a list of indices.

    This function is typically used to obtain the indices of data rows organized
    in a ragged array.

    Parameters
    ----------
    rowsize : list or np.ndarray or xr.DataArray
        A list of row sizes.

    Returns
    -------
    np.ndarray
        A list of indices.

    Examples
    --------
    To obtain the indices within a ragged array of three consecutive rows of sizes 100, 202, and 53:

    >>> rowsize_to_index([100, 202, 53])
    array([  0, 100, 302, 355])
    """
    return np.cumsum(np.insert(np.array(rowsize), 0, 0))


def segment(
    x: np.ndarray,
    tolerance: float | np.timedelta64 | timedelta | pd.Timedelta,
    rowsize: np.ndarray[int] = None,
) -> np.ndarray[int]:
    """Divide an array into segments based on a tolerance value.

    Parameters
    ----------
    x : list, np.ndarray, or xr.DataArray
        An array to divide into segment.
    tolerance : float, np.timedelta64, timedelta, pd.Timedelta
        The maximum signed difference between consecutive points in a segment.
        The array x will be segmented wherever differences exceed the tolerance.
    rowsize : np.ndarray[int], optional
        The size of rows if x is originally a ragged array. If present, x will be
        divided both by gaps that exceed the tolerance, and by the original rows
        of the ragged array.

    Returns
    -------
    np.ndarray[int]
        An array of row sizes that divides the input array into segments.

    Examples
    --------
    The simplest use of ``segment`` is to provide a tolerance value that is
    used to divide an array into segments:
    >>> from clouddrift.ragged import segment, subset
    >>> import numpy as np

    >>> x = [0, 1, 1, 1, 2, 2, 3, 3, 3, 3, 4]
    >>> segment(x, 0.5)
    array([1, 3, 2, 4, 1])

    If the array is already previously segmented (e.g. multiple rows in
    a ragged array), then the ``rowsize`` argument can be used to preserve
    the original segments:

    >>> x = [0, 1, 1, 1, 2, 2, 3, 3, 3, 3, 4]
    >>> rowsize = [3, 2, 6]
    >>> segment(x, 0.5, rowsize)
    array([1, 2, 1, 1, 1, 4, 1])

    The tolerance can also be negative. In this case, the input array is
    segmented where the negative difference exceeds the negative
    value of the tolerance, i.e. where ``x[n+1] - x[n] < -tolerance``:

    >>> x = [0, 1, 2, 0, 1, 2]
    >>> segment(x, -0.5)
    array([3, 3])

    To segment an array for both positive and negative gaps, invoke the function
    twice, once for a positive tolerance and once for a negative tolerance.
    The result of the first invocation can be passed as the ``rowsize`` argument
    to the first ``segment`` invocation:

    >>> x = [1, 1, 2, 2, 1, 1, 2, 2]
    >>> segment(x, 0.5, rowsize=segment(x, -0.5))
    array([2, 2, 2, 2])

    If the input array contains time objects, the tolerance must be a time interval:

    >>> x = np.array([np.datetime64("2023-01-01"), np.datetime64("2023-01-02"),
    ...               np.datetime64("2023-01-03"), np.datetime64("2023-02-01"),
    ...               np.datetime64("2023-02-02")])
    >>> segment(x, np.timedelta64(1, "D"))
    array([3, 2])
    """

    # for compatibility with datetime list or np.timedelta64 arrays
    if isinstance(tolerance, (np.timedelta64, timedelta)):
        tolerance = pd.Timedelta(tolerance)

    if isinstance(tolerance, pd.Timedelta):
        positive_tol = tolerance >= pd.Timedelta("0 seconds")
    else:
        positive_tol = tolerance >= 0

    if rowsize is None:
        if positive_tol:
            exceeds_tolerance = np.diff(x) > tolerance
        else:
            exceeds_tolerance = np.diff(x) < tolerance
        segment_sizes = np.diff(np.insert(np.where(exceeds_tolerance)[0] + 1, 0, 0))
        segment_sizes = np.append(segment_sizes, len(x) - np.sum(segment_sizes))
        return segment_sizes
    else:
        if not np.sum(rowsize) == len(x):
            raise ValueError("The sum of rowsize must equal the length of x.")
        segment_sizes = []
        start = 0
        for r in rowsize:
            end = start + int(r)
            segment_sizes.append(segment(x[start:end], tolerance))
            start = end
        return np.concatenate(segment_sizes)


def subset(
    ds: xr.Dataset,
    criteria: dict,
    id_var_name: str = "id",
    rowsize_var_name: str = "rowsize",
    row_dim_name: str = "rows",
    obs_dim_name: str = "obs",
    full_rows=False,
) -> xr.Dataset:
    """Subset a ragged array xarray dataset as a function of one or more criteria.
    The criteria are passed with a dictionary, where a dictionary key
    is a variable to subset and the associated dictionary value is either a range
    (valuemin, valuemax), a list [value1, value2, valueN], a single value, or a
    masking function applied to any variable of the dataset.

    This function needs to know the names of the dimensions of the ragged array dataset
    (`row_dim_name` and `obs_dim_name`), and the name of the rowsize variable (`rowsize_var_name`).
    Default values corresponds to the clouddrift convention ("rows", "obs", and "rowsize") but should
    be changed as needed.

    Parameters
    ----------
    ds : xr.Dataset
        Xarray dataset composed of ragged arrays.
    criteria : dict
        Dictionary containing the variables (as keys) and the ranges/values/functions (as values) to subset.
    id_var_name : str, optional
        Name of the variable with dimension `row_dim_name` containing the identification number of the
        rows (default is "id").
    rowsize_var_name : str, optional
        Name of the variable containing the number of observations per row (default is "rowsize").
    row_dim_name : str, optional
        Name of the row dimension (default is "rows").
    obs_dim_name : str, optional
        Name of the observation dimension (default is "obs").
    full_rows : bool, optional
        If True, the function returns complete rows for which the criteria
        are matched at least once. Default is False which means that only segments matching the criteria
        are returned when filtering along the observation dimension.

    Returns
    -------
    xr.Dataset
        Subset xarray dataset matching the criterion(a).

    Examples
    --------
    Criteria are combined on any data (with dimension "obs") or metadata (with dimension "rows") variables
    part of the Dataset. The following examples are based on NOAA GDP datasets which can be accessed with the
    ``clouddrift.datasets`` module. In these datasets, each row of the ragged arrays corresponds to the data from
    a single drifter trajectory and the `row_dim_name` is "traj" and the `obs_dim_name` is "obs".

    Retrieve a region, like the Gulf of Mexico, using ranges of latitude and longitude:
    >>> from clouddrift.datasets import gdp6h
    >>> from clouddrift.ragged import subset
    >>> import numpy as np

    >>> ds = gdp6h()
    ...

    >>> subset(ds, {"lat": (21, 31), "lon": (-98, -78)}, row_dim_name="traj")
    <xarray.Dataset> ...
    ...

    The parameter `full_rows` can be used to retrieve trajectories passing through a region, for example all trajectories passing through the Gulf of Mexico:

    >>> subset(ds, {"lat": (21, 31), "lon": (-98, -78)}, full_rows=True, row_dim_name="traj")
    <xarray.Dataset> ...
    ...

    Retrieve drogued trajectory segments:

    >>> subset(ds, {"drogue_status": True}, row_dim_name="traj")
    <xarray.Dataset> ...
    Dimensions:                (traj: ..., obs: ...)
    Coordinates:
        id                     (traj) int64 ...
        time                   (obs) datetime64[ns] ...
    ...

    Retrieve trajectory segments with temperature higher than 25°C (303.15K):

    >>> subset(ds, {"temp": (303.15, np.inf)}, row_dim_name="traj")
    <xarray.Dataset> ...
    ...

    You can use the same approach to return only the trajectories that are
    shorter than some number of observations (similar to :func:`prune` but for
    the entire dataset):

    >>> subset(ds, {"rowsize": (0, 1000)}, row_dim_name="traj")
    <xarray.Dataset> ...
    ...

    Retrieve specific drifters using their IDs:

    >>> subset(ds, {"id": [2578, 2582, 2583]}, row_dim_name="traj")
    <xarray.Dataset> ...
    ...

    Sometimes, you may want to retrieve specific rows of a ragged array.
    You can do that by filtering along the trajectory dimension directly, since
    this one corresponds to row numbers:

    >>> rows = [5, 6, 7]
    >>> subset(ds, {"traj": rows}, row_dim_name="traj")
    <xarray.Dataset> ...
    ...

    Retrieve a specific time period:

    >>> subset(ds, {"time": (np.datetime64("2000-01-01"), np.datetime64("2020-01-31"))}, row_dim_name="traj")
    <xarray.Dataset> ...
    ...

    Note that to subset time variable, the range has to be defined as a function
    type of the variable. By default, ``xarray`` uses ``np.datetime64`` to
    represent datetime data. If the datetime data is a ``datetime.datetime``, or
    ``pd.Timestamp``, the range would have to be defined accordingly.

    Those criteria can also be combined:

    >>> subset(ds, {"lat": (21, 31), "lon": (-98, -78), "drogue_status": True, "temp": (303.15, np.inf), "time": (np.datetime64("2000-01-01"), np.datetime64("2020-01-31"))}, row_dim_name="traj")
    <xarray.Dataset> ...
    ...

    You can also use a function to filter the data. For example, retrieve every other observation
    of each trajectory:

    >>> func = (lambda arr: ((arr - arr[0]) % 2) == 0)
    >>> subset(ds, {"id": func}, row_dim_name="traj")
    <xarray.Dataset> ...
    ...

    The filtering function can accept several input variables passed as a tuple. For example, retrieve
    drifters released in the Mediterranean Sea, but exclude those released in the Bay of Biscay and the Black Sea:

    >>> def mediterranean_mask(lon: xr.DataArray, lat: xr.DataArray) -> xr.DataArray:
    ...    # Mediterranean Sea bounding box
    ...    in_med = np.logical_and(-6.0327 <= lon, np.logical_and(lon <= 36.2173,
    ...                                                           np.logical_and(30.2639 <= lat, lat <= 45.7833)))
    ...    # Bay of Biscay
    ...    in_biscay = np.logical_and(lon <= -0.1462, lat >= 43.2744)
    ...    # Black Sea
    ...    in_blacksea = np.logical_and(lon >= 27.4437, lat >= 40.9088)
    ...    return np.logical_and(in_med, np.logical_not(np.logical_or(in_biscay, in_blacksea)))
    >>> subset(ds, {("start_lon", "start_lat"): mediterranean_mask}, row_dim_name="traj")
    <xarray.Dataset> Size: ...
    Dimensions:                (traj: ..., obs: ...)
    Coordinates:
        id                     (traj) int64 ...
        time                   (obs) datetime64[ns] ...
    ...

    Raises
    ------
    ValueError
        If one of the variable in a criterion is not found in the Dataset.
    TypeError
        If one of the `criteria` key is a tuple while its associated value is not a `Callable` criterion.
    TypeError
        If variables of a `criterion` key associated to a `Callable` do not share the same dimension.

    See Also
    --------
    :func:`apply_ragged`
    """
    mask_row = xr.DataArray(
        data=np.ones(ds.sizes[row_dim_name], dtype="bool"), dims=[row_dim_name]
    )
    mask_obs = xr.DataArray(
        data=np.ones(ds.sizes[obs_dim_name], dtype="bool"), dims=[obs_dim_name]
    )

    for key in criteria.keys():
        if np.all(np.isin(key, ds.variables) | np.isin(key, ds.dims)):
            if isinstance(key, tuple):
                criterion = [ds[k] for k in key]
                if not all(c.dims == criterion[0].dims for c in criterion):
                    raise TypeError(
                        "Variables passed to the Callable must share the same dimension."
                    )
                criterion_dims = criterion[0].dims
            else:
                criterion = ds[key]
                criterion_dims = criterion.dims

            if row_dim_name in criterion_dims:
                mask_row = np.logical_and(
                    mask_row,
                    _mask_var(
                        criterion, criteria[key], ds[rowsize_var_name], row_dim_name
                    ),
                )
            elif obs_dim_name in criterion_dims:
                mask_obs = np.logical_and(
                    mask_obs,
                    _mask_var(
                        criterion, criteria[key], ds[rowsize_var_name], obs_dim_name
                    ),
                )
        else:
            raise ValueError(f"Unknown variable '{key}'.")

    # remove data when rows are filtered
    traj_idx = rowsize_to_index(ds[rowsize_var_name].values)
    for i in np.where(~mask_row)[0]:
        mask_obs[slice(traj_idx[i], traj_idx[i + 1])] = False

    # remove rows completely filtered in mask_obs
    ids_with_mask_obs = np.repeat(ds[id_var_name].values, ds[rowsize_var_name].values)[
        mask_obs
    ]
    mask_row = np.logical_and(
        mask_row, np.in1d(ds[id_var_name], np.unique(ids_with_mask_obs))
    )

    # reset mask_obs to True if we want to keep complete rows
    if full_rows:
        for i in np.where(mask_row)[0]:
            mask_obs[slice(traj_idx[i], traj_idx[i + 1])] = True
        ids_with_mask_obs = np.repeat(
            ds[id_var_name].values, ds[rowsize_var_name].values
        )[mask_obs]

    if not any(mask_row):
        warnings.warn("No data matches the criteria; returning an empty dataset.")
        return xr.Dataset()
    else:
        # apply the filtering for both dimensions
        ds_sub = ds.isel({row_dim_name: mask_row, obs_dim_name: mask_obs})
        _, unique_idx, sorted_rowsize = np.unique(
            ids_with_mask_obs, return_index=True, return_counts=True
        )
        ds_sub[rowsize_var_name].values = sorted_rowsize[np.argsort(unique_idx)]
        return ds_sub


def unpack(
    ragged_array: np.ndarray | xr.DataArray,
    rowsize: np.ndarray | xr.DataArray,
    rows: int | Iterable[int] = None,
    axis: int = 0,
) -> list[np.ndarray | xr.DataArray]:
    """Unpack a ragged array into a list of regular arrays.

    Unpacking a ``np.ndarray`` ragged array is about 2 orders of magnitude
    faster than unpacking an ``xr.DataArray`` ragged array, so unless you need a
    ``DataArray`` as the result, we recommend passing ``np.ndarray`` as input.

    Parameters
    ----------
    ragged_array : array-like
        A ragged_array to unpack
    rowsize : array-like
        An array of integers whose values is the size of each row in the ragged
        array
    rows : int or Iterable[int], optional
        A row or list of rows to unpack. Default is None, which unpacks all rows.
    axis : int, optional
        The axis along which to unpack the ragged array. Default is 0.

    Returns
    -------
    list
        A list of array-likes with sizes that correspond to the values in
        rowsize, and types that correspond to the type of ragged_array

    Examples
    --------

    Unpacking longitude arrays from a ragged Xarray Dataset:
    >>> from clouddrift.ragged import unpack
    >>> from clouddrift.datasets import gdp6h

    >>> ds = gdp6h()

    >>> lon = unpack(ds.lon, ds["rowsize"]) # return a list[xr.DataArray] (slower)
    >>> lon = unpack(ds.lon.values, ds["rowsize"]) # return a list[np.ndarray] (faster)
    >>> first_lon = unpack(ds.lon.values, ds["rowsize"], rows=0) # return only the first row
    >>> first_two_lons = unpack(ds.lon.values, ds["rowsize"], rows=[0, 1]) # return first two rows

    Looping over trajectories in a ragged Xarray Dataset to compute velocities
    for each:

    >>> from clouddrift.kinematics import velocity_from_position

    >>> for lon, lat, time in list(zip(
    ...     unpack(ds.lon.values, ds["rowsize"]),
    ...     unpack(ds.lat.values, ds["rowsize"]),
    ...     unpack(ds.time.values, ds["rowsize"])
    ... )):
    ...     u, v = velocity_from_position(lon, lat, time)
    """
    indices = rowsize_to_index(rowsize)

    if rows is None:
        rows = range(indices.size - 1)
    if isinstance(rows, (int, np.integer)):
        rows = [rows]

    unpacked = np.split(ragged_array, indices[1:-1], axis=axis)

    return [unpacked[i] for i in rows]


def obs_index_to_row(
    index: int | list[int] | np.ndarray | xr.DataArray,
    rowsize: list[int] | np.ndarray | xr.DataArray,
) -> list:
    """Obtain a list of row indices from a list of observation indices of a ragged array.
       A ragged array is constituted of rows of different sizes indicated by ``rowsize`` and is
       also constituted of a continuous sequence of observations with indices 0 to its length - 1.
       This function allows the user to obtain the row index of a given observation given its index.
       This answers the question: "In which row is an observation located?"

    Parameters
    ----------
    index : int or list or np.ndarray
        A integer observation index or a list of observation indices of a ragged array.
    rowsize : list or np.ndarray or xr.DataArray
        A sequence of row sizes of a ragged array.

    Returns
    -------
    list
        A list of row indices.

    Examples
    --------
    To obtain the row index of observation with index 5 within a ragged array of three consecutive
    rows of sizes 2, 4, and 3:

    >>> obs_index_to_row(5, [2, 4, 3])
    [1]

    To obtain the row indices of observations with indices 0, 2, and 4 within a ragged array of three
    consecutive rows of sizes 2, 4, and 3:

    >>> obs_index_to_row([0, 2, 4], [2, 4, 3])
    [0, 1, 1]

    """
    # convert index to list if it is not
    if isinstance(index, xr.DataArray):
        index_list = [int(i) for i in index.values]
    elif isinstance(index, np.ndarray):
        index_list = [int(i) for i in index]
    elif isinstance(index, int):
        index_list = [index]
    else:
        index_list = index

    # if index is not a list of integers or integer-likes, raise an error
    if not all(isinstance(i, int) for i in index_list):
        raise ValueError("The index must be an integer or a list of integers.")

    rowsize_index = rowsize_to_index(rowsize)

    # test that no index is out of bounds
    if any([(i < rowsize_index[0]) | (i >= rowsize_index[-1]) for i in index_list]):
        raise ValueError("Input index out of bounds based on input rowsize")

    return (np.searchsorted(rowsize_index, index_list, side="right") - 1).tolist()


def _mask_var(
    var: xr.DataArray | list[xr.DataArray],
    criterion: tuple | list | np.ndarray | xr.DataArray | bool | float | int | Callable,
    rowsize: xr.DataArray = None,
    dim_name: str = "dim_0",
) -> xr.DataArray:
    """Return the mask of a subset of the data matching a test criterion.

    Parameters
    ----------
    var : xr.DataArray or list[xr.DataArray]
        DataArray or list of DataArray (only applicable if the criterion is a Callable) to be used by the criterion
    criterion : array-like or scalar or Callable
        The criterion can take four forms:
        - tuple: (min, max) defining a range
        - list, np.ndarray, or xr.DataArray: An array-like defining multiples values
        - scalar: value defining a single value
        - function: a function applied against each row using ``apply_ragged`` and returning a mask
    rowsize : xr.DataArray, optional
        List of integers specifying the number of data points in each row
    dim_name : str, optional
        Name of the masked dimension (default is "dim_0")

    Examples
    --------
    >>> import xarray as xr
    >>> from clouddrift.ragged import _mask_var

    >>> x = xr.DataArray(data=np.arange(0, 5))
    >>> _mask_var(x, (2, 4))
    <xarray.DataArray (dim_0: 5)> ...
    array([False, False,  True,  True,  True])
    Dimensions without coordinates: dim_0

    >>> _mask_var(x, [0, 2, 4])
    array([ True, False,  True, False,  True])

    >>> _mask_var(x, 4)
    <xarray.DataArray (dim_0: 5)> ...
    array([False, False, False, False,  True])
    Dimensions without coordinates: dim_0

    >>> rowsize = xr.DataArray(data=[2, 3])
    >>> _mask_var(x, lambda arr: arr==arr[0]+1, rowsize, "dim_0")
    <xarray.DataArray (dim_0: 5)> ...
    array([False,  True, False,  True, False])
    Dimensions without coordinates: dim_0

    >>> y = xr.DataArray(data=np.arange(0, 5)+2)
    >>> rowsize = xr.DataArray(data=[2, 3])
    >>> _mask_var([x, y], lambda var1, var2: ((var1 * var2) % 2) == 0, rowsize, "dim_0")
    <xarray.DataArray (dim_0: 5)> ...
    array([ True, False,  True, False,  True])
    Dimensions without coordinates: dim_0

    Returns
    -------
    mask : xr.DataArray
        The mask of the subset of the data matching the criteria
    """
    if not callable(criterion) and isinstance(var, list):
        raise TypeError(
            "The `var` parameter can be a `list` only if the `criterion` is a `Callable`."
        )

    if isinstance(criterion, tuple):  # min/max defining range
        mask = np.logical_and(var >= criterion[0], var <= criterion[1])
    elif isinstance(criterion, (list, np.ndarray, xr.DataArray)):
        # select multiple values
        mask = np.isin(var, criterion)
    elif callable(criterion):
        # mask directly created by applying `criterion` function
        if not isinstance(var, list):
            var = [var]

        if len(var[0]) == len(rowsize):
            mask = criterion(*var)
        else:
            mask = apply_ragged(criterion, var, rowsize)

        mask = xr.DataArray(data=mask, dims=[dim_name]).astype(bool)

        if not len(var[0]) == len(mask):
            raise ValueError(
                "The `Callable` function must return a masked array that matches the length of the variable to filter."
            )
    else:  # select one specific value
        mask = var == criterion
    return mask

from contextlib import contextmanager
from hashlib import sha256
import logging
import warnings


import numpy as np
import pandas as pd


from solarforecastarbiter import datamodel


def _observation_valid(index, obs_id, aggregate_observations):
    """
    Indicates where the observation data is valid. For now,
    effective_from and effective_until are inclusive, so data missing
    at those times is marked as missing in the aggregate.
    """
    nindex = pd.DatetimeIndex([], tz=index.tz)
    for aggobs in aggregate_observations:
        if aggobs['observation_id'] == obs_id:
            if aggobs['observation_deleted_at'] is None:
                locs = index.slice_locs(aggobs['effective_from'],
                                        aggobs['effective_until'])
                nindex = nindex.union(index[locs[0]:locs[1]])
            elif (
                    aggobs['effective_until'] is None or
                    aggobs['effective_until'] >= index[0]
            ):
                raise ValueError(
                    'Deleted Observation data cannot be retrieved'
                    ' to include in Aggregate')
            else:  # observation deleted and effective_until before index
                return pd.Series(False, index=index)
    return pd.Series(1, index=nindex).reindex(index).fillna(0).astype(bool)


def _make_aggregate_index(data, interval_length, interval_label,
                          timezone):
    """
    Compute the aggregate the index should have based on the min and
    max timestamps in the data, the interval length, label, and timezone.
    """
    # first, find limits for a new index
    start = pd.Timestamp('20380119T031407Z')
    end = pd.Timestamp('19700101T000001Z')
    for df in data.values():
        start = min(start, min(df.index))
        end = max(end, max(df.index))
    # adjust start, end to nearest interval
    # hard to understand what this interval should be for
    # odd (e.g. 52min) intervals, so required that interval
    # is a divisor of one day
    if 86400 % pd.Timedelta(interval_length).total_seconds() != 0:
        raise ValueError(
            'interval_length must be a divisor of one day')
    if interval_label == 'ending':
        start = start.ceil(interval_length)
        end = end.ceil(interval_length)
    elif interval_label == 'beginning':
        start = start.floor(interval_length)
        end = end.floor(interval_length)
    else:
        raise ValueError(
            'interval_label must be beginning or ending for aggregates')
    # raise the error if unlocalized
    start = start.tz_convert(timezone)
    end = end.tz_convert(timezone)
    return pd.date_range(
        start, end, freq=interval_length, tz=timezone)


def compute_aggregate(data, interval_length, interval_label,
                      timezone, agg_func, aggregate_observations):
    """
    Computes an aggregate quantity according to agg_func of the data.
    This function assumes the data has an interval_value_type of
    interval_mean or instantaneous and that the data interval_length
    is less than or equal to the aggregate interval_length.
    NaNs in the output are the result of missing data from an
    underyling observation of the aggregate.

    Parameters
    ----------
    data : dict of pandas.DataFrames
        With keys 'observation_id' corresponding to observation in
        aggregate_observations. DataFrames must have 'value' and 'quality_flag'
        columns.
    interval_length : str or pandas.Timedelta
        The time between timesteps in the aggregate result.
    interval_label : str
        Whether the timestamps in the aggregated output represent the beginning
        or ending of the interval
    timezone : str
        The IANA timezone for the output index
    agg_func : str
        The aggregation function (e.g 'sum', 'mean', 'min') to create the
        aggregate
    aggregate_observations : tuple of dicts
        Each dict should have 'observation_id' (string),
        'effective_from' (timestamp), 'effective_until' (timestamp or None),
        and 'observation_deleted_at' (timestamp or None) fields.

    Returns
    -------
    pandas.DataFrame
        - Index is a DatetimeIndex that adheres to interval_length and
          interval_label

        - Columns are 'value', for the aggregated value according to agg_func,
          and 'quality_flag', the bitwise or of all flags in the aggregate for
          the interval.

        - A 'value' of NaN means that data from one or more
          observations was missing in that interval.

    Raises
    ------
    KeyError
        If data is missing a key for an observation in aggregate_obsevations

        + Or, if any DataFrames in data do not have 'value' or 'quality_flag'
          columns

    ValueError
        If interval_length is not a divisor of one day

        + Or, if an observation has been deleted but the data is required for
          the aggregate
        + Or, if interval_label is not beginning or ending

    """
    new_index = _make_aggregate_index(
        data, interval_length, interval_label, timezone)
    unique_ids = {ao['observation_id'] for ao in aggregate_observations}
    valid_mask = {obs_id: _observation_valid(
        new_index, obs_id, aggregate_observations) for obs_id in unique_ids}

    missing_from_data_dict = {
        ao['observation_id'] for ao in aggregate_observations
        if ao['observation_deleted_at'] is None
        } - set(data.keys())

    if missing_from_data_dict:
        raise KeyError(
            'Cannot aggregate data with missing keys '
            f'{", ".join(missing_from_data_dict)}')

    value_is_missing = pd.Series(False, index=new_index)
    value = {}
    qf = {}
    closed = datamodel.CLOSED_MAPPING[interval_label]
    for obs_id, df in data.items():
        resampler = df.resample(interval_length, closed=closed, label=closed)
        new_val = resampler['value'].mean().reindex(new_index)
        # data is missing when the resampled value is NaN and the data
        # should be valid according to effective_from/until
        valid = valid_mask[obs_id]
        missing = new_val.isna() & valid
        if missing.any():
            warnings.warn('Values missing for one or more observations')
            value_is_missing[missing] = True
        value[obs_id] = new_val[valid]
        qf[obs_id] = resampler['quality_flag'].apply(np.bitwise_or.reduce)
    final_value = pd.DataFrame(value).reindex(new_index).aggregate(
        agg_func, axis=1)
    final_value[value_is_missing] = np.nan
    # have to fill in nans and convert to int to do bitwise_or
    # only works with pandas >= 0.25.0
    final_qf = pd.DataFrame(qf).reindex(new_index).fillna(0).astype(
        int).aggregate(np.bitwise_or.reduce, axis=1)
    out = pd.DataFrame({'value': final_value, 'quality_flag': final_qf})
    return out


def sha256_pandas_object_hash(obj):
    """
    Compute a hash for a pandas object. No sorting of the
    object is performed, so an object with the same data in
    in a different order returns a different hash.

    Parameters
    ----------
    obj: pandas.Series or pandas.DataFrame

    Returns
    -------
    str
       Hex digest of the SHA-256 hash of the individual object row hashes
    """
    return sha256(
        pd.util.hash_pandas_object(obj).values.tobytes()
    ).hexdigest()


class ListHandler(logging.Handler):
    """
    A logger handler that appends each log record to a list.
    """
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def export_records(self, level=logging.WARNING):
        """
        Convert each log record in the records list with level
        greater than or equal to `level` to a
        :py:class:`solarforecastarbiter.datamodel.ReportMessage`
        and return the tuple of messages.
        """
        out = []
        for rec in self.records:
            if rec.levelno >= level:
                out.append(
                    datamodel.ReportMessage(
                        message=rec.getMessage(),
                        step=rec.name,
                        level=rec.levelname,
                        function=rec.funcName
                    )
                )
        return tuple(out)


@contextmanager
def hijack_loggers(loggers, level=logging.INFO):
    """
    Context manager to temporarily set the handler
    of each logger in `loggers`.

    Parameters
    ----------
    loggers: list of str or logging.Logger
        Loggers to change
    level: logging LEVEL int
        Level to set the temporary handler to

    Returns
    -------
    ListHandler
        The handler that will be temporarily assigned
        to each logger.

    Notes
    -----
    This may not capture all records when used in a
    distributed or multiprocessing workflow
    """
    handler = ListHandler()
    handler.setLevel(level)

    old_handlers = {}
    for name in loggers:
        logger = logging.getLogger(name)
        old_handlers[name] = logger.handlers
        logger.handlers = [handler]
    yield handler
    for name in loggers:
        logger = logging.getLogger(name)
        logger.handlers = old_handlers[name]
    del handler

"""
Algorithms that Involve Multiple DataFrames
===========================================

The pandas operations ``concat``, ``join``, and ``merge`` combine multiple
DataFrames.  This module contains analogous algorithms in the parallel case.

There are two important cases:

1.  We combine along a partitioned index
2.  We combine along an unpartitioned index or other column

In the first case we know which partitions of each dataframe interact with
which others.  This lets uss be significantly more clever and efficient.

In the second case each partition from one dataset interacts with all
partitions from the other.  We handle this through a shuffle operation.

Partitioned Joins
-----------------

In the first case where we join along a partitioned index we proceed in the
following stages.

1.  Align the partitions of all inputs to be the same.  This involves a call
    to ``dd.repartition`` which will split up and concat existing partitions as
    necessary.  After this step all inputs have partitions that align with
    each other.  This step is relatively cheap.  See the function ``align``.
2.  Remove unnecessary partitions based on the type of join we perform (left,
    right, inner, outer).  We can do this at the partition level before any
    computation happens.  We'll do it again on each partition when we call the
    in-memory function.  See the function ``require``.
3.  Embarrassingly parallel calls to ``pd.concat``, ``pd.join``, or
    ``pd.merge``.  Now that the data is aligned and unnecessary blocks have
    been removed we can rely on the fast in-memory Pandas join machinery to
    execute joins per-partition.  We know that all intersecting records exist
    within the same partition


Hash Joins via Shuffle
----------------------

When we join along an unpartitioned index or along an arbitrary column any
partition from one input might interact with any partition in another.  In
this case we perform a hash-join by shuffling data in each input by that
column.  This results in new inputs with the same partition structure cleanly
separated along that column.

We proceed with hash joins in the following stages:

1.  Shuffle each input on the specified column.  See the function
    ``dask.dataframe.shuffle.shuffle``.
2.  Perform embarrassingly parallel join across shuffled inputs.
"""

from .core import repartition, tokens, DataFrame
from .shuffle import shuffle
from bisect import bisect_left, bisect_right
from toolz import merge_sorted, unique, merge, curry
import pandas as pd


def bound(seq, left, right):
    """ Bound sorted list by left and right values

    >>> bound([1, 3, 4, 5, 8, 10, 12], 4, 10)
    [4, 5, 8, 10]
    """
    return seq[bisect_left(seq, left): bisect_right(seq, right)]


def align(*dfs):
    """ Mutually partition and align DataFrame blocks

    This serves as precursor to multi-dataframe operations like join, concat,
    or merge.

    Parameters
    ----------
    dfs: sequence of dd.DataFrames
        Sequence of dataframes to be aligned on their index

    Returns
    -------
    dfs: sequence of dd.DataFrames
        These DataFrames have consistent divisions with each other
    divisions: tuple
        Full divisions sequence of the entire result
    result: list
        A list of lists of keys that show which dataframes exist on which
        divisions
    """
    divisions = list(unique(merge_sorted(*[df.divisions for df in dfs])))
    divisionss = [bound(divisions, df.divisions[0], df.divisions[-1])
                  for df in dfs]
    dfs2 = list(map(repartition, dfs, divisionss))

    result = list()
    inds = [0 for df in dfs]
    for d in divisions[:-1]:
        L = list()
        for i in range(len(dfs)):
            j = inds[i]
            divs = dfs2[i].divisions
            if j < len(divs) - 1 and divs[j] == d:
                L.append((dfs2[i]._name, inds[i]))
                inds[i] += 1
            else:
                L.append(None)
        result.append(L)
    return dfs2, tuple(divisions), result


def require(divisions, parts, required=None):
    """ Clear out divisions where required components are not present

    In left, right, or inner joins we exclude portions of the dataset if one
    side or the other is not present.  We can achieve this at the partition
    level as well

    >>> divisions = [1, 3, 5, 7, 9]
    >>> parts = [(('a', 0), None),
    ...          (('a', 1), ('b', 0)),
    ...          (('a', 2), ('b', 1)),
    ...          (None, ('b', 2))]

    >>> divisions2, parts2 = require(divisions, parts, required=[0])
    >>> divisions2
    (1, 3, 5, 7)
    >>> parts2  # doctest: +NORMALIZE_WHITESPACE
    ((('a', 0), None),
     (('a', 1), ('b', 0)),
     (('a', 2), ('b', 1)))

    >>> divisions2, parts2 = require(divisions, parts, required=[1])
    >>> divisions2
    (3, 5, 7, 9)
    >>> parts2  # doctest: +NORMALIZE_WHITESPACE
    ((('a', 1), ('b', 0)),
     (('a', 2), ('b', 1)),
     (None, ('b', 2)))

    >>> divisions2, parts2 = require(divisions, parts, required=[0, 1])
    >>> divisions2
    (3, 5, 7)
    >>> parts2  # doctest: +NORMALIZE_WHITESPACE
    ((('a', 1), ('b', 0)),
     (('a', 2), ('b', 1)))
    """
    if not required:
        return divisions, parts
    for i in required:
        present = [j for j, p in enumerate(parts) if p[i] is not None]
        divisions = tuple(divisions[min(present): max(present) + 2])
        parts = tuple(parts[min(present): max(present) + 1])
    return divisions, parts



required = {'left': [0], 'right': [1], 'inner': [0, 1], 'outer': []}

def join_indexed_dataframes(lhs, rhs, how='left', lsuffix='', rsuffix=''):
    """ Join two partitiond dataframes along their index """
    (lhs, rhs), divisions, parts = align(lhs, rhs)
    divisions, parts = require(divisions, parts, required[how])

    left_empty = pd.DataFrame([], columns=lhs.columns)
    right_empty = pd.DataFrame([], columns=rhs.columns)

    name = 'join-indexed' + next(tokens)
    dsk = dict(((name, i),
                (pd.DataFrame.join, a, b, None, how, lsuffix, rsuffix)
                if a is not None and b is not None else
                (pd.DataFrame.join, a, right_empty, None, how, lsuffix, rsuffix)
                if a is not None and how in ('left', 'outer') else
                (pd.DataFrame.join, left_empty, b, None, how, lsuffix, rsuffix)
                if b is not None and how in ('right', 'outer') else
                None)
                for i, (a, b) in enumerate(parts))

    # fake column names
    j = left_empty.join(right_empty, None, how, lsuffix, rsuffix)

    return DataFrame(merge(lhs.dask, rhs.dask, dsk), name, j.columns, divisions)


def hash_join(lhs, on_left, rhs, on_right, how='inner', npartitions=None, suffixes=('_x', '_y')):
    """ Join two DataFrames on particular columns with hash join

    This shuffles both datasets on the joined column and then performs an
    embarassingly parallel join partition-by-partition

    >>> hash_join(a, 'id', rhs, 'id', how='left', npartitions=10)  # doctest: +SKIP
    """
    if npartitions is None:
        npartitions = max(lhs.npartitions, rhs.npartitions)

    lhs2 = shuffle(lhs, on_left, npartitions)
    rhs2 = shuffle(rhs, on_right, npartitions)

    name = 'hash-join' + next(tokens)
    pdmerge = curry(pd.merge, suffixes=suffixes)
    dsk = dict(((name, i), (pdmerge, (lhs2._name, i), (rhs2._name, i),
                             how, None, on_left, on_right))
                for i in range(npartitions))

    # fake column names
    left_empty = pd.DataFrame([], columns=lhs.columns)
    right_empty = pd.DataFrame([], columns=rhs.columns)
    j = pd.merge(left_empty, right_empty, how, None, on_left, on_right)

    divisions = [None] * (npartitions + 1)

    return DataFrame(merge(lhs2.dask, rhs2.dask, dsk),
                     name, j.columns, divisions)


def concat_indexed_dataframes(dfs, join='outer'):
    """ Concatenate indexed dataframes together along the index """
    assert join in ('inner', 'outer')
    dfs2, divisions, parts = align(*dfs)

    empties = [pd.DataFrame([], columns=df.columns) for df in dfs]

    columns = pd.concat(empties, 0, join).columns

    parts2 = [[df if df is not None else empty
               for df, empty in zip(part, empties)]
              for part in parts]

    name = 'concat-indexed' + next(tokens)
    dsk = dict(((name, i), (pd.concat, part, 0, join))
                for i, part in enumerate(parts2))

    return DataFrame(merge(dsk, *[df.dask for df in dfs2]), name, columns, divisions)

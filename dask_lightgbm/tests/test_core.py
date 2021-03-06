# Workaround for conflict with distributed 1.23.0
# https://github.com/dask/dask-xgboost/pull/27#issuecomment-417474734
from concurrent.futures import ThreadPoolExecutor

import dask.array as da
import dask.dataframe as dd
import distributed.comm.utils
import lightgbm
import numpy as np
import pandas as pd
import pytest
import scipy.sparse
import sparse
from dask.array.utils import assert_eq
from dask.distributed import Client
from dask_ml.metrics import accuracy_score, r2_score
from distributed.utils_test import gen_cluster, loop, cluster  # noqa
from sklearn.datasets import make_blobs, make_regression
from sklearn.metrics import confusion_matrix

import dask_lightgbm.core as dlgbm

distributed.comm.utils._offload_executor = ThreadPoolExecutor(max_workers=2)


@pytest.fixture()
def listen_port():
    listen_port.port += 10
    return listen_port.port


listen_port.port = 13000


def _create_data(objective, n_samples=100, centers=2, output="array", chunk_size=50):
    if objective == 'classification':
        X, y = make_blobs(n_samples=n_samples, centers=centers, random_state=42)
    elif objective == 'regression':
        X, y = make_regression(n_samples=n_samples, random_state=42)
    else:
        raise ValueError(objective)
    rnd = np.random.RandomState(42)
    w = rnd.rand(X.shape[0])*0.01

    if output == "array":
        dX = da.from_array(X, (chunk_size, X.shape[1]))
        dy = da.from_array(y, chunk_size)
        dw = da.from_array(w, chunk_size)
    elif output == "dataframe":
        X_df = pd.DataFrame(X, columns=[f"feature_{i}" for i in range(X.shape[1])])
        y_df = pd.Series(y, name="target")
        dX = dd.from_pandas(X_df, chunksize=chunk_size)
        dy = dd.from_pandas(y_df, chunksize=chunk_size)
        dw = dd.from_array(w, chunksize=chunk_size)
    elif output == "scipy_csr_matrix":
        dX = da.from_array(X, chunks=(chunk_size, X.shape[1])).map_blocks(scipy.sparse.csr_matrix)
        dy = da.from_array(y, chunks=chunk_size)
        dw = da.from_array(w, chunk_size)
    elif output == "sparse":
        dX = da.from_array(X, chunks=(chunk_size, X.shape[1])).map_blocks(sparse.COO)
        dy = da.from_array(y, chunks=chunk_size)
        dw = da.from_array(w, chunk_size)

    return X, y, w, dX, dy, dw


@pytest.mark.parametrize('output', ['array', 'scipy_csr_matrix', 'sparse', 'dataframe'])
@pytest.mark.parametrize('centers', [[[-4, -4], [4, 4]], [[-4, -4], [4, 4], [-4, 4]]])  # noqa
def test_classifier(loop, output, listen_port, centers):
    with cluster() as (s, [a, b]):
        with Client(s['address'], loop=loop) as client:
            X, y, w, dX, dy, dw = _create_data('classification', output=output, centers=centers)

            a = dlgbm.LGBMClassifier(local_listen_port=listen_port)
            a = a.fit(dX, dy, sample_weight=dw)
            p1 = a.predict(dX, client=client)
            s1 = accuracy_score(dy, p1)
            p1 = p1.compute()

            b = lightgbm.LGBMClassifier()
            b.fit(X, y, sample_weight=w)
            p2 = b.predict(X)
            s2 = b.score(X, y)
            print(confusion_matrix(y, p1))
            print(confusion_matrix(y, p2))

            assert_eq(s1, s2)
            print(s1)

            assert_eq(p1, p2)
            assert_eq(y, p1)
            assert_eq(y, p2)


@pytest.mark.parametrize('output', ['array', 'scipy_csr_matrix', 'sparse', 'dataframe'])
@pytest.mark.parametrize('centers', [[[-4, -4], [4, 4]], [[-4, -4], [4, 4], [-4, 4]]])  # noqa
def test_classifier_proba(loop, output, listen_port, centers):
    with cluster() as (s, [a, b]):
        with Client(s['address'], loop=loop) as client:
            X, y, w, dX, dy, dw = _create_data('classification', output=output, centers=centers)

            a = dlgbm.LGBMClassifier(local_listen_port=listen_port)
            a = a.fit(dX, dy, sample_weight=dw)
            p1 = a.predict_proba(dX, client=client)
            p1 = p1.compute()

            b = lightgbm.LGBMClassifier()
            b.fit(X, y, sample_weight=w)
            p2 = b.predict_proba(X)

            assert_eq(p1, p2, atol=0.3)


def test_classifier_local_predict(loop, listen_port): #noqa
    with cluster() as (s, [a, b]):
        with Client(s['address'], loop=loop):
            X, y, w, dX, dy, dw = _create_data('classification', output="array")

            a = dlgbm.LGBMClassifier(local_listen_port=listen_port)
            a = a.fit(dX, dy, sample_weight=dw)
            p1 = a.to_local().predict(dX)

            b = lightgbm.LGBMClassifier()
            b.fit(X, y, sample_weight=w)
            p2 = b.predict(X)

            assert_eq(p1, p2)
            assert_eq(y, p1)
            assert_eq(y, p2)


@pytest.mark.parametrize('output', ['array', 'scipy_csr_matrix', 'sparse', 'dataframe'])
def test_regressor(loop, output, listen_port):
    with cluster() as (s, [a, b]):
        with Client(s['address'], loop=loop) as client:
            X, y, w, dX, dy, dw = _create_data('regression', output=output)

            a = dlgbm.LGBMRegressor(local_listen_port=listen_port, seed=42)
            a = a.fit(dX, dy, client=client, sample_weight=dw)
            p1 = a.predict(dX, client=client)
            if output != 'dataframe':
                s1 = r2_score(dy, p1)
            p1 = p1.compute()

            b = lightgbm.LGBMRegressor(seed=42)
            b.fit(X, y, sample_weight=w)
            s2 = b.score(X, y)
            p2 = b.predict(X)

            # Scores should be the same
            if output != 'dataframe':
                assert_eq(s1, s2, atol=.01)

            # Predictions should be roughly the same
            assert_eq(y, p1, rtol=1., atol=50.)
            assert_eq(y, p2, rtol=1., atol=50.)


@pytest.mark.parametrize('output', ['array', 'scipy_csr_matrix', 'sparse', 'dataframe'])
@pytest.mark.parametrize('alpha', [.1, .5, .9])
def test_regressor_quantile(loop, output, listen_port, alpha):
    with cluster() as (s, [a, b]):
        with Client(s['address'], loop=loop) as client:
            X, y, w, dX, dy, dw = _create_data('regression', output=output)

            a = dlgbm.LGBMRegressor(local_listen_port=listen_port, seed=42, objective='quantile', alpha=alpha)
            a = a.fit(dX, dy, client=client, sample_weight=dw)
            p1 = a.predict(dX, client=client).compute()
            q1 = np.count_nonzero(y < p1) / y.shape[0]

            b = lightgbm.LGBMRegressor(seed=42, objective='quantile', alpha=alpha)
            b.fit(X, y, sample_weight=w)
            p2 = b.predict(X)
            q2 = np.count_nonzero(y < p2) / y.shape[0]

            # Quantiles should be right
            np.isclose(q1, alpha, atol=.1)
            np.isclose(q2, alpha, atol=.1)


def test_regressor_local_predict(loop, listen_port):
    with cluster() as (s, [a, b]):
        with Client(s['address'], loop=loop):
            X, y, w, dX, dy, dw = _create_data('regression', output="array")

            a = dlgbm.LGBMRegressor(local_listen_port=listen_port, seed=42)
            a = a.fit(dX, dy, sample_weight=dw)
            p1 = a.predict(dX)
            p2 = a.to_local().predict(X)
            s1 = r2_score(dy, p1)
            p1 = p1.compute()
            s2 = a.to_local().score(X, y)
            print(s1)

            # Predictions and scores should be the same
            assert_eq(p1, p2)
            np.isclose(s1, s2)


def test_build_network_params():
    workers_ips = [
        "tcp://192.168.0.1:34545",
        "tcp://192.168.0.2:34346",
        "tcp://192.168.0.3:34347"
    ]

    params = dlgbm.build_network_params(workers_ips, "tcp://192.168.0.2:34346", 12400, 120)
    exp_params = {
        "machines": "192.168.0.1:12400,192.168.0.2:12401,192.168.0.3:12402",
        "local_listen_port": 12401,
        "num_machines": len(workers_ips),
        "listen_time_out": 120
    }
    assert exp_params == params


@gen_cluster(client=True, timeout=None, check_new_threads=False)
def test_errors(c, s, a, b):
    def f(part):
        raise Exception('foo')
    df = dd.demo.make_timeseries()
    df = df.map_partitions(f, meta=df._meta)
    with pytest.raises(Exception) as info:
        yield dlgbm.train(c, df, df.x, params={}, model_factory=lightgbm.LGBMClassifier)
        assert 'foo' in str(info.value)

"""Minimal ray.data namespace for import-time compatibility."""

from __future__ import annotations

from . import read_api


class Dataset:
    pass


class DataContext:
    @classmethod
    def get_current(cls):
        return cls()


def _unavailable(*_args, **_kwargs):
    raise RuntimeError("ray.data is stubbed for Data-Juicer default executor")


read_parquet = _unavailable
read_csv = _unavailable
read_text = _unavailable
read_numpy = _unavailable
read_tfrecords = _unavailable
read_lance = _unavailable
read_json = _unavailable
read_webdataset = _unavailable
read_datasource = _unavailable

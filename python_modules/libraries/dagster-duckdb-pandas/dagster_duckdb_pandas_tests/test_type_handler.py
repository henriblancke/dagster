import os

import duckdb
import pandas as pd
import pytest
from dagster import (
    AssetIn,
    AssetKey,
    DailyPartitionsDefinition,
    DynamicPartitionsDefinition,
    MultiPartitionKey,
    MultiPartitionsDefinition,
    Out,
    StaticPartitionsDefinition,
    TimeWindowPartitionMapping,
    asset,
    graph,
    instance_for_test,
    materialize,
    op,
)
from dagster._check import CheckError
from dagster_duckdb_pandas import duckdb_pandas_io_manager


@op(out=Out(metadata={"schema": "a_df"}))
def a_df() -> pd.DataFrame:
    return pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})


@op(out=Out(metadata={"schema": "add_one"}))
def add_one(df: pd.DataFrame):
    return df + 1


@graph
def add_one_to_dataframe():
    add_one(a_df())


def test_duckdb_io_manager_with_ops(tmp_path):
    resource_defs = {
        "io_manager": duckdb_pandas_io_manager.configured(
            {"database": os.path.join(tmp_path, "unit_test.duckdb")}
        ),
    }

    job = add_one_to_dataframe.to_job(resource_defs=resource_defs)

    # run the job twice to ensure that tables get properly deleted
    for _ in range(2):
        res = job.execute_in_process()

        assert res.success
        duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))

        out_df = duckdb_conn.execute("SELECT * FROM a_df.result").fetch_df()
        assert out_df["a"].tolist() == [1, 2, 3]

        out_df = duckdb_conn.execute("SELECT * FROM add_one.result").fetch_df()
        assert out_df["a"].tolist() == [2, 3, 4]

        duckdb_conn.close()


@asset(key_prefix=["my_schema"])
def b_df() -> pd.DataFrame:
    return pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})


@asset(key_prefix=["my_schema"])
def b_plus_one(b_df: pd.DataFrame) -> pd.DataFrame:
    return b_df + 1


def test_duckdb_io_manager_with_assets(tmp_path):
    resource_defs = {
        "io_manager": duckdb_pandas_io_manager.configured(
            {"database": os.path.join(tmp_path, "unit_test.duckdb")}
        ),
    }

    # materialize asset twice to ensure that tables get properly deleted
    for _ in range(2):
        res = materialize([b_df, b_plus_one], resources=resource_defs)
        assert res.success

        duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))

        out_df = duckdb_conn.execute("SELECT * FROM my_schema.b_df").fetch_df()
        assert out_df["a"].tolist() == [1, 2, 3]

        out_df = duckdb_conn.execute("SELECT * FROM my_schema.b_plus_one").fetch_df()
        assert out_df["a"].tolist() == [2, 3, 4]

        duckdb_conn.close()


@asset(key_prefix=["my_schema"], ins={"b_df": AssetIn("b_df", metadata={"columns": ["a"]})})
def b_plus_one_columns(b_df: pd.DataFrame) -> pd.DataFrame:
    return b_df + 1


def test_loading_columns(tmp_path):
    resource_defs = {
        "io_manager": duckdb_pandas_io_manager.configured(
            {"database": os.path.join(tmp_path, "unit_test.duckdb")}
        ),
    }

    # materialize asset twice to ensure that tables get properly deleted
    for _ in range(2):
        res = materialize([b_df, b_plus_one_columns], resources=resource_defs)
        assert res.success

        duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))

        out_df = duckdb_conn.execute("SELECT * FROM my_schema.b_df").fetch_df()
        assert out_df["a"].tolist() == [1, 2, 3]

        out_df = duckdb_conn.execute("SELECT * FROM my_schema.b_plus_one_columns").fetch_df()
        assert out_df["a"].tolist() == [2, 3, 4]

        assert out_df.shape[1] == 1

        duckdb_conn.close()


@op
def non_supported_type() -> int:
    return 1


@graph
def not_supported():
    non_supported_type()


def test_not_supported_type(tmp_path):
    resource_defs = {
        "io_manager": duckdb_pandas_io_manager.configured(
            {"database": os.path.join(tmp_path, "unit_test.duckdb")}
        ),
    }

    job = not_supported.to_job(resource_defs=resource_defs)

    with pytest.raises(
        CheckError,
        match="DuckDBIOManager does not have a handler for type '<class 'int'>'",
    ):
        job.execute_in_process()


@asset(
    partitions_def=DailyPartitionsDefinition(start_date="2022-01-01"),
    key_prefix=["my_schema"],
    metadata={"partition_expr": "time"},
    config_schema={"value": str},
)
def daily_partitioned(context) -> pd.DataFrame:
    partition = pd.Timestamp(context.asset_partition_key_for_output())
    value = context.op_config["value"]

    return pd.DataFrame(
        {
            "time": [partition, partition, partition],
            "a": [value, value, value],
            "b": [4, 5, 6],
        }
    )


def test_time_window_partitioned_asset(tmp_path):
    duckdb_io_manager = duckdb_pandas_io_manager.configured(
        {"database": os.path.join(tmp_path, "unit_test.duckdb")}
    )
    resource_defs = {"io_manager": duckdb_io_manager}

    materialize(
        [daily_partitioned],
        partition_key="2022-01-01",
        resources=resource_defs,
        run_config={"ops": {"my_schema__daily_partitioned": {"config": {"value": "1"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.daily_partitioned").fetch_df()

    assert out_df["a"].tolist() == ["1", "1", "1"]
    duckdb_conn.close()

    materialize(
        [daily_partitioned],
        partition_key="2022-01-02",
        resources=resource_defs,
        run_config={"ops": {"my_schema__daily_partitioned": {"config": {"value": "2"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.daily_partitioned").fetch_df()

    assert sorted(out_df["a"].tolist()) == ["1", "1", "1", "2", "2", "2"]
    duckdb_conn.close()

    materialize(
        [daily_partitioned],
        partition_key="2022-01-01",
        resources=resource_defs,
        run_config={"ops": {"my_schema__daily_partitioned": {"config": {"value": "3"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.daily_partitioned").fetch_df()

    assert sorted(out_df["a"].tolist()) == ["2", "2", "2", "3", "3", "3"]
    duckdb_conn.close()


@asset(
    partitions_def=StaticPartitionsDefinition(["red", "yellow", "blue"]),
    key_prefix=["my_schema"],
    metadata={"partition_expr": "color"},
    config_schema={"value": str},
)
def static_partitioned(context) -> pd.DataFrame:
    partition = context.asset_partition_key_for_output()
    value = context.op_config["value"]
    return pd.DataFrame(
        {
            "color": [partition, partition, partition],
            "a": [value, value, value],
            "b": [4, 5, 6],
        }
    )


def test_static_partitioned_asset(tmp_path):
    duckdb_io_manager = duckdb_pandas_io_manager.configured(
        {"database": os.path.join(tmp_path, "unit_test.duckdb")}
    )
    resource_defs = {"io_manager": duckdb_io_manager}

    materialize(
        [static_partitioned],
        partition_key="red",
        resources=resource_defs,
        run_config={"ops": {"my_schema__static_partitioned": {"config": {"value": "1"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.static_partitioned").fetch_df()
    assert out_df["a"].tolist() == ["1", "1", "1"]
    duckdb_conn.close()

    materialize(
        [static_partitioned],
        partition_key="blue",
        resources=resource_defs,
        run_config={"ops": {"my_schema__static_partitioned": {"config": {"value": "2"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.static_partitioned").fetch_df()
    assert sorted(out_df["a"].tolist()) == ["1", "1", "1", "2", "2", "2"]
    duckdb_conn.close()

    materialize(
        [static_partitioned],
        partition_key="red",
        resources=resource_defs,
        run_config={"ops": {"my_schema__static_partitioned": {"config": {"value": "3"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.static_partitioned").fetch_df()
    assert sorted(out_df["a"].tolist()) == ["2", "2", "2", "3", "3", "3"]
    duckdb_conn.close()


@asset(
    partitions_def=MultiPartitionsDefinition(
        {
            "time": DailyPartitionsDefinition(start_date="2022-01-01"),
            "color": StaticPartitionsDefinition(["red", "yellow", "blue"]),
        }
    ),
    key_prefix=["my_schema"],
    metadata={"partition_expr": {"time": "CAST(time as TIMESTAMP)", "color": "color"}},
    config_schema={"value": str},
)
def multi_partitioned(context) -> pd.DataFrame:
    partition = context.partition_key.keys_by_dimension
    value = context.op_config["value"]
    return pd.DataFrame(
        {
            "color": [partition["color"], partition["color"], partition["color"]],
            "time": [partition["time"], partition["time"], partition["time"]],
            "a": [value, value, value],
        }
    )


def test_multi_partitioned_asset(tmp_path):
    duckdb_io_manager = duckdb_pandas_io_manager.configured(
        {"database": os.path.join(tmp_path, "unit_test.duckdb")}
    )
    resource_defs = {"io_manager": duckdb_io_manager}

    materialize(
        [multi_partitioned],
        partition_key=MultiPartitionKey({"time": "2022-01-01", "color": "red"}),
        resources=resource_defs,
        run_config={"ops": {"my_schema__multi_partitioned": {"config": {"value": "1"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.multi_partitioned").fetch_df()
    assert out_df["a"].tolist() == ["1", "1", "1"]
    duckdb_conn.close()

    materialize(
        [multi_partitioned],
        partition_key=MultiPartitionKey({"time": "2022-01-01", "color": "blue"}),
        resources=resource_defs,
        run_config={"ops": {"my_schema__multi_partitioned": {"config": {"value": "2"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.multi_partitioned").fetch_df()
    assert sorted(out_df["a"].tolist()) == ["1", "1", "1", "2", "2", "2"]
    duckdb_conn.close()

    materialize(
        [multi_partitioned],
        partition_key=MultiPartitionKey({"time": "2022-01-02", "color": "red"}),
        resources=resource_defs,
        run_config={"ops": {"my_schema__multi_partitioned": {"config": {"value": "3"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.multi_partitioned").fetch_df()
    assert sorted(out_df["a"].tolist()) == ["1", "1", "1", "2", "2", "2", "3", "3", "3"]
    duckdb_conn.close()

    materialize(
        [multi_partitioned],
        partition_key=MultiPartitionKey({"time": "2022-01-01", "color": "red"}),
        resources=resource_defs,
        run_config={"ops": {"my_schema__multi_partitioned": {"config": {"value": "4"}}}},
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.multi_partitioned").fetch_df()
    assert sorted(out_df["a"].tolist()) == ["2", "2", "2", "3", "3", "3", "4", "4", "4"]
    duckdb_conn.close()


dynamic_fruits = DynamicPartitionsDefinition(name="dynamic_fruits")


@asset(
    partitions_def=dynamic_fruits,
    key_prefix=["my_schema"],
    metadata={"partition_expr": "fruit"},
    config_schema={"value": str},
)
def dynamic_partitioned(context) -> pd.DataFrame:
    partition = context.asset_partition_key_for_output()
    value = context.op_config["value"]
    return pd.DataFrame(
        {
            "fruit": [partition, partition, partition],
            "a": [value, value, value],
        }
    )


def test_dynamic_partition(tmp_path):
    with instance_for_test() as instance:
        duckdb_io_manager = duckdb_pandas_io_manager.configured(
            {"database": os.path.join(tmp_path, "unit_test.duckdb")}
        )
        resource_defs = {"io_manager": duckdb_io_manager}

        instance.add_dynamic_partitions(dynamic_fruits.name, ["apple"])

        materialize(
            [dynamic_partitioned],
            partition_key="apple",
            resources=resource_defs,
            instance=instance,
            run_config={"ops": {"my_schema__dynamic_partitioned": {"config": {"value": "1"}}}},
        )

        duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
        out_df = duckdb_conn.execute("SELECT * FROM my_schema.dynamic_partitioned").fetch_df()
        assert out_df["a"].tolist() == ["1", "1", "1"]
        duckdb_conn.close()

        instance.add_dynamic_partitions(dynamic_fruits.name, ["orange"])

        materialize(
            [dynamic_partitioned],
            partition_key="orange",
            resources=resource_defs,
            instance=instance,
            run_config={"ops": {"my_schema__dynamic_partitioned": {"config": {"value": "2"}}}},
        )

        duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
        out_df = duckdb_conn.execute("SELECT * FROM my_schema.dynamic_partitioned").fetch_df()
        assert sorted(out_df["a"].tolist()) == ["1", "1", "1", "2", "2", "2"]
        duckdb_conn.close()

        materialize(
            [dynamic_partitioned],
            partition_key="apple",
            resources=resource_defs,
            instance=instance,
            run_config={"ops": {"my_schema__dynamic_partitioned": {"config": {"value": "3"}}}},
        )

        duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
        out_df = duckdb_conn.execute("SELECT * FROM my_schema.dynamic_partitioned").fetch_df()
        assert sorted(out_df["a"].tolist()) == ["2", "2", "2", "3", "3", "3"]
        duckdb_conn.close()


def test_self_dependent_asset(tmp_path):
    daily_partitions = DailyPartitionsDefinition(start_date="2023-01-01")

    @asset(
        partitions_def=daily_partitions,
        key_prefix=["my_schema"],
        ins={
            "self_dependent_asset": AssetIn(
                key=AssetKey(["my_schema", "self_dependent_asset"]),
                partition_mapping=TimeWindowPartitionMapping(start_offset=-1, end_offset=-1),
            ),
        },
        metadata={
            "partition_expr": "strptime(key, '%Y-%m-%d')",
        },
        config_schema={"value": str, "last_partition_key": str},
    )
    def self_dependent_asset(context, self_dependent_asset: pd.DataFrame) -> pd.DataFrame:
        key = context.asset_partition_key_for_output()

        if not self_dependent_asset.empty:
            assert len(self_dependent_asset.index) == 3
            assert (self_dependent_asset["key"] == context.op_config["last_partition_key"]).all()
        value = context.op_config["value"]
        pd_df = pd.DataFrame(
            {
                "key": [key, key, key],
                "a": [value, value, value],
            }
        )

        return pd_df

    duckdb_io_manager = duckdb_pandas_io_manager.configured(
        {"database": os.path.join(tmp_path, "unit_test.duckdb")}
    )
    resource_defs = {"io_manager": duckdb_io_manager}

    materialize(
        [self_dependent_asset],
        partition_key="2023-01-01",
        resources=resource_defs,
        run_config={
            "ops": {
                "my_schema__self_dependent_asset": {
                    "config": {"value": "1", "last_partition_key": "NA"}
                }
            }
        },
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.self_dependent_asset").fetch_df()

    assert out_df["a"].tolist() == ["1", "1", "1"]
    duckdb_conn.close()

    materialize(
        [self_dependent_asset],
        partition_key="2023-01-02",
        resources=resource_defs,
        run_config={
            "ops": {
                "my_schema__self_dependent_asset": {
                    "config": {"value": "2", "last_partition_key": "2023-01-01"}
                }
            }
        },
    )

    duckdb_conn = duckdb.connect(database=os.path.join(tmp_path, "unit_test.duckdb"))
    out_df = duckdb_conn.execute("SELECT * FROM my_schema.self_dependent_asset").fetch_df()

    assert out_df["a"].tolist() == ["1", "1", "1", "2", "2", "2"]
    duckdb_conn.close()

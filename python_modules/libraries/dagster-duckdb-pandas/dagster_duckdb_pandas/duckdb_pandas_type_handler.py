import duckdb
import pandas as pd
from dagster import InputContext, MetadataValue, OutputContext, TableColumn, TableSchema
from dagster._core.storage.db_io_manager import DbTypeHandler, TableSlice
from dagster_duckdb.io_manager import DuckDbClient, build_duckdb_io_manager


class DuckDBPandasTypeHandler(DbTypeHandler[pd.DataFrame]):
    """Stores and loads Pandas DataFrames in DuckDB.

    To use this type handler, pass it to ``build_duckdb_io_manager``

    Example:
        .. code-block:: python

            from dagster_duckdb import build_duckdb_io_manager
            from dagster_duckdb_pandas import DuckDBPandasTypeHandler

            @asset
            def my_table():
                ...

            duckdb_io_manager = build_duckdb_io_manager([DuckDBPandasTypeHandler()])

            @repository
            def my_repo():
                return with_resources(
                    [my_table],
                    {"io_manager": duckdb_io_manager.configured({"database": "my_db.duckdb"})}
                )

    """

    def handle_output(
        self, context: OutputContext, table_slice: TableSlice, obj: pd.DataFrame, connection
    ):
        """Stores the pandas DataFrame in duckdb."""
        connection.execute(
            f"create table if not exists {table_slice.schema}.{table_slice.table} as select * from"
            " obj;"
        )
        if not connection.fetchall():
            # table was not created, therefore already exists. Insert the data
            connection.execute(
                f"insert into {table_slice.schema}.{table_slice.table} select * from obj"
            )

        context.add_output_metadata(
            {
                "row_count": obj.shape[0],
                "dataframe_columns": MetadataValue.table_schema(
                    TableSchema(
                        columns=[
                            TableColumn(name=name, type=str(dtype))
                            for name, dtype in obj.dtypes.iteritems()
                        ]
                    )
                ),
            }
        )

    def load_input(
        self, context: InputContext, table_slice: TableSlice, connection
    ) -> pd.DataFrame:
        """Loads the input as a Pandas DataFrame."""
        try:
            return connection.execute(DuckDbClient.get_select_statement(table_slice)).fetchdf()
        except duckdb.CatalogException as e:
            if (
                "Schema with name" in e.args[0] or "Table with name" in e.args[0]
            ) and "does not exist" in e.args[0]:
                # table does not exist, so check if we are in a self dependent asset
                if (
                    context.upstream_output
                    and context.asset_key == context.upstream_output.asset_key
                ):
                    return pd.DataFrame()
            raise e

    @property
    def supported_types(self):
        return [pd.DataFrame]


duckdb_pandas_io_manager = build_duckdb_io_manager(
    [DuckDBPandasTypeHandler()], default_load_type=pd.DataFrame
)
duckdb_pandas_io_manager.__doc__ = """
An IO manager definition that reads inputs from and writes Pandas DataFrames to DuckDB. When
using the duckdb_pandas_io_manager, any inputs and outputs without type annotations will be loaded
as Pandas DataFrames.

Returns:
    IOManagerDefinition

Examples:

    .. code-block:: python

        from dagster_duckdb_pandas import duckdb_pandas_io_manager

        @asset(
            key_prefix=["my_schema"]  # will be used as the schema in DuckDB
        )
        def my_table() -> pd.DataFrame:  # the name of the asset will be the table name
            ...

        @repository
        def my_repo():
            return with_resources(
                [my_table],
                {"io_manager": duckdb_pandas_io_manager.configured({"database": "my_db.duckdb"})}
            )

    If you do not provide a schema, Dagster will determine a schema based on the assets and ops using
    the IO Manager. For assets, the schema will be determined from the asset key.
    For ops, the schema can be specified by including a "schema" entry in output metadata. If "schema" is not provided
    via config or on the asset/op, "public" will be used for the schema.

    .. code-block:: python

        @op(
            out={"my_table": Out(metadata={"schema": "my_schema"})}
        )
        def make_my_table() -> pd.DataFrame:
            # the returned value will be stored at my_schema.my_table
            ...

    To only use specific columns of a table as input to a downstream op or asset, add the metadata "columns" to the
    In or AssetIn.

    .. code-block:: python

        @asset(
            ins={"my_table": AssetIn("my_table", metadata={"columns": ["a"]})}
        )
        def my_table_a(my_table: pd.DataFrame) -> pd.DataFrame:
            # my_table will just contain the data from column "a"
            ...

"""

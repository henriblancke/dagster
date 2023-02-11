# start_marker
from dagster_aws.s3 import s3_pickle_io_manager, s3_resource

from dagster import Definitions, asset


@asset
def upstream_asset():
    return [1, 2, 3]


@asset
def downstream_asset(upstream_asset):
    return upstream_asset + [4]


defs = Definitions(
    assets=[upstream_asset, downstream_asset],
    resources={"io_manager": s3_pickle_io_manager, "s3": s3_resource},
)


# end_marker

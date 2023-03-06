from typing import TYPE_CHECKING, Any, Dict, Iterable, Mapping, Optional, Sequence, Type, Union

import dagster._check as check
from dagster._annotations import experimental, public
from dagster._config.structured_config import (
    ConfigurableResource,
    attach_resource_id_to_key_mapping,
)
from dagster._core.definitions.events import AssetKey, CoercibleToAssetKey
from dagster._core.definitions.executor_definition import ExecutorDefinition
from dagster._core.definitions.logger_definition import LoggerDefinition
from dagster._core.definitions.resource_definition import ResourceDefinition
from dagster._core.execution.build_resources import wrap_resources_for_execution
from dagster._core.execution.with_resources import with_resources
from dagster._core.executor.base import Executor
from dagster._core.instance import DagsterInstance
from dagster._utils.cached_method import cached_method

from .assets import AssetsDefinition, SourceAsset
from .cacheable_assets import CacheableAssetsDefinition
from .decorators import repository
from .job_definition import JobDefinition
from .partitioned_schedule import UnresolvedPartitionedAssetScheduleDefinition
from .repository_definition import (
    SINGLETON_REPOSITORY_NAME,
    PendingRepositoryDefinition,
    RepositoryDefinition,
)
from .schedule_definition import ScheduleDefinition
from .sensor_definition import SensorDefinition
from .unresolved_asset_job_definition import UnresolvedAssetJobDefinition

if TYPE_CHECKING:
    from dagster._core.storage.asset_value_loader import AssetValueLoader


@public
@experimental
def create_repository_using_definitions_args(
    name: str,
    assets: Optional[
        Iterable[Union[AssetsDefinition, SourceAsset, CacheableAssetsDefinition]]
    ] = None,
    schedules: Optional[
        Iterable[Union[ScheduleDefinition, UnresolvedPartitionedAssetScheduleDefinition]]
    ] = None,
    sensors: Optional[Iterable[SensorDefinition]] = None,
    jobs: Optional[Iterable[Union[JobDefinition, UnresolvedAssetJobDefinition]]] = None,
    resources: Optional[Mapping[str, Any]] = None,
    executor: Optional[Union[ExecutorDefinition, Executor]] = None,
    loggers: Optional[Mapping[str, LoggerDefinition]] = None,
) -> Union[RepositoryDefinition, PendingRepositoryDefinition]:
    """
    Create a named repository using the same arguments as :py:class:`Definitions`. In older
    versions of Dagster, repositories were the mechanism for organizing assets, schedules, sensors,
    and jobs. There could be many repositories per code location. This was a complicated ontology but
    gave users a way to organize code locations that contained large numbers of heterogenous definitions.

    As a stopgap for those who both want to 1) use the new :py:class:`Definitions` API and 2) but still
    want multiple logical groups of assets in the same code location, we have introduced this function.

    Example usage:

    .. code-block:: python

        named_repo = create_repository_using_definitions_args(
            name="a_repo",
            assets=[asset_one, asset_two],
            schedules=[a_schedule],
            sensors=[a_sensor],
            jobs=[a_job],
            resources={
                "a_resource": some_resource,
            }
        )

    """
    return _create_repository_using_definitions_args(
        name=name,
        assets=assets,
        schedules=schedules,
        sensors=sensors,
        jobs=jobs,
        resources=resources,
        executor=executor,
        loggers=loggers,
    )


def gen_key_for_nested_resource(resource_def: ResourceDefinition, counter: int) -> str:
    """
    Generates a unique identifier for a nested resource. This is used to identify it in the UI.
    URLs may break if the repository updates, but reloading the same repo will result in a
    deterministic set of nested resource keys.
    """
    return f"_nested_{counter}"


def _create_repository_using_definitions_args(
    name: str,
    assets: Optional[
        Iterable[Union[AssetsDefinition, SourceAsset, CacheableAssetsDefinition]]
    ] = None,
    schedules: Optional[
        Iterable[Union[ScheduleDefinition, UnresolvedPartitionedAssetScheduleDefinition]]
    ] = None,
    sensors: Optional[Iterable[SensorDefinition]] = None,
    jobs: Optional[Iterable[Union[JobDefinition, UnresolvedAssetJobDefinition]]] = None,
    resources: Optional[Mapping[str, Any]] = None,
    executor: Optional[Union[ExecutorDefinition, Executor]] = None,
    loggers: Optional[Mapping[str, LoggerDefinition]] = None,
):
    check.opt_iterable_param(
        assets, "assets", (AssetsDefinition, SourceAsset, CacheableAssetsDefinition)
    )
    check.opt_iterable_param(
        schedules, "schedules", (ScheduleDefinition, UnresolvedPartitionedAssetScheduleDefinition)
    )
    check.opt_iterable_param(sensors, "sensors", SensorDefinition)
    check.opt_iterable_param(jobs, "jobs", (JobDefinition, UnresolvedAssetJobDefinition))

    check.opt_inst_param(executor, "executor", (ExecutorDefinition, Executor))

    executor_def = (
        executor
        if isinstance(executor, ExecutorDefinition) or executor is None
        else ExecutorDefinition.hardcoded_executor(executor)
    )

    # Generate a mapping from each top-level resource instance ID to its resource key
    resource_key_mapping = {id(v): k for k, v in resources.items()} if resources else {}

    # Provide this mapping to each resource instance so that it can be used to resolve
    # nested resources
    resources_with_key_mapping = (
        {
            k: attach_resource_id_to_key_mapping(v, resource_key_mapping)
            for k, v in resources.items()
        }
        if resources
        else {}
    )
    nested_resources = {}

    nested_resource_ctr = 0
    if resources:
        for resource in resources.values():
            if isinstance(resource, ConfigurableResource):
                for nested_resource in resource.nested_resources.values():
                    if id(nested_resource) in resource_key_mapping:
                        continue
                    new_key = gen_key_for_nested_resource(nested_resource, nested_resource_ctr)
                    resource_key_mapping[id(nested_resource)] = new_key
                    nested_resources[new_key] = nested_resource
                    nested_resource_ctr += 1

    resource_defs = wrap_resources_for_execution(resources_with_key_mapping)
    nested_resource_defs = wrap_resources_for_execution(nested_resources)
    ui_visible_resources = {**resource_defs, **nested_resource_defs}

    check.opt_mapping_param(loggers, "loggers", key_type=str, value_type=LoggerDefinition)

    @repository(
        name=name,
        default_executor_def=executor_def,
        default_logger_defs=loggers,
        _top_level_resources=resource_defs,
        _ui_visible_resources=ui_visible_resources,
        _resource_key_mapping=resource_key_mapping,
    )
    def created_repo():
        return [
            *with_resources(assets or [], resource_defs),
            *(schedules or []),
            *(sensors or []),
            *(jobs or []),
        ]

    return created_repo


class Definitions:
    """
    A set of definitions explicitly available and loadable by Dagster tools.

    Parameters:
        assets (Optional[Iterable[Union[AssetsDefinition, SourceAsset, CacheableAssetsDefinition]]]):
            A list of assets. Assets can be created by annotating
            a function with :py:func:`@asset <asset>` or
            :py:func:`@observable_source_asset <observable_source_asset>`.
            Or they can by directly instantiating :py:class:`AssetsDefinition`,
            :py:class:`SourceAsset`, or :py:class:`CacheableAssetsDefinition`.

        schedules (Optional[Iterable[Union[ScheduleDefinition, UnresolvedPartitionedAssetScheduleDefinition]]]):
            List of schedules.

        sensors (Optional[Iterable[SensorDefinition]]):
            List of sensors, typically created with :py:func:`@sensor <sensor>`.

        jobs (Optional[Iterable[Union[JobDefinition, UnresolvedAssetJobDefinition]]]):
            List of jobs. Typically created with :py:func:`define_asset_job <define_asset_job>`
            or with :py:func:`@job <job>` for jobs defined in terms of ops directly.
            Jobs created with :py:func:`@job <job>` must already have resources bound
            at job creation time. They do not respect the `resources` argument here.

        resources (Optional[Mapping[str, Any]]): Dictionary of resources to bind to assets.
            The resources dictionary takes raw Python objects,
            not just instances of :py:class:`ResourceDefinition`. If that raw object inherits from
            :py:class:`IOManager`, it gets coerced to an :py:class:`IOManagerDefinition`.
            Any other object is coerced to a :py:class:`ResourceDefinition`.
            These resources will be automatically bound
            to any assets passed to this Definitions instance using
            :py:func:`with_resources <with_resources>`. Assets passed to Definitions with
            resources already bound using :py:func:`with_resources <with_resources>` will
            override this dictionary.

        executor (Optional[Union[ExecutorDefinition, Executor]]):
            Default executor for jobs. Individual jobs can override this and define their own executors
            by setting the executor on :py:func:`@job <job>` or :py:func:`define_asset_job <define_asset_job>`
            explicitly. This executor will also be used for materializing assets directly
            outside of the context of jobs. If an :py:class:`Executor` is passed, it is coerced into
            an :py:class:`ExecutorDefinition`.

        loggers (Optional[Mapping[str, LoggerDefinition]):
            Default loggers for jobs. Individual jobs
            can define their own loggers by setting them explictly.

    Example usage:

    .. code-block:: python

        defs = Definitions(
            assets=[asset_one, asset_two],
            schedules=[a_schedule],
            sensors=[a_sensor],
            jobs=[a_job],
            resources={
                "a_resource": some_resource,
            }
        )

    Dagster separates user-defined code from system tools such the web server and
    the daemon. Rather than loading code directly into process, a tool such as the
    webserver interacts with user-defined code over a serialization boundary.

    These tools must be able to locate and load this code when they start. Via CLI
    arguments or config, they specify a Python module to inspect.

    A Python module is loadable by Dagster tools if there is a top-level variable
    that is an instance of :py:class:`Definitions`.

    Before the introduction of :py:class:`Definitions`,
    :py:func:`@repository <repository>` was the API for organizing defintions.
    :py:class:`Definitions` provides a few conveniences for dealing with resources
    that do not apply to old-style :py:func:`@repository <repository>` declarations:

    * It takes a dictionary of top-level resources which are automatically bound
      (via :py:func:`with_resources <with_resources>`) to any asset passed to it.
      If you need to apply different resources to different assets, use legacy
      :py:func:`@repository <repository>` and use
      :py:func:`with_resources <with_resources>` as before.
    * The resources dictionary takes raw Python objects, not just instances
      of :py:class:`ResourceDefinition`. If that raw object inherits from
      :py:class:`IOManager`, it gets coerced to an :py:class:`IOManagerDefinition`.
      Any other object is coerced to a :py:class:`ResourceDefinition`.
    """

    def __init__(
        self,
        assets: Optional[
            Iterable[Union[AssetsDefinition, SourceAsset, CacheableAssetsDefinition]]
        ] = None,
        schedules: Optional[
            Iterable[Union[ScheduleDefinition, UnresolvedPartitionedAssetScheduleDefinition]]
        ] = None,
        sensors: Optional[Iterable[SensorDefinition]] = None,
        jobs: Optional[Iterable[Union[JobDefinition, UnresolvedAssetJobDefinition]]] = None,
        resources: Optional[Mapping[str, Any]] = None,
        executor: Optional[Union[ExecutorDefinition, Executor]] = None,
        loggers: Optional[Mapping[str, LoggerDefinition]] = None,
    ):
        self._created_pending_or_normal_repo = _create_repository_using_definitions_args(
            name=SINGLETON_REPOSITORY_NAME,
            assets=assets,
            schedules=schedules,
            sensors=sensors,
            jobs=jobs,
            resources=resources,
            executor=executor,
            loggers=loggers,
        )

    @public
    def get_job_def(self, name: str) -> JobDefinition:
        """Get a job definition by name. If you passed in a an :py:class:`UnresolvedAssetJobDefinition`
        (return value of :py:func:`define_asset_job`) it will be resolved to a :py:class:`JobDefinition` when returned
        from this function.
        """
        check.str_param(name, "name")
        return self.get_repository_def().get_job(name)

    @public
    def get_sensor_def(self, name: str) -> SensorDefinition:
        """Get a sensor definition by name."""
        check.str_param(name, "name")
        return self.get_repository_def().get_sensor_def(name)

    @public
    def get_schedule_def(self, name: str) -> ScheduleDefinition:
        """Get a schedule definition by name."""
        check.str_param(name, "name")
        return self.get_repository_def().get_schedule_def(name)

    @public
    def load_asset_value(
        self,
        asset_key: CoercibleToAssetKey,
        *,
        python_type: Optional[Type] = None,
        instance: Optional[DagsterInstance] = None,
        partition_key: Optional[str] = None,
    ) -> object:
        """
        Load the contents of an asset as a Python object.

        Invokes `load_input` on the :py:class:`IOManager` associated with the asset.

        If you want to load the values of multiple assets, it's more efficient to use
        :py:meth:`~dagster.Definitions.get_asset_value_loader`, which avoids spinning up
        resources separately for each asset.

        Args:
            asset_key (Union[AssetKey, Sequence[str], str]): The key of the asset to load.
            python_type (Optional[Type]): The python type to load the asset as. This is what will
                be returned inside `load_input` by `context.dagster_type.typing_type`.
            partition_key (Optional[str]): The partition of the asset to load.

        Returns:
            The contents of an asset as a Python object.
        """
        return self.get_repository_def().load_asset_value(
            asset_key=asset_key,
            python_type=python_type,
            instance=instance,
            partition_key=partition_key,
        )

    @public
    def get_asset_value_loader(
        self, instance: Optional[DagsterInstance] = None
    ) -> "AssetValueLoader":
        """
        Returns an object that can load the contents of assets as Python objects.

        Invokes `load_input` on the :py:class:`IOManager` associated with the assets. Avoids
        spinning up resources separately for each asset.

        Usage:

        .. code-block:: python

            with defs.get_asset_value_loader() as loader:
                asset1 = loader.load_asset_value("asset1")
                asset2 = loader.load_asset_value("asset2")
        """
        return self.get_repository_def().get_asset_value_loader(
            instance=instance,
        )

    def get_all_job_defs(self) -> Sequence[JobDefinition]:
        """Get all the Job definitions in the code location."""
        return self.get_repository_def().get_all_jobs()

    def has_implicit_global_asset_job_def(self) -> bool:
        return self.get_repository_def().has_implicit_global_asset_job_def()

    def get_implicit_global_asset_job_def(self) -> JobDefinition:
        """A useful conveninence method when there is a single defined global asset job.
        This occurs when all assets in the code location use a single partitioning scheme.
        If there are multiple partitioning schemes you must use get_implicit_job_def_for_assets
        instead to access to the correct implicit asset one.
        """
        return self.get_repository_def().get_implicit_global_asset_job_def()

    def get_implicit_job_def_for_assets(
        self, asset_keys: Iterable[AssetKey]
    ) -> Optional[JobDefinition]:
        return self.get_repository_def().get_implicit_job_def_for_assets(asset_keys)

    @cached_method
    def get_repository_def(self) -> RepositoryDefinition:
        """
        Definitions is implemented by wrapping RepositoryDefinition. Get that underlying object
        in order to access an functionality which is not exposed on Definitions. This method
        also resolves a PendingRepositoryDefinition to a RepositoryDefinition.
        """
        return (
            self._created_pending_or_normal_repo.compute_repository_definition()
            if isinstance(self._created_pending_or_normal_repo, PendingRepositoryDefinition)
            else self._created_pending_or_normal_repo
        )

    def get_inner_repository_for_loading_process(
        self,
    ) -> Union[RepositoryDefinition, PendingRepositoryDefinition]:
        """This method is used internally to access the inner repository during the loading process
        at CLI entry points. We explicitly do not want to resolve the pending repo because the entire
        point is to defer that resolution until later.
        """
        return self._created_pending_or_normal_repo

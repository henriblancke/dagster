from dagster._core.host_representation.handle import JobHandle
from dagster._core.storage.pipeline_run import DagsterRunStatus
from dagster._core.test_utils import (
    create_run_for_test,
    instance_for_test,
    poll_for_event,
    poll_for_finished_run,
)
from dagster._grpc.server import ExecuteExternalPipelineArgs
from dagster._grpc.types import StartRunResult
from dagster._serdes.serdes import deserialize_value

from .utils import get_bar_repo_repository_location


def _check_event_log_contains(event_log, expected_type_and_message):
    types_and_messages = [
        (e.dagster_event.event_type_value, e.message) for e in event_log if e.is_dagster_event
    ]
    for expected_event_type, expected_message_fragment in expected_type_and_message:
        assert any(
            event_type == expected_event_type and expected_message_fragment in message
            for event_type, message in types_and_messages
        )


def test_launch_run_with_unloadable_pipeline_grpc():
    with instance_for_test() as instance:
        with get_bar_repo_repository_location(instance) as repository_location:
            job_handle = JobHandle("foo", repository_location.get_repository("bar_repo").handle)
            api_client = repository_location.client

            run = create_run_for_test(instance, "foo")
            run_id = run.run_id

            original_origin = job_handle.get_external_origin()

            # point the api to a pipeline that cannot be loaded
            res = deserialize_value(
                api_client.start_run(
                    ExecuteExternalPipelineArgs(
                        pipeline_origin=original_origin._replace(
                            pipeline_name="i_am_fake_pipeline"
                        ),
                        pipeline_run_id=run_id,
                        instance_ref=instance.get_ref(),
                    )
                ),
                StartRunResult,
            )

            assert res.success
            finished_run = poll_for_finished_run(instance, run_id)

            assert finished_run
            assert finished_run.run_id == run_id
            assert finished_run.status == DagsterRunStatus.FAILURE

            poll_for_event(
                instance, run_id, event_type="ENGINE_EVENT", message="Process for run exited"
            )
            event_records = instance.all_logs(run_id)
            _check_event_log_contains(
                event_records,
                [
                    ("ENGINE_EVENT", "Started process for run"),
                    ("ENGINE_EVENT", "Could not load pipeline definition"),
                    (
                        "PIPELINE_FAILURE",
                        "This run has been marked as failed from outside the execution context",
                    ),
                    ("ENGINE_EVENT", "Process for run exited"),
                ],
            )


def test_launch_run_grpc():
    with instance_for_test() as instance:
        with get_bar_repo_repository_location(instance) as repository_location:
            job_handle = JobHandle("foo", repository_location.get_repository("bar_repo").handle)
            api_client = repository_location.client

            run = create_run_for_test(instance, "foo")
            run_id = run.run_id

            res = deserialize_value(
                api_client.start_run(
                    ExecuteExternalPipelineArgs(
                        pipeline_origin=job_handle.get_external_origin(),
                        pipeline_run_id=run_id,
                        instance_ref=instance.get_ref(),
                    )
                ),
                StartRunResult,
            )

            assert res.success
            finished_run = poll_for_finished_run(instance, run_id)

            assert finished_run
            assert finished_run.run_id == run_id
            assert finished_run.status == DagsterRunStatus.SUCCESS

            poll_for_event(
                instance, run_id, event_type="ENGINE_EVENT", message="Process for run exited"
            )
            event_records = instance.all_logs(run_id)
            _check_event_log_contains(
                event_records,
                [
                    ("ENGINE_EVENT", msg)
                    for msg in [
                        "Started process for run",
                        "Executing steps in process",
                        "Finished steps in process",
                        "Process for run exited",
                    ]
                ],
            )


def test_launch_unloadable_run_grpc():
    with instance_for_test() as instance:
        with get_bar_repo_repository_location(instance) as repository_location:
            job_handle = JobHandle("foo", repository_location.get_repository("bar_repo").handle)
            api_client = repository_location.client

            run = create_run_for_test(instance, "foo")
            run_id = run.run_id

            with instance_for_test() as other_instance:
                res = deserialize_value(
                    api_client.start_run(
                        ExecuteExternalPipelineArgs(
                            pipeline_origin=job_handle.get_external_origin(),
                            pipeline_run_id=run_id,
                            instance_ref=other_instance.get_ref(),
                        )
                    ),
                    StartRunResult,
                )

                assert not res.success
                assert (
                    "gRPC server could not load run {run_id} in order to execute it. "
                    "Make sure that the gRPC server has access to your run storage.".format(
                        run_id=run_id
                    )
                    in res.serializable_error_info.message
                )

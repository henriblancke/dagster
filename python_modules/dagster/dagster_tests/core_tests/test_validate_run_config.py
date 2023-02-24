import pytest
from dagster import validate_run_config
from dagster._core.definitions.decorators import op
from dagster._core.errors import DagsterInvalidConfigError
from dagster._legacy import pipeline


def test_validate_run_config():
    @op
    def basic():
        pass

    @pipeline
    def basic_pipeline():
        basic()

    validate_run_config(basic_pipeline)

    @op(config_schema={"foo": str})
    def requires_config(_):
        pass

    @pipeline
    def pipeline_requires_config():
        requires_config()

    result = validate_run_config(
        pipeline_requires_config, {"ops": {"requires_config": {"config": {"foo": "bar"}}}}
    )

    assert result == {
        "ops": {"requires_config": {"config": {"foo": "bar"}, "inputs": {}, "outputs": None}},
        "execution": {"in_process": {"retries": {"enabled": {}}}},
        "resources": {"io_manager": {"config": None}},
        "loggers": {},
    }

    result_with_storage = validate_run_config(
        pipeline_requires_config,
        {"ops": {"requires_config": {"config": {"foo": "bar"}}}},
    )

    assert result_with_storage == {
        "ops": {"requires_config": {"config": {"foo": "bar"}, "inputs": {}, "outputs": None}},
        "execution": {"in_process": {"retries": {"enabled": {}}}},
        "resources": {"io_manager": {"config": None}},
        "loggers": {},
    }

    with pytest.raises(DagsterInvalidConfigError):
        validate_run_config(pipeline_requires_config)

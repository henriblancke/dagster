from dagster._core.definitions import SourceAsset
from dagster._core.definitions.events import AssetKey
from dagster._core.definitions.metadata import MetadataEntry, MetadataValue


def test_source_asset_metadata():
    sa = SourceAsset(key=AssetKey("foo"), metadata={"foo": "bar", "baz": object()})
    assert sa.metadata_entries == [
        MetadataEntry(label="foo", value=MetadataValue.text("bar")),
        MetadataEntry(
            label="baz",
            value=MetadataValue.text("[object] (unserializable)"),
        ),
    ]
    assert sa.metadata == {
        "foo": MetadataValue.text("bar"),
        "baz": MetadataValue.text("[object] (unserializable)"),
    }


def test_source_asset_key_args():
    assert SourceAsset(key="foo").key == AssetKey(["foo"])
    assert SourceAsset(key=["bar", "foo"]).key == AssetKey(["bar", "foo"])

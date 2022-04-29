import pytest

import rubrix as rb
from rubrix import settings
from rubrix.settings import TextClassificationSettings, TokenClassificationSettings


@pytest.mark.parametrize(
    ("settings_", "wrong_settings"),
    [
        (
            TextClassificationSettings(labels_schema={"A", "B"}),
            TokenClassificationSettings(labels_schema={"PER", "ORG"}),
        ),
        (
            TokenClassificationSettings(labels_schema={"PER", "ORG"}),
            TextClassificationSettings(labels_schema={"A", "B"}),
        ),
    ],
)
def test_settings_workflow(mocked_client, settings_, wrong_settings):
    dataset = "test-dataset"
    rb.delete(dataset)
    settings.save_settings(dataset, settings=settings_)

    found_settings = settings.load_settings(dataset)
    assert found_settings == settings_

    with pytest.raises(
        ValueError, match="Provided settings are not compatible with dataset task."
    ):
        settings.save_settings(dataset, wrong_settings)


def test_settings_with_a_created_dataset(mocked_client):
    dataset = "dataset-name"
    rb.delete(dataset)
    rb.log(rb.TextClassificationRecord(text="The input text"), name=dataset)

    settings_ = rb.settings.load_settings(dataset)
    assert settings_ is None

    classification_settings = TextClassificationSettings(labels_schema={"L1", "L2"})
    rb.settings.save_settings(dataset, classification_settings)
    settings_ = rb.settings.load_settings(dataset)
    assert settings_ == classification_settings
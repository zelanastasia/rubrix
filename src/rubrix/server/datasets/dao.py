#  coding=utf-8
#  Copyright 2021-present, the Recognai S.L. team.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import json
from collections import defaultdict
from typing import Any, Dict, List, Optional, Type, TypeVar

from fastapi import Depends

from rubrix.server.commons.es_wrapper import ElasticsearchWrapper
from rubrix.server.tasks.commons import TaskType

from ..commons import es_helpers
from ..commons.errors import WrongTaskError
from ..commons.es_settings import DATASETS_INDEX_NAME, DATASETS_INDEX_TEMPLATE
from ..tasks.commons.dao.dao import DatasetRecordsDAO, dataset_records_index
from ..tasks.commons.task_factory import TaskFactory
from .model import DatasetDB

BaseDatasetDB = TypeVar("BaseDatasetDB", bound=DatasetDB)

NO_WORKSPACE = ""


class DatasetsDAO:
    """Datasets DAO"""

    _INSTANCE = None

    @classmethod
    def get_instance(
        cls,
        es: ElasticsearchWrapper = Depends(ElasticsearchWrapper.get_instance),
        records_dao: DatasetRecordsDAO = Depends(DatasetRecordsDAO.get_instance),
    ) -> "DatasetsDAO":
        """
        Gets or creates the dao instance

        Parameters
        ----------
        es:
            The elasticsearch wrapper dependency

        records_dao:
            The dataset records dao

        Returns
        -------
            The dao instance

        """
        if cls._INSTANCE is None:
            cls._INSTANCE = cls(es, records_dao)
        return cls._INSTANCE

    def __init__(self, es: ElasticsearchWrapper, records_dao: DatasetRecordsDAO):
        self._es = es
        self.__records_dao__ = records_dao
        self.init()

    def init(self):
        """Initializes dataset dao. Used on app startup"""
        self._es.create_index_template(
            name=DATASETS_INDEX_NAME,
            template=DATASETS_INDEX_TEMPLATE,
            force_recreate=True,
        )
        self._es.create_index(DATASETS_INDEX_NAME)

    def list_datasets(
        self,
        owner_list: List[str] = None,
        task_dataset_map: Optional[Dict[TaskType, Type[BaseDatasetDB]]] = None,
    ) -> List[BaseDatasetDB]:
        """
        List the dataset for an owner list

        Parameters
        ----------
        owner_list:
            The selected owners. Optional

        Returns
        -------
            A list of datasets for a given owner list, if any. All datasets, otherwise

        """
        filters = []
        if owner_list:
            owners_filter = es_helpers.filters.terms_filter("owner.keyword", owner_list)
            if NO_WORKSPACE in owner_list:
                filters.append(
                    es_helpers.filters.boolean_filter(
                        minimum_should_match=1,  # OR Condition
                        should_filters=[
                            es_helpers.filters.boolean_filter(
                                must_not_query=es_helpers.filters.exists_field("owner")
                            ),
                            owners_filter,
                        ],
                    )
                )
            else:
                filters.append(owners_filter)

        if task_dataset_map:
            filters.append(
                es_helpers.filters.terms_filter(
                    "task.keyword", [task for task in task_dataset_map]
                )
            )

        docs = self._es.list_documents(
            index=DATASETS_INDEX_NAME,
            query={
                "query": es_helpers.filters.boolean_filter(
                    should_filters=filters, minimum_should_match=len(filters)
                )
            }
            if filters
            else None,
        )

        if not task_dataset_map:
            task_dataset_map = defaultdict(lambda x: DatasetDB)

        return [
            self._es_doc_to_dataset(doc, ds_class=ds_class)
            for doc in docs
            for task in [doc["_source"]["task"]]
            for ds_class in [task_dataset_map[task]]
        ]

    def create_dataset(self, dataset: BaseDatasetDB) -> BaseDatasetDB:
        """
        Stores a dataset in elasticsearch and creates corresponding dataset records index

        Parameters
        ----------
        dataset:
            The dataset

        Returns
        -------
            Created dataset
        """

        self._es.add_document(
            index=DATASETS_INDEX_NAME,
            doc_id=dataset.id,
            document=self._dataset_to_es_doc(dataset),
        )
        self.__records_dao__.create_dataset_index(dataset, force_recreate=True)
        return dataset

    def update_dataset(
        self,
        dataset: BaseDatasetDB,
    ) -> BaseDatasetDB:
        """
        Updates an stored dataset

        Parameters
        ----------
        dataset:
            The dataset

        Returns
        -------
            The updated dataset

        """
        dataset_id = dataset.id

        self._es.update_document(
            index=DATASETS_INDEX_NAME,
            doc_id=dataset_id,
            document=self._dataset_to_es_doc(dataset),
            partial_update=True,
        )
        return dataset

    def delete_dataset(self, dataset: BaseDatasetDB):
        """
        Deletes indices related to provided dataset

        Parameters
        ----------
        dataset:
            The dataset

        """
        try:
            self._es.delete_index(dataset_records_index(dataset.id))
        finally:
            self._es.delete_document(index=DATASETS_INDEX_NAME, doc_id=dataset.id)

    def find_by_name(
        self,
        name: str,
        owner: Optional[str],
        task: Optional[TaskType] = None,
        as_dataset_class: Optional[Type] = None,
    ) -> Optional[BaseDatasetDB]:
        """
        Finds a dataset by name

        Args:
            name: The dataset name
            owner: The dataset owner
            task: If provided, expected dataset task
            as_dataset_class: If provided, it will be used as data model for found dataset.

        Returns:
            The found dataset if any. None otherwise

        """

        dataset_id = DatasetDB.build_dataset_id(
            name=name,
            owner=owner,
        )
        document = self._es.get_document_by_id(
            index=DATASETS_INDEX_NAME, doc_id=dataset_id
        )
        if not document and owner is None:
            # We must search by name since we have no owner
            results = self._es.list_documents(
                index=DATASETS_INDEX_NAME,
                query={"query": {"term": {"name.keyword": name}}},
            )
            results = list(results)
            if len(results) == 0:
                return None

            if len(results) > 1:
                raise ValueError(
                    f"Ambiguous dataset info found for name {name}. Please provide a valid owner"
                )

            document = results[0]

        if document is None:
            return None

        base_ds = self._es_doc_to_dataset(document)
        if task is None:
            return base_ds

        if task != base_ds.task:
            raise WrongTaskError(
                detail=f"Provided task {task} cannot be applied to dataset"
            )

        dataset_type = (
            as_dataset_class if as_dataset_class else TaskFactory.get_task_dataset(task)
        )
        return self._es_doc_to_dataset(document, ds_class=dataset_type)

    @staticmethod
    def _es_doc_to_dataset(
        doc: Dict[str, Any], ds_class: Type[BaseDatasetDB] = DatasetDB
    ) -> BaseDatasetDB:
        """Transforms a stored elasticsearch document into a `DatasetDB`"""

        def __key_value_list_to_dict__(
            key_value_list: List[Dict[str, Any]]
        ) -> Dict[str, Any]:
            return {data["key"]: json.loads(data["value"]) for data in key_value_list}

        source = doc["_source"]
        tags = source.get("tags", [])
        metadata = source.get("metadata", [])

        data = {
            **source,
            "tags": __key_value_list_to_dict__(tags),
            "metadata": __key_value_list_to_dict__(metadata),
        }

        return ds_class.parse_obj(data)

    @staticmethod
    def _dataset_to_es_doc(dataset: DatasetDB) -> Dict[str, Any]:
        def __dict_to_key_value_list__(data: Dict[str, Any]) -> List[Dict[str, Any]]:
            return [
                {"key": key, "value": json.dumps(value)} for key, value in data.items()
            ]

        data = dataset.dict(by_alias=True)
        tags = data.get("tags", {})
        metadata = data.get("metadata", {})

        return {
            **data,
            "tags": __dict_to_key_value_list__(tags),
            "metadata": __dict_to_key_value_list__(metadata),
        }

    def copy(self, source: DatasetDB, target: DatasetDB):
        source_doc = self._es.get_document_by_id(
            index=DATASETS_INDEX_NAME, doc_id=source.id
        )
        self._es.add_document(
            index=DATASETS_INDEX_NAME,
            doc_id=target.id,
            document={
                **source_doc["_source"],  # we copy extended fields from source document
                **self._dataset_to_es_doc(target),
            },
        )
        index_from = dataset_records_index(source.id)
        index_to = dataset_records_index(target.id)
        self._es.clone_index(index=index_from, clone_to=index_to)

    def close(self, dataset: DatasetDB):
        """Close a dataset. It's mean that release all related resources, like elasticsearch index"""
        self._es.close_index(dataset_records_index(dataset.id))

    def open(self, dataset: DatasetDB):
        """Make available a dataset"""
        self._es.open_index(dataset_records_index(dataset.id))

    def get_all_workspaces(self) -> List[str]:
        """Get all datasets (Only for super users)"""

        workspaces_dict = self._es.aggregate(
            index=DATASETS_INDEX_NAME,
            aggregation=es_helpers.aggregations.terms_aggregation(
                "owner.keyword",
                missing=NO_WORKSPACE,
                size=500,  # TODO: A max number of workspaces env var could be leveraged by this.
            ),
        )

        return [k for k in workspaces_dict]

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
import hashlib
import logging
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

import numpy as np

from rubrix import TextClassificationRecord
from rubrix.labeling.text_classification.weak_labels import WeakLabels, WeakMultiLabels

_LOGGER = logging.getLogger(__name__)

# When we break a tie, by how much shall we increase the probability of the winner?
_PROBABILITY_INCREASE_ON_TIE_BREAK = 0.0001


class TieBreakPolicy(Enum):
    """A tie break policy"""

    ABSTAIN = "abstain"
    RANDOM = "random"
    TRUE_RANDOM = "true-random"

    @classmethod
    def _missing_(cls, value):
        raise ValueError(
            f"{value} is not a valid {cls.__name__}, please select one of {list(cls._value2member_map_.keys())}"
        )


class LabelModel:
    """Abstract base class for a label model implementation.

    Args:
        weak_labels: Every label model implementation needs at least a `WeakLabels` instance.
    """

    def __init__(self, weak_labels: WeakLabels):
        self._weak_labels = weak_labels

    @property
    def weak_labels(self) -> WeakLabels:
        """The underlying `WeakLabels` object, containing the weak labels and records."""
        return self._weak_labels

    def fit(self, include_annotated_records: bool = False, *args, **kwargs):
        """Fits the label model.

        Args:
            include_annotated_records: Whether to include annotated records in the training.
        """
        raise NotImplementedError

    def score(self, *args, **kwargs) -> Dict:
        """Evaluates the label model."""
        raise NotImplementedError

    def predict(
        self,
        include_annotated_records: bool = False,
        prediction_agent: str = "LabelModel",
        **kwargs,
    ) -> List[TextClassificationRecord]:
        """Applies the label model.

        Args:
            include_annotated_records: Whether to include annotated records.
            prediction_agent: String used for the ``prediction_agent`` in the returned records.
            **kwargs: Specific to the label model implementations

        Returns:
            A list of records that include the predictions of the label model.
        """
        raise NotImplementedError


class MajorityVoter(LabelModel):
    """A basic label model that computes the majority vote across all rules.

    For multi-label classification, it will simply vote for all labels with a non-zero probability,
    that is labels that got at least one vote by the rules.

    Args:
        weak_labels: The weak labels object.
    """

    def __init__(self, weak_labels: Union[WeakLabels, WeakMultiLabels]):
        super().__init__(weak_labels=weak_labels)

    def fit(self, *args, **kwargs):
        raise NotImplementedError("No need to call fit on the 'MajorityVoter'!")

    def predict(
        self,
        include_annotated_records: bool = False,
        include_abstentions: bool = False,
        prediction_agent: str = "MajorityVoter",
        tie_break_policy: Union[TieBreakPolicy, str] = "abstain",
    ) -> List[TextClassificationRecord]:
        """Applies the label model.

        Args:
            include_annotated_records: Whether to include annotated records.
            include_abstentions: Whether to include records in the output, for which the label model abstained.
            prediction_agent: String used for the ``prediction_agent`` in the returned records.
            tie_break_policy: IGNORED FOR MULTI-LABEL! Policy to break ties. You can choose among two policies:

                - `abstain`: Do not provide any prediction
                - `random`: randomly choose among tied option using deterministic hash

                The last policy can introduce quite a bit of noise, especially when the tie is among many labels,
                as is the case when all the labeling functions (rules) abstained.

        Returns:
            A list of records that include the predictions of the label model.
        """
        wl_matrix = self._weak_labels.matrix(
            has_annotation=None if include_annotated_records else False
        )
        records = self._weak_labels.records(
            has_annotation=None if include_annotated_records else False
        )

        if isinstance(self._weak_labels, WeakMultiLabels):
            probabilities = self._compute_multi_label_probs(wl_matrix)

            return self._make_multi_label_records(
                probabilities=probabilities,
                records=records,
                include_abstentions=include_abstentions,
                prediction_agent=prediction_agent,
            )

        if isinstance(tie_break_policy, str):
            tie_break_policy = TieBreakPolicy(tie_break_policy)

        probabilities = self._compute_single_label_probs(wl_matrix)

        return self._make_single_label_records(
            probabilities=probabilities,
            records=records,
            include_abstentions=include_abstentions,
            prediction_agent=prediction_agent,
            tie_break_policy=tie_break_policy,
        )

    def _compute_single_label_probs(self, wl_matrix: np.ndarray) -> np.ndarray:
        """Helper methods that computes the probabilities.

        Args:
            wl_matrix: The weak label matrix.

        Returns:
            A matrix of "probabilities" with nr or records x nr of labels.
            The label order matches the one from `self.weak_labels.labels`.
        """
        counts = np.column_stack(
            [
                np.count_nonzero(
                    wl_matrix == self._weak_labels.label2int[label], axis=1
                )
                for label in self._weak_labels.labels
            ]
        )
        with np.errstate(invalid="ignore"):
            probabilities = counts / counts.sum(axis=1).reshape(len(counts), -1)

        # we treat abstentions as ties among all labels (see snorkel)
        probabilities[np.isnan(probabilities)] = 1.0 / len(self._weak_labels.labels)

        return probabilities

    def _make_single_label_records(
        self,
        probabilities: np.ndarray,
        records: List[TextClassificationRecord],
        include_abstentions: bool,
        prediction_agent: str,
        tie_break_policy: TieBreakPolicy,
    ):
        """Helper method to create records given predicted probabilities.

        Args:
            probabilities: The predicted probabilities.
            records: The records associated with the probabilities.
            include_abstentions: Whether to include records in the output, for which the label model abstained.
            prediction_agent: String used for the ``prediction_agent`` in the returned records.
            tie_break_policy: Policy to break ties. See the ``MajorityVoter.predict`` method.

        Returns:
            A list of records that include the predictions of the label model.
        """
        records_with_prediction = []
        for i, prob, rec in zip(range(len(records)), probabilities, records):
            # Check if model abstains, that is if the highest probability is assigned to more than one label
            # 1.e-8 is taken from the abs tolerance of np.isclose
            equal_prob_idx = np.nonzero(np.abs(prob.max() - prob) < 1.0e-8)[0]
            tie = False
            if len(equal_prob_idx) > 1:
                tie = True

            # maybe skip record
            if not include_abstentions and (
                tie and tie_break_policy is TieBreakPolicy.ABSTAIN
            ):
                continue

            if not tie:
                pred_for_rec = [
                    (self._weak_labels.labels[idx], prob[idx])
                    for idx in np.argsort(prob)[::-1]
                ]
            # resolve ties following the tie break policy
            elif tie_break_policy is TieBreakPolicy.ABSTAIN:
                pred_for_rec = None
            elif tie_break_policy is TieBreakPolicy.RANDOM:
                random_idx = int(hashlib.sha1(f"{i}".encode()).hexdigest(), 16) % len(
                    equal_prob_idx
                )
                for idx in equal_prob_idx:
                    if idx == random_idx:
                        prob[idx] += _PROBABILITY_INCREASE_ON_TIE_BREAK
                    else:
                        prob[idx] -= _PROBABILITY_INCREASE_ON_TIE_BREAK / (
                            len(equal_prob_idx) - 1
                        )
                pred_for_rec = [
                    (self._weak_labels.labels[idx], prob[idx])
                    for idx in np.argsort(prob)[::-1]
                ]
            else:
                raise NotImplementedError(
                    f"The tie break policy '{tie_break_policy.value}' is not implemented for {self.__class__.__name__}!"
                )

            records_with_prediction.append(rec.copy(deep=True))
            records_with_prediction[-1].prediction = pred_for_rec
            records_with_prediction[-1].prediction_agent = prediction_agent

        return records_with_prediction

    def _compute_multi_label_probs(self, wl_matrix: np.ndarray) -> np.ndarray:
        """Helper methods that computes the probabilities.

        Args:
            wl_matrix: The weak label matrix.

        Returns:
            A matrix of "probabilities" with nr or records x nr of labels.
            The label order matches the one from `self.weak_labels.labels`.
        """
        # turn abstentions (-1) into 0
        counts = np.where(wl_matrix == -1, 0, wl_matrix).sum(axis=1)
        # binary probability, predict all labels with at least one vote
        probabilities = np.where(counts > 0, 1, 0).astype(np.float16)

        all_rules_abstained = wl_matrix.sum(axis=1).sum(axis=1) == (
            -1 * self._weak_labels.cardinality * len(self._weak_labels.rules)
        )
        probabilities[all_rules_abstained] = [np.nan] * len(self._weak_labels.labels)

        return probabilities

    def _make_multi_label_records(
        self,
        probabilities: np.ndarray,
        records: List[TextClassificationRecord],
        include_abstentions: bool,
        prediction_agent: str,
    ) -> List[TextClassificationRecord]:
        """Helper method to create records given predicted probabilities.

        Args:
            probabilities: The predicted probabilities.
            records: The records associated with the probabilities.
            include_abstentions: Whether to include records in the output, for which the label model abstained.
            prediction_agent: String used for the ``prediction_agent`` in the returned records.

        Returns:
            A list of records that include the predictions of the label model.
        """
        records_with_prediction = []
        for prob, rec in zip(probabilities, records):
            all_abstained = np.isnan(prob).all()
            # maybe skip record
            if not include_abstentions and all_abstained:
                continue

            pred_for_rec = None
            if not all_abstained:
                pred_for_rec = [
                    (self._weak_labels.labels[i], prob[i])
                    for i in np.argsort(prob)[::-1]
                ]

            records_with_prediction.append(rec.copy(deep=True))
            records_with_prediction[-1].prediction = pred_for_rec
            records_with_prediction[-1].prediction_agent = prediction_agent

        return records_with_prediction

    def score(
        self,
        tie_break_policy: Union[TieBreakPolicy, str] = "abstain",
        output_str: bool = False,
    ) -> Union[Dict[str, float], str]:
        """Returns some scores/metrics of the label model with respect to the annotated records.

        The metrics are:

        - accuracy
        - micro/macro averages for precision, recall and f1
        - precision, recall, f1 and support for each label

        For more details about the metrics, check out the
        `sklearn docs <https://scikit-learn.org/stable/modules/generated/sklearn.metrics.precision_recall_fscore_support.html#sklearn-metrics-precision-recall-fscore-support>`__.

        Args:
            tie_break_policy: IGNORED FOR MULTI-LABEL! Policy to break ties. You can choose among two policies:

                - `abstain`: Do not provide any prediction
                - `random`: randomly choose among tied option using deterministic hash

                The last policy can introduce quite a bit of noise, especially when the tie is among many labels,
                as is the case when all the labeling functions (rules) abstained.
            output_str: If True, return output as nicely formatted string.

        Returns:
            The scores/metrics in a dictionary or as a nicely formatted str.

        .. note:: Metrics are only calculated over non-abstained predictions!

        Raises:
            MissingAnnotationError: If the ``weak_labels`` do not contain annotated records.
        """
        try:
            import sklearn
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "'sklearn' must be installed to compute the metrics! "
                "You can install 'sklearn' with the command: `pip install scikit-learn`"
            )
        from sklearn.metrics import classification_report

        wl_matrix = self._weak_labels.matrix(has_annotation=True)

        if isinstance(self._weak_labels, WeakMultiLabels):
            probabilities = self._compute_multi_label_probs(wl_matrix)

            annotation, prediction = self._score_multi_label(probabilities)
            target_names = self._weak_labels.labels
        else:
            if isinstance(tie_break_policy, str):
                tie_break_policy = TieBreakPolicy(tie_break_policy)

            probabilities = self._compute_single_label_probs(wl_matrix)

            annotation, prediction = self._score_single_label(
                probabilities, tie_break_policy
            )
            target_names = (self._weak_labels.labels[: annotation.max() + 1],)

        return classification_report(
            annotation,
            prediction,
            target_names=target_names,
            output_dict=not output_str,
        )

    def _score_single_label(
        self, probabilities: np.ndarray, tie_break_policy: TieBreakPolicy
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Helper method to compute scores for single-label classifications.

        Args:
            probabilities: The probabilities.
            tie_break_policy: Policy to break ties. You can choose among two policies:

                - `abstain`: Exclude from scores.
                - `random`: randomly choose among tied option using deterministic hash.

                The last policy can introduce quite a bit of noise, especially when the tie is among many labels,
                as is the case when all the labeling functions (rules) abstained.

        Returns:
            A tuple of the annotation and prediction array.
        """
        # 1.e-8 is taken from the abs tolerance of np.isclose
        is_max = (
            np.abs(probabilities.max(axis=1, keepdims=True) - probabilities) < 1.0e-8
        )
        is_tie = is_max.sum(axis=1) > 1

        prediction = np.argmax(is_max, axis=1)
        # we need to transform the indexes!
        annotation = np.array(
            [
                self._weak_labels.labels.index(self._weak_labels.int2label[i])
                for i in self._weak_labels.annotation()
            ],
            dtype=np.short,
        )

        if not is_tie.any():
            pass
        # resolve ties
        elif tie_break_policy is TieBreakPolicy.ABSTAIN:
            prediction, annotation = prediction[~is_tie], annotation[~is_tie]
        elif tie_break_policy is TieBreakPolicy.RANDOM:
            for i in np.nonzero(is_tie)[0]:
                equal_prob_idx = np.nonzero(is_max[i])[0]
                random_idx = int(hashlib.sha1(f"{i}".encode()).hexdigest(), 16) % len(
                    equal_prob_idx
                )
                prediction[i] = equal_prob_idx[random_idx]
        else:
            raise NotImplementedError(
                f"The tie break policy '{tie_break_policy.value}' is not implemented for MajorityVoter!"
            )

        return annotation, prediction

    def _score_multi_label(
        self, probabilities: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Helper method to compute scores for multi-label classifications.

        Args:
            probabilities: The probabilities.

        Returns:
            A tuple of the annotation and prediction array.
        """
        prediction = np.where(probabilities > 0.5, 1, 0)

        is_abstain = np.isnan(probabilities).all(axis=1)

        prediction, annotation = (
            prediction[~is_abstain],
            self._weak_labels.annotation()[~is_abstain],
        )

        return annotation, prediction


class Snorkel(LabelModel):
    """The label model by `Snorkel <https://github.com/snorkel-team/snorkel/>`__.

    Args:
        weak_labels: A `WeakLabels` object containing the weak labels and records.
        verbose: Whether to show print statements
        device: What device to place the model on ('cpu' or 'cuda:0', for example).
            Passed on to the `torch.Tensor.to()` calls.

    Examples:
        >>> from rubrix.labeling.text_classification import WeakLabels
        >>> weak_labels = WeakLabels(dataset="my_dataset")
        >>> label_model = Snorkel(weak_labels)
        >>> label_model.fit()
        >>> records = label_model.predict()
    """

    def __init__(
        self,
        weak_labels: Union[WeakLabels, WeakMultiLabels],
        verbose: bool = True,
        device: str = "cpu",
    ):
        try:
            import snorkel
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "'snorkel' must be installed to use the `Snorkel` label model! "
                "You can install 'snorkel' with the command: `pip install snorkel`"
            )
        else:
            from snorkel.labeling.model import LabelModel as SnorkelLabelModel

        super().__init__(weak_labels)

        if isinstance(weak_labels, WeakLabels):
            snorkel_model = SnorkelLabelModel(
                cardinality=self._weak_labels.cardinality,
                verbose=verbose,
                device=device,
            )
            self._model = _SnorkelSingleLabel(weak_labels, snorkel_model)
        else:
            snorkel_models = [
                SnorkelLabelModel(cardinality=2, verbose=verbose, device=device)
                for _ in range(weak_labels.cardinality)
            ]
            self._model = _SnorkelMultiLabel(weak_labels, snorkel_models)

    def fit(self, include_annotated_records: bool = False, **kwargs):
        """Fits the label model.

        Args:
            include_annotated_records: Whether to include annotated records in the training.
            **kwargs: Additional kwargs are passed on to Snorkel's
                `fit method <https://snorkel.readthedocs.io/en/latest/packages/_autosummary/labeling/snorkel.labeling.model.label_model.LabelModel.html#snorkel.labeling.model.label_model.LabelModel.fit>`__.
                They must not contain ``L_train``, the label matrix is provided automatically.
        """
        if "L_train" in kwargs:
            raise ValueError(
                "Your kwargs must not contain 'L_train', it is provided automatically."
            )

        l_train = self._weak_labels.matrix(
            has_annotation=None if include_annotated_records else False
        )

        self._model.fit(l_train, **kwargs)

    def predict(
        self,
        include_annotated_records: bool = False,
        include_abstentions: bool = False,
        prediction_agent: str = "Snorkel",
        tie_break_policy: Union[TieBreakPolicy, str] = "abstain",
    ) -> List[TextClassificationRecord]:
        """Returns a list of records that contain the predictions of the label model

        Args:
            include_annotated_records: Whether to include annotated records.
            include_abstentions: Whether to include records in the output, for which the label model abstained.
            prediction_agent: String used for the ``prediction_agent`` in the returned records.
            tie_break_policy: IGNORED FOR MULTI_LABEL! Policy to break ties. You can choose among three policies:

                - `abstain`: Do not provide any prediction
                - `random`: randomly choose among tied option using deterministic hash
                - `true-random`: randomly choose among the tied options. NOTE: repeated runs may have slightly different results due to differences in broken ties

                The last two policies can introduce quite a bit of noise, especially when the tie is among many labels,
                as is the case when all the labeling functions (rules) abstained.

        Returns:
            A list of records that include the predictions of the label model.
        """
        l_pred = self._weak_labels.matrix(
            has_annotation=None if include_annotated_records else False
        )
        predictions: List[Optional[List[Tuple[str, float]]]] = self._model.predict(
            l_pred, tie_break_policy=tie_break_policy
        )

        # add predictions to records
        records = self._weak_labels.records(
            has_annotation=None if include_annotated_records else False
        )
        records_with_prediction = []
        for rec, pred in zip(records, predictions):
            if not include_abstentions and pred is None:
                continue

            records_with_prediction.append(rec.copy(deep=True))
            records_with_prediction[-1].prediction = pred
            records_with_prediction[-1].prediction_agent = prediction_agent

        return records_with_prediction

    def score(
        self,
        tie_break_policy: Union[TieBreakPolicy, str] = "abstain",
        threshold: float = 0.5,
        output_str: bool = False,
    ) -> Union[Dict[str, float], str]:
        """Returns some scores/metrics of the label model with respect to the annotated records.

        The metrics are:

        - accuracy
        - micro/macro averages for precision, recall and f1
        - precision, recall, f1 and support for each label

        For more details about the metrics, check out the
        `sklearn docs <https://scikit-learn.org/stable/modules/generated/sklearn.metrics.precision_recall_fscore_support.html#sklearn-metrics-precision-recall-fscore-support>`__.

        Args:
            tie_break_policy: IGNORED FOR MULTI-LABEL. Policy to break ties. You can choose among three policies:

                - `abstain`: Do not provide any prediction
                - `random`: randomly choose among tied option using deterministic hash
                - `true-random`: randomly choose among the tied options. NOTE: repeated runs may have slightly different results due to differences in broken ties

                The last two policies can introduce quite a bit of noise, especially when the tie is among many labels,
                as is the case when all the labeling functions (rules) abstained.
            threshold: IGNORED FOR SINGLE-LABEL! The probability threshold (excluded) of a label to be accepted.
            output_str: If True, return output as nicely formatted string.

        Returns:
            The scores/metrics in a dictionary or as a nicely formatted str.

        .. note:: Metrics are only calculated over non-abstained predictions!

        Raises:
            MissingAnnotationError: If the ``weak_labels`` do not contain annotated records.
        """
        from sklearn.metrics import classification_report

        if self._weak_labels.annotation().size == 0:
            raise MissingAnnotationError(
                "You need annotated records to compute scores/metrics for your label model."
            )

        l_pred = self._weak_labels.matrix(has_annotation=True)

        annotation, prediction, target_names = self._model.score(
            self._weak_labels.annotation(),
            l_pred,
            tie_break_policy=tie_break_policy,
            threshold=threshold,
        )

        return classification_report(
            annotation,
            prediction,
            target_names=target_names,
            output_dict=not output_str,
        )


class _SnorkelSingleLabel:
    """Helper class for the Snorkel label model that holds the logic for the single label case.

    Args:
        weak_labels: The weak labels
        model: The snorkel model
    """

    def __init__(
        self, weak_labels: WeakLabels, model: "snorkel.labeling.model.LabelModel"
    ):
        self._weak_labels = weak_labels
        self._model = model

        # Check if we need to remap the "weak labels to int" mapping
        # Snorkel expects the abstain id to be -1 and the rest of the labels to be sequential
        self._need_remap = False
        self._weaklabelsInt2snorkelInt = {
            i: i for i in range(-1, weak_labels.cardinality)
        }
        if weak_labels.label2int[None] != -1 or sorted(weak_labels.int2label) != list(
            range(-1, weak_labels.cardinality)
        ):
            self._need_remap = True
            self._weaklabelsInt2snorkelInt = {
                weak_labels.label2int[label]: i
                for i, label in enumerate([None] + weak_labels.labels, -1)
            }

        self._snorkelInt2weaklabelsInt = {
            val: key for key, val in self._weaklabelsInt2snorkelInt.items()
        }

    def fit(self, l_train, **kwargs):
        """Fits the underlying Snorkel label model

        Args:
            l_train: The weak label matrix
            **kwargs: Passed on to the snorkel fit method

        Returns:
        """
        if self._need_remap:
            l_train = self._copy_and_remap(l_train)

        self._model.fit(L_train=l_train, **kwargs)

    def _copy_and_remap(self, matrix_or_array: np.ndarray):
        """Helper function to copy and remap the weak label matrix or annotation array to be compatible with snorkel.

        Snorkel expects the abstain id to be -1 and the rest of the labels to be sequential.

        Args:
            matrix_or_array: The original weak label matrix or annotation array

        Returns:
            A copy of the weak label matrix, remapped to match snorkel's requirements.
        """
        matrix_or_array = matrix_or_array.copy()

        # save masks for swapping
        label_masks = {}

        # compute masks
        for idx in self._weaklabelsInt2snorkelInt:
            label_masks[idx] = matrix_or_array == idx

        # swap integers
        for idx in self._weaklabelsInt2snorkelInt:
            matrix_or_array[label_masks[idx]] = self._weaklabelsInt2snorkelInt[idx]

        return matrix_or_array

    def predict(self, l_pred: np.ndarray, tie_break_policy: Union[str, TieBreakPolicy]):
        """Returns the predictions of the label model.

        Args:
            l_pred: The weak label matrix
            tie_break_policy: The policy to break ties, see the ``Snorkel.predict`` method.

        Returns:
            The predictions in Rubrix format
        """
        if isinstance(tie_break_policy, str):
            tie_break_policy = TieBreakPolicy(tie_break_policy)

        if self._need_remap:
            l_pred = self._copy_and_remap(l_pred)

        # get predictions and probabilities
        snorkel_predictions, probabilities = self._model.predict(
            L=l_pred,
            return_probs=True,
            tie_break_policy=tie_break_policy.value,
        )

        predictions = []
        for snorkel_pred, prob in zip(snorkel_predictions, probabilities):
            # set predictions to None if model abstained
            if snorkel_pred == -1:
                pred = None
            else:
                # If we have a tie, increase a bit the probability of the random winner (see tie_break_policy)
                # 1.e-8 is taken from the abs tolerance of np.isclose
                equal_prob_idx = np.nonzero(np.abs(prob.max() - prob) < 1.0e-8)[0]
                if len(equal_prob_idx) > 1:
                    for idx in equal_prob_idx:
                        if idx == snorkel_pred:
                            prob[idx] += _PROBABILITY_INCREASE_ON_TIE_BREAK
                        else:
                            prob[idx] -= _PROBABILITY_INCREASE_ON_TIE_BREAK / (
                                len(equal_prob_idx) - 1
                            )

                pred = [
                    (
                        self._weak_labels.int2label[
                            self._snorkelInt2weaklabelsInt[snorkel_idx]
                        ],
                        prob[snorkel_idx],
                    )
                    for snorkel_idx in np.argsort(prob)[::-1]
                ]

            predictions.append(pred)

        return predictions

    def score(
        self,
        annotation: np.ndarray,
        l_pred: np.ndarray,
        tie_break_policy: Union[str, TieBreakPolicy],
        **kwargs,
    ) -> Tuple[np.ndarray, np.ndarray, List[str]]:
        """Computes the annotation and prediction array

        Args:
            annotation: The annotation array
            l_pred: The weak label matrix
            tie_break_policy: Policy to break the ties, see ``Snorkel.score()``.
            **kwargs: We need this to absorb the threshold argument.

        Returns:
            The masked annotation and prediction array, and a list of target names.
        """
        predictions = self.predict(l_pred, tie_break_policy=tie_break_policy)
        prediction = np.array(
            [
                -1
                if pred is None
                else self._weaklabelsInt2snorkelInt[
                    self._weak_labels.label2int[pred[0][0]]
                ]
                for pred in predictions
            ]
        )
        # metrics are only calculated for non-abstained data points
        abstention_mask = prediction != -1

        annotation = annotation[abstention_mask]
        if self._need_remap:
            annotation = self._copy_and_remap(annotation)

        target_names = self._weak_labels.labels[: annotation.max() + 1]

        return annotation, prediction[abstention_mask], target_names


class _SnorkelMultiLabel:
    """Helper class for the Snorkel label model that holds the logic for the multi label case.

    Args:
        weak_labels: The weak labels.
        models: The snorkel models, one for each label.
    """

    def __init__(
        self,
        weak_labels: WeakMultiLabels,
        models: List["snorkel.labeling.model.LabelModel"],
    ):
        self._weak_labels = weak_labels
        self._models = models

    def fit(self, l_train: np.ndarray, **kwargs):
        """Fits the model for each label using a 1vsAll approach.

        Args:
            l_train: The 3D weak multi label matrix
        """
        for i, model in enumerate(self._models):
            model.fit(L_train=l_train[:, :, i], **kwargs)

    def predict(self, l_pred, **kwargs) -> List[Optional[List[Tuple[str, float]]]]:
        """Returns the predictions of the label models.

        Args:
            l_pred: The 3D weak multi label matrix
            **kwargs: we need to absorb the tie_break_policy parameter

        Returns:
            The sorted predictions in Rubrix format
        """
        # 1. compute probabilities for each label
        probs_per_label = np.full((l_pred.shape[0], l_pred.shape[2]), np.nan)
        for i, model in enumerate(self._models):
            _, probs = model.predict(
                L=l_pred[:, :, i],
                return_probs=True,
            )
            # we filter out the cases for which all rules abstained, in these cases snorkel assigns equal probabilities
            has_weak_labels = l_pred[:, :, i].sum(axis=1) != -1 * l_pred.shape[1]
            probs_for_1 = probs[has_weak_labels][:, 1]
            # Since in our 1vsAll approach, the "All" label will always win, we have to normalize
            # the "1" probability to get something meaningful. That's why we normalize it by its max value
            probs_per_label[:, i][has_weak_labels] = probs_for_1 / probs_for_1.max()

        # 2. construct predictions
        predictions = []
        for probs in probs_per_label:
            # if all nan we mark the record as abstained by setting the predictions to None
            if np.isnan(probs).all():
                preds = None
            else:
                preds = [
                    (label, (0.0 if np.isnan(prob) else prob))
                    for label, prob in zip(self._weak_labels.labels, probs)
                ]
                preds = sorted(preds, key=lambda p: p[1], reverse=True)

            predictions.append(preds)

        return predictions

    def score(
        self, annotation: np.ndarray, l_pred: np.ndarray, threshold: float, **kwargs
    ) -> Tuple[np.ndarray, np.ndarray, List[str]]:
        """Computes the annotation and prediction array.

        Args:
            annotation: The annotation array.
            l_pred: The weak label matrix.
            threshold: The probability threshold (excluded) to accept a label as prediction.
            **kwargs: We need this to absorb the tie_break_policy argument.

        Returns:
            The masked annotation and prediction array, and a list of target names.
        """
        predictions = self.predict(l_pred)
        prediction = -1 * np.ones_like(annotation)
        for i, pred in enumerate(predictions):
            if pred is None:
                continue
            pred.sort(key=lambda x: self._weak_labels.labels.index(x[0]))
            prediction[i] = np.array([1 if p[1] > threshold else 0 for p in pred])

        abstention_mask = prediction.sum(axis=1) > -1

        return (
            annotation[abstention_mask],
            prediction[abstention_mask],
            self._weak_labels.labels,
        )


class FlyingSquid(LabelModel):
    """The label model by `FlyingSquid <https://github.com/HazyResearch/flyingsquid>`__.

    Args:
        weak_labels: A `WeakLabels` object containing the weak labels and records.
        **kwargs: Passed on to the init of the FlyingSquid's
            `LabelModel <https://github.com/HazyResearch/flyingsquid/blob/master/flyingsquid/label_model.py#L18>`__.

    Examples:
        >>> from rubrix.labeling.text_classification import WeakLabels
        >>> weak_labels = WeakLabels(dataset="my_dataset")
        >>> label_model = FlyingSquid(weak_labels)
        >>> label_model.fit()
        >>> records = label_model.predict()
    """

    def __init__(self, weak_labels: WeakLabels, **kwargs):
        try:
            import flyingsquid
            import pgmpy
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "'flyingsquid' must be installed to use the `FlyingSquid` label model! "
                "You can install 'flyingsquid' with the command: `pip install pgmpy flyingsquid`"
            )
        else:
            from flyingsquid.label_model import LabelModel as FlyingSquidLabelModel

            self._FlyingSquidLabelModel = FlyingSquidLabelModel

        super().__init__(weak_labels)

        if len(self._weak_labels.rules) < 3:
            raise TooFewRulesError(
                "The FlyingSquid label model needs at least three (independent) rules!"
            )

        if "m" in kwargs:
            raise ValueError(
                "Your kwargs must not contain 'm', it is provided automatically."
            )

        self._init_kwargs = kwargs
        self._models: List[FlyingSquidLabelModel] = []

    def fit(self, include_annotated_records: bool = False, **kwargs):
        """Fits the label model.

        Args:
            include_annotated_records: Whether to include annotated records in the training.
            **kwargs: Passed on to the FlyingSquid's
                `LabelModel.fit() <https://github.com/HazyResearch/flyingsquid/blob/master/flyingsquid/label_model.py#L320>`__
                method.
        """
        wl_matrix = self._weak_labels.matrix(
            has_annotation=None if include_annotated_records else False
        )

        models = []
        # create a label model for each label (except for binary classification)
        # much of the implementation is taken from wrench:
        # https://github.com/JieyuZ2/wrench/blob/main/wrench/labelmodel/flyingsquid.py
        # If binary, we only need one model
        for i in range(
            1 if self._weak_labels.cardinality == 2 else self._weak_labels.cardinality
        ):
            model = self._FlyingSquidLabelModel(
                m=len(self._weak_labels.rules), **self._init_kwargs
            )
            wl_matrix_i = self._copy_and_transform_wl_matrix(wl_matrix, i)
            model.fit(L_train=wl_matrix_i, **kwargs)
            models.append(model)

        self._models = models

    def _copy_and_transform_wl_matrix(self, weak_label_matrix: np.ndarray, i: int):
        """Helper function to copy and transform the weak label matrix with respect to a target label.

         FlyingSquid expects the matrix to contain -1, 0 and 1, which are mapped the following way:

        - target label: -1
        - abstain label: 0
        - other label: 1

        Args:
            weak_label_matrix: The original weak label matrix
            i: Index of the target label

        Returns:
            A copy of the weak label matrix, transformed with respect to the target label.
        """
        wl_matrix_i = weak_label_matrix.copy()

        target_mask = (
            wl_matrix_i == self._weak_labels.label2int[self._weak_labels.labels[i]]
        )
        abstain_mask = wl_matrix_i == self._weak_labels.label2int[None]
        other_mask = (~target_mask) & (~abstain_mask)

        wl_matrix_i[target_mask] = -1
        wl_matrix_i[abstain_mask] = 0
        wl_matrix_i[other_mask] = 1

        return wl_matrix_i

    def predict(
        self,
        include_annotated_records: bool = False,
        include_abstentions: bool = False,
        prediction_agent: str = "FlyingSquid",
        verbose: bool = True,
        tie_break_policy: Union[TieBreakPolicy, str] = "abstain",
    ) -> List[TextClassificationRecord]:
        """Applies the label model.

        Args:
            include_annotated_records: Whether to include annotated records.
            include_abstentions: Whether to include records in the output, for which the label model abstained.
            prediction_agent: String used for the ``prediction_agent`` in the returned records.
            verbose: If True, print out messages of the progress to stderr.
            tie_break_policy: Policy to break ties. You can choose among two policies:

                - `abstain`: Do not provide any prediction
                - `random`: randomly choose among tied option using deterministic hash

                The last policy can introduce quite a bit of noise, especially when the tie is among many labels,
                as is the case when all the labeling functions (rules) abstained.

        Returns:
            A list of records that include the predictions of the label model.

        Raises:
            NotFittedError: If the label model was still not fitted.
        """
        if isinstance(tie_break_policy, str):
            tie_break_policy = TieBreakPolicy(tie_break_policy)

        wl_matrix = self._weak_labels.matrix(
            has_annotation=None if include_annotated_records else False
        )
        probabilities = self._predict(wl_matrix, verbose)

        # add predictions to records
        records_with_prediction = []
        for i, prob, rec in zip(
            range(len(probabilities)),
            probabilities,
            self._weak_labels.records(
                has_annotation=None if include_annotated_records else False
            ),
        ):
            # Check if model abstains, that is if the highest probability is assigned to more than one label
            # 1.e-8 is taken from the abs tolerance of np.isclose
            equal_prob_idx = np.nonzero(np.abs(prob.max() - prob) < 1.0e-8)[0]
            tie = False
            if len(equal_prob_idx) > 1:
                tie = True

            # maybe skip record
            if not include_abstentions and (
                tie and tie_break_policy is TieBreakPolicy.ABSTAIN
            ):
                continue

            if not tie:
                pred_for_rec = [
                    (self._weak_labels.labels[i], prob[i])
                    for i in np.argsort(prob)[::-1]
                ]
            # resolve ties following the tie break policy
            elif tie_break_policy is TieBreakPolicy.ABSTAIN:
                pred_for_rec = None
            elif tie_break_policy is TieBreakPolicy.RANDOM:
                random_idx = int(hashlib.sha1(f"{i}".encode()).hexdigest(), 16) % len(
                    equal_prob_idx
                )
                for idx in equal_prob_idx:
                    if idx == random_idx:
                        prob[idx] += _PROBABILITY_INCREASE_ON_TIE_BREAK
                    else:
                        prob[idx] -= _PROBABILITY_INCREASE_ON_TIE_BREAK / (
                            len(equal_prob_idx) - 1
                        )
                pred_for_rec = [
                    (self._weak_labels.labels[i], prob[i])
                    for i in np.argsort(prob)[::-1]
                ]
            else:
                raise NotImplementedError(
                    f"The tie break policy '{tie_break_policy.value}' is not implemented for FlyingSquid!"
                )

            records_with_prediction.append(rec.copy(deep=True))
            records_with_prediction[-1].prediction = pred_for_rec
            records_with_prediction[-1].prediction_agent = prediction_agent

        return records_with_prediction

    def _predict(self, weak_label_matrix: np.ndarray, verbose: bool) -> np.ndarray:
        """Helper function that calls the ``predict_proba`` method of FlyingSquid's label model.

        Much of the implementation is taken from wrench:
        https://github.com/JieyuZ2/wrench/blob/main/wrench/labelmodel/flyingsquid.py

        Args:
            weak_label_matrix: The weak label matrix.
            verbose: If True, print out messages of the progress to stderr.

        Returns:
            A matrix containing the probability for each label and record.

        Raises:
            NotFittedError: If the label model was still not fitted.
        """
        if not self._models:
            raise NotFittedError(
                "This FlyingSquid instance is not fitted yet. Call `fit` before using this model."
            )
        # create predictions for each label
        if self._weak_labels.cardinality > 2:
            probas = np.zeros((len(weak_label_matrix), self._weak_labels.cardinality))
            for i in range(self._weak_labels.cardinality):
                wl_matrix_i = self._copy_and_transform_wl_matrix(weak_label_matrix, i)
                probas[:, i] = self._models[i].predict_proba(
                    L_matrix=wl_matrix_i, verbose=verbose
                )[:, 0]
            probas = np.nan_to_num(probas, nan=-np.inf)  # handle NaN
            probas = np.exp(probas) / np.sum(np.exp(probas), axis=1, keepdims=True)
        # if binary, we only have one model
        else:
            wl_matrix_i = self._copy_and_transform_wl_matrix(weak_label_matrix, 0)
            probas = self._models[0].predict_proba(
                L_matrix=wl_matrix_i, verbose=verbose
            )

        return probas

    def score(
        self,
        tie_break_policy: Union[TieBreakPolicy, str] = "abstain",
        verbose: bool = False,
        output_str: bool = False,
    ) -> Union[Dict[str, float], str]:
        """Returns some scores/metrics of the label model with respect to the annotated records.

        The metrics are:

        - accuracy
        - micro/macro averages for precision, recall and f1
        - precision, recall, f1 and support for each label

        For more details about the metrics, check out the
        `sklearn docs <https://scikit-learn.org/stable/modules/generated/sklearn.metrics.precision_recall_fscore_support.html#sklearn-metrics-precision-recall-fscore-support>`__.

        Args:
            tie_break_policy: Policy to break ties. You can choose among two policies:

                - `abstain`: Do not provide any prediction
                - `random`: randomly choose among tied option using deterministic hash

                The last policy can introduce quite a bit of noise, especially when the tie is among many labels,
                as is the case when all the labeling functions (rules) abstained.
            verbose: If True, print out messages of the progress to stderr.
            output_str: If True, return output as nicely formatted string.

        Returns:
            The scores/metrics in a dictionary or as a nicely formatted str.

        .. note:: Metrics are only calculated over non-abstained predictions!

        Raises:
            NotFittedError: If the label model was still not fitted.
            MissingAnnotationError: If the ``weak_labels`` do not contain annotated records.
        """
        try:
            import sklearn
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "'sklearn' must be installed to compute the metrics! "
                "You can install 'sklearn' with the command: `pip install scikit-learn`"
            )
        from sklearn.metrics import classification_report

        if isinstance(tie_break_policy, str):
            tie_break_policy = TieBreakPolicy(tie_break_policy)

        wl_matrix = self._weak_labels.matrix(has_annotation=True)
        probabilities = self._predict(wl_matrix, verbose)

        # 1.e-8 is taken from the abs tolerance of np.isclose
        is_max = (
            np.abs(probabilities.max(axis=1, keepdims=True) - probabilities) < 1.0e-8
        )
        is_tie = is_max.sum(axis=1) > 1

        prediction = np.argmax(is_max, axis=1)
        # we need to transform the indexes!
        annotation = np.array(
            [
                self._weak_labels.labels.index(self._weak_labels.int2label[i])
                for i in self._weak_labels.annotation()
            ],
            dtype=np.short,
        )

        if not is_tie.any():
            pass
        # resolve ties
        elif tie_break_policy is TieBreakPolicy.ABSTAIN:
            prediction, annotation = prediction[~is_tie], annotation[~is_tie]
        elif tie_break_policy is TieBreakPolicy.RANDOM:
            for i in np.nonzero(is_tie)[0]:
                equal_prob_idx = np.nonzero(is_max[i])[0]
                random_idx = int(hashlib.sha1(f"{i}".encode()).hexdigest(), 16) % len(
                    equal_prob_idx
                )
                prediction[i] = equal_prob_idx[random_idx]
        else:
            raise NotImplementedError(
                f"The tie break policy '{tie_break_policy.value}' is not implemented for FlyingSquid!"
            )

        return classification_report(
            annotation,
            prediction,
            target_names=self._weak_labels.labels[: annotation.max() + 1],
            output_dict=not output_str,
        )


class LabelModelError(Exception):
    pass


class MissingAnnotationError(LabelModelError):
    pass


class TooFewRulesError(LabelModelError):
    pass


class NotFittedError(LabelModelError):
    pass

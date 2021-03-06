# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, division
from logging import getLogger

from concurrent.futures import ProcessPoolExecutor, wait

import numpy as np

from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold

from .basics.classifier_characteristics import ClassifierCharacteristics

logger = getLogger('disteval.recursive_selection')


def recursive_feature_selection_roc_auc(clf,
                                        X,
                                        y,
                                        sample_weight=None,
                                        n_features=10,
                                        cv_steps=10,
                                        n_jobs=1,
                                        forward=True,
                                        matching_features=True):
    """Method building a feature set in a recursive fashion. Depending
    on the setting it is run as a forward selection/backward elimination
    searching for a set of n features with the highest/lowest mismatch.
    To get the set with the size n starting from n_total features the
    following approaches are used:

    Forward Selection:
    To get the k+1 set every not yet selected feature is used to
    generate (n_total - k sets). The set with the best score is the
    k + 1 set. Those steps are repeated until n features are selected

    Backward Elimination:
    To get k+1 eliminated features every not yet eleminated feature is used
    to generate (n_total - k) sets. The sets consist of all not yet
    eliminated features minus the one that is tested. The set with the
    best score determines the next feature to eliminate. Those steps are
    repeated until n features are eliminated.

    What the best score depends also on the settings:
    matching_features:
        forward: min(|auc - 0.5|)
        not forward: max(|aux - 0.5|)

    not matching_features:
        forward: max(auc )
        not forward: min(aux)


    Parameters
    ----------
    clf: object
        Classifier that should be used for the classification.
        It needs a fit and a predict_proba function.

    X : numpy.float32array, shape=(n_samples, n_obs)
        Values describing the samples.

    y : numpy.float32array, shape=(n_samples)
        Array of the true labels.

    sample_weight : None or numpy.float32array, shape=(n_samples)
        If weights are used this has to contains the sample weights.
        None in the case of no weights.

    n_features : int, optional (default=10)
        Number of feature that are selected (forward=True) or eliminated
        (forward=False)

    n_jobs: int, optional (default=1)
        Number of parallel jobs spawned in each a classification in run.
        Total number of used cores is the product of n_jobs from the clf
        and the n_jobs of this function.

    forward: bool, optional (default=True)
        If True it is a 'forward selection'. If False it is a 'backward
        elimination'.

    matching_features: bool, optional (default=True)
        Wether for matching or mismatching feature should be searched

    Returns
    -------
    selected_features: list of ints
        Return a list containing the indeces of X, that were
        selected/eliminated. The order corresponds to the order the
        features were selected/eliminated.

    auc_scores: np.array float shape(n_features_total, n_features)
        Return a array containing the auc values for all steps.
        np.nan is the feature was already selected in the specific run.
    """
    desired_characteristics = ClassifierCharacteristics()
    desired_characteristics.opts['callable:fit'] = True
    desired_characteristics.opts['callable:predict_proba'] = True

    clf_characteristics = ClassifierCharacteristics(clf)
    assert clf_characteristics.fulfilling(desired_characteristics), \
        'Classifier sanity check failed!'

    if n_features > X.shape[1]:
        logger.info(' \'n_features\' higher than total number of features.'
                    ' \'n_features\' reduced!')
        n_features = X.shape[1]
    auc_scores = np.zeros((X.shape[1], n_features))
    selected_features = []

    while len(selected_features) != n_features:
        auc_scores_i = get_all_auc_scores(clf,
                                          selected_features,
                                          X,
                                          y,
                                          sample_weight=sample_weight,
                                          cv_steps=cv_steps,
                                          n_jobs=n_jobs,
                                          forward=forward)
        value_best = None
        index_best = None
        for idx, auc in enumerate(auc_scores_i):
            if not np.isfinite(auc):
                continue
            if value_best is None:
                value_best = auc
                index_best = idx
            if matching_features:
                if forward:
                    if np.abs(auc - 0.5) < np.abs(value_best - 0.5):
                        value_best = auc
                        index_best = idx
                else:
                    if np.abs(auc - 0.5) > np.abs(value_best - 0.5):
                        value_best = auc
                        index_best = idx
            else:
                if forward:
                    if auc > value_best:
                        value_best = auc
                        index_best = idx
                else:
                    if auc < value_best:
                        value_best = auc
                        index_best = idx
        auc_scores[:, len(selected_features)] = auc_scores_i
        selected_features.append(index_best)
    return selected_features, auc_scores



def __single_auc_score__(feature_i,
                         clf,
                         cv_indices,
                         X,
                         y,
                         sample_weight=None):
    """Method determining the 'area under curve' for a single test set.
    This function is intended for internal use.
    Parameters
    ----------
    feature_i: int
        Index of the tested feature.

    clf: object
        Classifier that should be used for the classification.
        It needs a fit and a predict_proba function.

    cv_indices: list of tuples
        Indices for all the cross validation steps. They are explicit
        pass, so all test sets use the same splitting.

    X : numpy.float32array, shape=(n_samples, n_obs)
        Values describing the samples.

    y : numpy.float32array, shape=(n_samples)
        Array of the true labels.

    sample_weight : None or numpy.float32array, shape=(n_samples)
        If weights are used this has to contain the sample weights.
        None in the case of no weights.

    Returns
    -------
    feature_i: int
        Index of the tested feature. It is need as a return value for
        asynchronous parallel processing

    auc_score: float
        Returns calculated auc score.
    """
    y_pred = np.zeros_like(y, dtype=float)
    for i, [train_idx, test_idx] in enumerate(cv_indices):
        X_train = X[train_idx]
        X_test = X[test_idx]
        y_train = y[train_idx]
        if sample_weight is None:
            sample_weight_train = None
            sample_weight_test = None
        else:
            sample_weight_train = sample_weight[train_idx]
            sample_weight_test = sample_weight[test_idx]
        clf = clf.fit(X=X_train,
                      y=y_train,
                      sample_weight=sample_weight_train)
    y_pred[test_idx] = clf.predict_proba(X_test)[:, 1]
    auc_score = roc_auc_score(y, y_pred, sample_weight=sample_weight_test)
    return feature_i, auc_score


def get_all_auc_scores(clf,
                       selected_features,
                       X,
                       y,
                       sample_weight=None,
                       cv_steps=10,
                       n_jobs=1,
                       forward=True,
                       random_state=None):
    """Method determining the 'area under curve' for all not yet
    selected features. In this function also the feature sets for the
    tests are created.
    Parameters
    ----------
    clf: object
        Classifier that should be used for the classification.
        It needs a fit and a predict_proba function.

    selected_features: list of ints
        List of already selected features

    X : numpy.float32array, shape=(n_samples, n_obs)
        Values describing the samples.

    y : numpy.float32array, shape=(n_samples)
        Array of the true labels.

    sample_weight : None or numpy.float32array, shape=(n_samples)
        If weights are used this has to contains the sample weights.
        None in the case of no weights.

    n_jobs: int, optional (default=1)
        Number of parallel jobs spawned in each a classification in run.
        Total number of used cores is the product of n_jobs from the clf
        and the n_jobs of this function.

    forward: bool, optional (default=True)
        If True it is a 'forward selection'. If False it is a 'backward
        elimination'.

    random_state: None, int or RandomState
        If int, random_state is the seed used by the random number generator;
        If RandomState instance, random_state is the random number generator;
        If None, the random number generator is the RandomState instance used
        by np.random.

    Returns
    -------
    auc_scores: np.array float shape(n_features_total)
        Return a array containing the auc values. np.nan is the feature
        is already selected.
    """
    if not isinstance(random_state, np.random.RandomState):
        random_state = np.random.RandomState(random_state)
    selected_features = np.array(selected_features, dtype=int)
    if cv_steps < 2:
        raise ValueError('\'cv_steps\' must be 2 or higher')
    else:
        cv_iterator = StratifiedKFold(n_splits=cv_steps,
                                      shuffle=True,
                                      random_state=random_state)
        cv_indices = [[train, test] for train, test in cv_iterator.split(X, y)]
    test_features = np.array([int(i) for i in range(X.shape[1])
                              if i not in selected_features], dtype=int)

    process_args = []
    for feature_i in test_features:
        if forward:
            set_i = np.hstack((selected_features, feature_i))
            test_set = np.sort(set_i)
        else:
            set_i = list(test_features)
            set_i.remove(feature_i)
            test_set = np.array(set_i)
        process_args.append([feature_i, X[:, test_set],
                             y,
                             sample_weight,
                             clf])

    test_sets = {}
    for feature_i in test_features:
        if forward:
            set_i = np.hstack((selected_features, feature_i))
            test_sets[feature_i] = np.sort(set_i)
        else:
            set_i = list(test_features)
            set_i.remove(feature_i)
            test_sets[feature_i] = np.array(set_i)

    auc_scores = np.empty(X.shape[1])
    auc_scores[:] = np.nan
    if n_jobs > 1:
        futures = []
        with ProcessPoolExecutor(max_workers=n_jobs) as executor:
            for feature_i, test_set in test_sets.items():
                futures.append(executor.submit(__single_auc_score__,
                                               feature_i=feature_i,
                                               clf=clf,
                                               cv_indices=cv_indices,
                                               X=X[:, test_set],
                                               y=y,
                                               sample_weight=sample_weight))
        results = wait(futures)
        for future_i in results.done:
            feature_i, auc = future_i.result()
            auc_scores[feature_i] = auc
    else:
        auc_scores = []
        for feature_i, test_set in test_sets.items():
            _, auc = __single_auc_score__(feature_i=feature_i,
                                          clf=clf,
                                          cv_indices=cv_indices,
                                          X=X[:, test_set],
                                          y=y,
                                          sample_weight=sample_weight)
            auc_scores[feature_i] = auc
    return auc_scores




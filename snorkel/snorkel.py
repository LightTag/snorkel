# Base Python
import cPickle, json, os, sys, warnings
from collections import defaultdict, OrderedDict, namedtuple
import lxml.etree as et

# Scientific modules
import numpy as np
import matplotlib
matplotlib.use('Agg')
warnings.filterwarnings("ignore", module="matplotlib")
warnings.filterwarnings("ignore", category=DeprecationWarning)
import matplotlib.pyplot as plt
import scipy.sparse as sparse
from features import Featurizer
from learning import LogReg, odds_to_prob
from lstm import *
from learning_utils import test_scores, calibration_plots


class TrainingSet(object):
    """
    Wrapper data object which applies the LFs to the candidates comprising the training set,
    featurizes them, and then stores the resulting _noisy training set_, as well as the LFs and featurizer.

    As input takes:
        - A set of Candidate objects comprising the training set
        - A set of labeling functions (LFs) which are functions f : Candidate -> {-1,0,1}
        - A Featurizer object, which is applied to the Candidate objects to generate features
    """
    def __init__(self, training_candidates, lfs, featurizer=None):
        self.training_candidates = training_candidates
        self.featurizer          = featurizer
        self.lfs                 = lfs
        self.L, self.F           = self.transform(self.training_candidates, fit=True)

    def transform(self, candidates, fit=False):
        """Apply LFs and featurize the candidates"""
        print "Applying LFs..."
        L = self._apply_lfs(candidates)
        F = None
        if self.featurizer is not None:
            print "Featurizing..."
            F = self.featurizer.fit_transform(candidates) if fit else self.featurizer.transform(candidates)
        return L, F

    def _apply_lfs(self, candidates):
        """Apply the labeling functions to the candidates to populate X"""
        X = sparse.lil_matrix((len(candidates), len(self.lfs)))
        for i,c in enumerate(candidates):
            for j,lf in enumerate(self.lfs):
                X[i,j] = lf(c)
        return X.tocsr()


class Learner(object):
    """
    Core learning class for Snorkel, encapsulating the overall process of learning a generative model of the
    training data set (specifically: of the LF-emitted labels and the true class labels), and then using this
    to train a given noise-aware discriminative model.

    As input takes a TrainingSet object and a NoiseAwareModel object (the discriminative model to train).
    """
    # TODO: Tuner (GridSearch) class that wraps this! 
    def __init__(self, training_set, model=None):
        self.training_set = training_set
        self.model        = model

        # Derived objects from the training set
        self.L_train         = self.training_set.L
        self.n_train, self.m = self.L_train.shape
        self.F_train         = self.training_set.F
        self.f               = self.F_train.shape[1]
        self.X_train         = None

        # Cache the transformed test set as well
        self.test_candidates = None
        self.gold_labels     = None
        self.X_test          = None

    def _set_model_X(self, L, F):
        """Given LF matrix L, feature matrix F, return the matrix used by the end discriminative model."""
        return sparse.hstack([L, F], format='csc')

    def train(self, lf_w0=5.0, feat_w0=0.0, **model_hyperparams):
        """Train model: **as default, use "joint" approach**"""
        # TODO: Bias term
        # Set the initial weights for LFs and feats
        w0 = np.concatenate([lf_w0*np.ones(self.m), feat_w0*np.ones(self.f)])

        # Construct matrix X for "joint" approach
        self.X_train = self._set_model_X(self.L_train, self.F_train)

        # Train model
        self.model.train(self.X_train, w0=w0, **model_hyperparams)

    def test(self, test_candidates, gold_labels, show_plots=True):
        """
        Apply the LFs and featurize the test candidates, using the same transformation as in training set;
        then test against gold labels using trained model.
        """
        # Cache transformed test set
        if self.X_test is None or test_candidates != self.test_candidates or any(gold_labels != self.gold_labels):
            self.test_candidates = test_candidates
            self.gold_labels     = gold_labels
            L_test, F_test       = self.training_set.transform(test_candidates)
            self.X_test          = self._set_model_X(L_test, F_test)
        test_scores(self.model.predict(self.X_test), gold_labels, return_vals=False, verbose=True)

        # Optionally, plot calibration plots
        if show_plots:
            calibration_plots(self.model.marginals(self.X_train), self.model.marginals(self.X_test), gold_labels)

    def lf_weights(self):
        return self.model.w[:self.m]

    def lf_accs(self):
        return odds_to_prob(self.lf_weights())

    def feat_weights(self):
        return self.model.w[self.m:]
        

class PipelinedLearner(Learner):
    """Implements the **"pipelined" approach**"""
    def _set_model_X(self, L, F):
        return F.tocsc()

    def train(self, feat_w0=0.0, lf_w0=1.0, **model_hyperparams):
        """Train model: **as default, use "joint" approach**"""
        w0_1 = lf_w0*np.ones(self.m)
        w0_2 = feat_w0*np.ones(self.f)

        # Learn lf accuracies first
        self.training_model = LogReg()
        self.training_model.train(self.L_train, w0=w0_1, **model_hyperparams)

        # Compute marginal probabilities over the candidates from this model of the training set
        training_marginals = self.training_model.marginals(self.L_train)

        # Learn model over features
        self.X_train = self._set_model_X(self.L_train, self.F_train)
        self.w       = self.model.train(self.X_train, training_marginals=training_marginals, w0=w0_2, \
            **model_hyperparams)

    def lf_weights(self):
        return self.training_model.w

    def feat_weights(self):
        return self.model.w

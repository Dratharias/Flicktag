"""L2-regularised logistic regression, fitted with Newton's method (IRLS).

Small enough to retrain on every few validations: a few hundred samples of a
768-dimensional embedding take milliseconds.
"""

from __future__ import annotations

import numpy as np


class LogisticRegression:
    def __init__(self, l2: float = 1.0, max_iter: int = 25, tol: float = 1e-6):
        self.l2 = l2
        self.max_iter = max_iter
        self.tol = tol
        self.weights: np.ndarray | None = None  # last entry is the bias

    @staticmethod
    def _augment(x: np.ndarray) -> np.ndarray:
        return np.hstack([x, np.ones((x.shape[0], 1), dtype=x.dtype)])

    def fit(self, x: np.ndarray, y: np.ndarray) -> "LogisticRegression":
        x = self._augment(np.asarray(x, dtype=np.float64))
        y = np.asarray(y, dtype=np.float64)
        # Balance classes so a tag seen on 5% of images is not always predicted off.
        positives = max(y.sum(), 1.0)
        negatives = max(len(y) - y.sum(), 1.0)
        sample_weight = np.where(y > 0.5, len(y) / (2 * positives), len(y) / (2 * negatives))

        n_features = x.shape[1]
        penalty = np.full(n_features, self.l2)
        penalty[-1] = 0.0  # never penalise the bias
        w = np.zeros(n_features)
        for _ in range(self.max_iter):
            p = _sigmoid(x @ w)
            gradient = x.T @ (sample_weight * (p - y)) + penalty * w
            curvature = sample_weight * p * (1 - p)
            hessian = (x * curvature[:, None]).T @ x + np.diag(penalty + 1e-9)
            step = np.linalg.solve(hessian, gradient)
            w -= step
            if np.max(np.abs(step)) < self.tol:
                break
        self.weights = w
        return self

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        if self.weights is None:
            raise RuntimeError("classifier is not fitted")
        x = np.atleast_2d(np.asarray(x, dtype=np.float64))
        return _sigmoid(self._augment(x) @ self.weights)


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -40, 40)))

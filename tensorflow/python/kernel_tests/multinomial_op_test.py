# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Tests for Multinomial."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import timeit

import numpy as np

from tensorflow.core.protobuf import config_pb2
from tensorflow.python.client import session
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import random_seed
from tensorflow.python.framework import tensor_shape
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import random_ops
from tensorflow.python.platform import test


def composed_sampler(logits, num_samples):
  # [batch size, num classes, num samples]
  unif = random_ops.random_uniform(logits.get_shape().concatenate(
      tensor_shape.TensorShape([num_samples])))
  noise = -math_ops.log(-math_ops.log(unif))
  # [batch size, num classes, 1]
  logits = array_ops.expand_dims(logits, -1)

  # [batch size, num samples]
  return math_ops.argmax(logits + noise, dimension=1)


native_sampler = random_ops.multinomial


class MultinomialTest(test.TestCase):
  use_gpu = False

  # check that events with tiny probabilities are not over-sampled
  def testLargeDynamicRange(self):
    random_seed.set_random_seed(10)
    counts_by_indices = {}
    with self.test_session(use_gpu=self.use_gpu) as sess:
      samples = random_ops.multinomial(
          constant_op.constant([[-30, 0]], dtype=dtypes.float32),
          num_samples=1000000,
          seed=15)
      for _ in range(100):
        x = sess.run(samples)
        indices, counts = np.unique(x, return_counts=True)
        for index, count in zip(indices, counts):
          if index in counts_by_indices.keys():
            counts_by_indices[index] += count
          else:
            counts_by_indices[index] = count
    # The CPU sadly is not expected to pass this test (it oversamples)
    # Would require changing the cpu algorithm back to using the gumbel trick
    # But that will slow the CPU sampling down significantly unless the speed
    # of the CPU RNG is significantly improved
    self.assertEqual(counts_by_indices[1], 100000000)

  def testLargeDynamicRange2(self):
    random_seed.set_random_seed(10)
    counts_by_indices = {}
    with self.test_session(use_gpu=self.use_gpu) as sess:
      samples = random_ops.multinomial(
          constant_op.constant([[0, -30]], dtype=dtypes.float32),
          num_samples=1000000,
          seed=15)
      for _ in range(100):
        x = sess.run(samples)
        indices, counts = np.unique(x, return_counts=True)
        for index, count in zip(indices, counts):
          if index in counts_by_indices.keys():
            counts_by_indices[index] += count
          else:
            counts_by_indices[index] = count
    self.assertEqual(counts_by_indices[0], 100000000)

  def testLargeDynamicRange3(self):
    random_seed.set_random_seed(10)
    counts_by_indices = {}
    # here the cpu undersamples and won't pass this test either
    with self.test_session(use_gpu=self.use_gpu) as sess:
      samples = random_ops.multinomial(
          constant_op.constant([[0, -17]], dtype=dtypes.float32),
          num_samples=1000000,
          seed=22)

      # we'll run out of memory if we try to draw 1e9 samples directly
      # really should fit in 12GB of memory...
      for _ in range(1000):
        x = sess.run(samples)
        indices, counts = np.unique(x, return_counts=True)
        for index, count in zip(indices, counts):
          if index in counts_by_indices.keys():
            counts_by_indices[index] += count
          else:
            counts_by_indices[index] = count
    self.assertGreater(counts_by_indices[1], 0)

  def testSmallEntropy(self):
    random_seed.set_random_seed(1618)
    with self.test_session(use_gpu=self.use_gpu):
      # A logit value of -10 corresponds to a probability of ~5e-5.
      logits = constant_op.constant([[-10., 10., -10.], [-10., -10., 10.]])
      num_samples = 1000
      samples = random_ops.multinomial(logits, num_samples).eval()
      self.assertAllEqual([[1] * num_samples, [2] * num_samples], samples)

  def testOneOpMultipleStepsIndependent(self):
    with self.test_session(use_gpu=self.use_gpu) as sess:
      sample_op1, _ = self._make_ops(10)
      # Consecutive runs shouldn't yield identical output.
      sample1a = sess.run(sample_op1)
      sample1b = sess.run(sample_op1)
      self.assertFalse(np.equal(sample1a, sample1b).all())

  def testTwoOpsIndependent(self):
    with self.test_session(use_gpu=self.use_gpu) as sess:
      sample_op1, sample_op2 = self._make_ops(32)
      sample1, sample2 = sess.run([sample_op1, sample_op2])
      # We expect sample1 and sample2 to be independent.
      # 1 in 2^32 chance of this assertion failing.
      self.assertFalse(np.equal(sample1, sample2).all())

  def testTwoOpsSameSeedDrawSameSequences(self):
    with self.test_session(use_gpu=self.use_gpu) as sess:
      sample_op1, sample_op2 = self._make_ops(1000, seed=1)
      sample1, sample2 = sess.run([sample_op1, sample_op2])
      self.assertAllEqual(sample1, sample2)

  def testLargeLogits(self):
    for neg in [True, False]:
      with self.test_session(use_gpu=self.use_gpu):
        logits = np.array([[1000.] * 5])
        if neg:
          logits *= -1
        samples = random_ops.multinomial(logits, 10).eval()
      # Sampled classes should be in-range.
      self.assertTrue((samples >= 0).all())
      self.assertTrue((samples < 5).all())

  def testSamplingCorrectness(self):
    np.random.seed(1618)  # Make it reproducible.
    num_samples = 21000

    rand_probs = self._normalize(np.random.random_sample((10,)))
    rand_probs2 = self._normalize(np.random.random_sample((3, 5)))  # batched
    for probs in [[.5, .5], [.85, .05, .1], rand_probs, rand_probs2]:
      probs = np.asarray(probs)
      if len(probs.shape) == 1:
        probs = probs.reshape(1, probs.size)  # singleton batch

      logits = np.log(probs).astype(np.float32)
      composed_freqs = self._do_sampling(logits, num_samples, composed_sampler)
      native_freqs = self._do_sampling(logits, num_samples, native_sampler)

      # the test here is similar to core/lib/random/distribution_sampler_test.cc
      composed_chi2 = self._chi2(probs, composed_freqs)
      native_chi2 = self._chi2(probs, native_freqs)
      composed_native_chi2 = self._chi2(composed_freqs, native_freqs)

      def check(chi2s):
        for chi2 in chi2s:
          self.assertLess(chi2, 1e-3)

      check(composed_chi2)
      check(native_chi2)
      check(composed_native_chi2)

  def _make_ops(self, num_samples, seed=None):
    prob_dist = constant_op.constant([[0.15, 0.5, 0.3, 0.05]])
    logits = math_ops.log(prob_dist)
    # Two independent sets of samples from the same distribution
    sample_op1 = random_ops.multinomial(logits, num_samples, seed)
    sample_op2 = random_ops.multinomial(logits, num_samples, seed)
    return (sample_op1, sample_op2)

  def _normalize(self, vec):
    batched = (len(vec.shape) == 2)
    return vec / vec.sum(axis=1, keepdims=True) if batched else vec / vec.sum()

  def _do_sampling(self, logits, num_samples, sampler):
    """Samples using the supplied sampler and inputs.

    Args:
      logits: Numpy ndarray of shape [batch_size, num_classes].
      num_samples: Int; number of samples to draw.
      sampler: A sampler function that takes (1) a [batch_size, num_classes]
        Tensor, (2) num_samples and returns a [batch_size, num_samples] Tensor.

    Returns:
      Frequencies from sampled classes; shape [batch_size, num_classes].
    """
    with self.test_session(use_gpu=self.use_gpu) as sess:
      random_seed.set_random_seed(1618)
      op = sampler(constant_op.constant(logits), num_samples)
      d = sess.run(op)

    batch_size, num_classes = logits.shape
    freqs_mat = []
    for i in range(batch_size):
      cnts = dict(collections.Counter(d[i, :]))

      # Requires drawn class labels be in range.
      self.assertLess(max(cnts.keys()), num_classes)
      self.assertGreaterEqual(min(cnts.keys()), 0)

      freqs = [(cnts[k] * 1. / num_samples if k in cnts else 0)
               for k in range(num_classes)]
      freqs_mat.append(freqs)

    return freqs_mat

  def _chi2(self, expected, actual):
    actual = np.asarray(actual)
    expected = np.asarray(expected)
    diff = actual - expected
    chi2 = np.sum(diff * diff / expected, axis=0)
    return chi2

  def testEmpty(self):
    classes = 5
    with self.test_session(use_gpu=self.use_gpu):
      for batch in 0, 3:
        for samples in 0, 7:
          x = random_ops.multinomial(
              array_ops.zeros([batch, classes]), samples).eval()
          self.assertEqual(x.shape, (batch, samples))

  def testEmptyClasses(self):
    with self.test_session(use_gpu=self.use_gpu):
      x = random_ops.multinomial(array_ops.zeros([5, 0]), 7)
      with self.assertRaisesOpError("num_classes should be positive"):
        x.eval()

  def testNegativeMinLogits(self):
    random_seed.set_random_seed(78844)
    with self.test_session(use_gpu=self.use_gpu):
      logits = constant_op.constant([[np.finfo(np.float32).min] * 1023 + [0]])
      num_samples = 1000
      samples = random_ops.multinomial(logits, num_samples).eval()
      self.assertAllEqual([[1023] * num_samples], samples)


class MultinomialGpuTest(MultinomialTest):
  use_gpu = True


# Benchmarking code
def native_op_vs_composed_ops(batch_size, num_classes, num_samples, num_iters):
  np.random.seed(1618)  # Make it reproducible.
  shape = [batch_size, num_classes]
  logits_np = np.random.randn(*shape).astype(np.float32)

  # No CSE/CF.
  optimizer_options = config_pb2.OptimizerOptions(
      opt_level=config_pb2.OptimizerOptions.L0)
  config = config_pb2.ConfigProto(graph_options=config_pb2.GraphOptions(
      optimizer_options=optimizer_options))

  with session.Session(config=config) as sess:
    logits = constant_op.constant(logits_np, shape=shape)
    native_op = control_flow_ops.group(native_sampler(logits, num_samples))
    composed_op = control_flow_ops.group(composed_sampler(logits, num_samples))

    native_dt = timeit.timeit(lambda: sess.run(native_op), number=num_iters)
    composed_dt = timeit.timeit(lambda: sess.run(composed_op), number=num_iters)
    return native_dt, composed_dt


class MultinomialBenchmark(test.Benchmark):

  def benchmarkNativeOpVsComposedOps(self):
    num_iters = 50
    print("Composition of existing ops vs. Native Multinomial op [%d iters]" %
          num_iters)
    print("BatchSize\tNumClasses\tNumSamples\tsec(composed)\tsec(native)\t"
          "speedup")

    for batch_size in [32, 128]:
      for num_classes in [10000, 100000]:
        for num_samples in [1, 4, 32]:
          n_dt, c_dt = native_op_vs_composed_ops(batch_size, num_classes,
                                                 num_samples, num_iters)
          print("%d\t%d\t%d\t%.3f\t%.3f\t%.2f" % (batch_size, num_classes,
                                                  num_samples, c_dt, n_dt,
                                                  c_dt / n_dt))

          self.report_benchmark(
              name="native_batch%d_classes%d_s%d" %
              (batch_size, num_classes, num_samples),
              iters=num_iters,
              wall_time=n_dt)
          self.report_benchmark(
              name="composed_batch%d_classes%d_s%d" %
              (batch_size, num_classes, num_samples),
              iters=num_iters,
              wall_time=c_dt)


if __name__ == "__main__":
  test.main()

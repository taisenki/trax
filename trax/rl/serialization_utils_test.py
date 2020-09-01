# coding=utf-8
# Copyright 2020 The Trax Authors.
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

"""Tests for trax.rl.serialization_utils."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import itertools

from absl.testing import parameterized
import gin
import gym
from jax import numpy as jnp
import numpy as np
from tensorflow import test
from trax import fastmath as trax_math
from trax import layers as tl
from trax import models as trax_models
from trax import shapes
from trax import test_utils
from trax.data import inputs as trax_input
from trax.layers import base as layers_base
from trax.models import transformer
from trax.rl import serialization_utils
from trax.rl import space_serializer
from trax.supervised import trainer_lib


# pylint: disable=invalid-name
def TestModel(extra_dim):
  """Dummy sequence model for testing."""
  def f(inputs):
    # Cast the input to float32 - this is for simulating discrete-input models.
    inputs = inputs.astype(np.float32)
    # Add an extra dimension if requested, e.g. the logit dimension for output
    # symbols.
    if extra_dim is not None:
      return jnp.broadcast_to(inputs[:, :, None], inputs.shape + (extra_dim,))
    else:
      return inputs
  return layers_base.Fn('TestModel', f)
  # pylint: enable=invalid-name


def generate_signals(seq_len):
  while True:
    yield (
        np.random.rand(seq_len),  # the 1st time series
        np.random.rand(seq_len),  # the 2nd time series
    )


def batch_stream(stream, batch_size):
  while True:
    yield trax_math.nested_stack(list(itertools.islice(stream, batch_size)))


def signal_inputs(seq_len, batch_size):
  def stream_fn(num_devices):
    del num_devices
    for (x, y) in batch_stream(
        generate_signals(seq_len=seq_len),
        batch_size=batch_size,
    ):
      mask = np.ones_like(x).astype(np.float32)
      # (input_x, input_y, target_x, target_y, mask)
      yield (x, y, x, y, mask)

  return trax_input.Inputs(
      train_stream=stream_fn,
      eval_stream=stream_fn,
  )


class SerializationTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self._serializer = space_serializer.create(
        gym.spaces.Discrete(2), vocab_size=2
    )
    self._repr_length = 100
    self._serialization_utils_kwargs = {
        'observation_serializer': self._serializer,
        'action_serializer': self._serializer,
        'representation_length': self._repr_length,
    }
    test_utils.ensure_flag('test_tmpdir')

  def test_serialized_model_discrete(self):
    vocab_size = 3
    obs = np.array([[[0, 1], [1, 1], [1, 0], [0, 0]]])
    act = np.array([[1, 0, 0]])
    mask = np.array([[1, 1, 1, 0]])

    test_model_inputs = []

    # pylint: disable=invalid-name
    def TestModelSavingInputs():
      def f(inputs):
        # Save the inputs for a later check.
        test_model_inputs.append(inputs)
        # Change type to np.float32 and add the logit dimension.
        return jnp.broadcast_to(
            inputs.astype(np.float32)[:, :, None], inputs.shape + (vocab_size,)
        )
      return layers_base.Fn('TestModelSavingInputs', f)
      # pylint: enable=invalid-name

    obs_serializer = space_serializer.create(
        gym.spaces.MultiDiscrete([2, 2]), vocab_size=vocab_size
    )
    act_serializer = space_serializer.create(
        gym.spaces.Discrete(2), vocab_size=vocab_size
    )
    serialized_model = serialization_utils.SerializedModel(
        TestModelSavingInputs(),  # pylint: disable=no-value-for-parameter
        observation_serializer=obs_serializer,
        action_serializer=act_serializer,
        significance_decay=0.9,
    )

    example = (obs, act, obs, mask)
    serialized_model.init(shapes.signature(example))
    (obs_logits, obs_repr, weights) = serialized_model(example)
    # Check that the model has been called with the correct input.
    np.testing.assert_array_equal(
        # The model is called multiple times for determining shapes etc.
        # Check the last saved input - that should be the actual concrete array
        # calculated during the forward pass.
        test_model_inputs[-1],
        # Should be serialized observations and actions interleaved.
        [[0, 1, 1, 1, 1, 0, 1, 0, 0, 0, 0]],
    )
    # Check the output shape.
    self.assertEqual(obs_logits.shape, obs_repr.shape + (vocab_size,))
    # Check that obs_logits are the same as obs_repr, just broadcasted over the
    # logit dimension.
    np.testing.assert_array_equal(np.min(obs_logits, axis=-1), obs_repr)
    np.testing.assert_array_equal(np.max(obs_logits, axis=-1), obs_repr)
    # Check that the observations are correct.
    np.testing.assert_array_equal(obs_repr, obs)
    # Check weights.
    np.testing.assert_array_equal(weights, [[[1, 1], [1, 1], [1, 1], [0, 0]]])

  def test_train_model_with_serialization(self):
    # Serializer handles discretization of the data.
    srl = space_serializer.BoxSpaceSerializer(
        space=gym.spaces.Box(shape=(1,), low=0.0, high=16.0),
        vocab_size=16,
        precision=2,
    )

    def model(mode):
      return serialization_utils.SerializedHalfModel(
          trax_models.TransformerLM(
              mode=mode,
              vocab_size=16,
              d_model=16,
              d_ff=8,
              n_layers=1,
              n_heads=1),
          observation_serializer=srl,
          action_serializer=srl,
          significance_decay=0.9,
      )

    output_dir = self.create_tempdir().full_path
    state = trainer_lib.train(
        output_dir=output_dir,
        model=model,
        inputs=functools.partial(signal_inputs, seq_len=5, batch_size=64),
        steps=2)
    self.assertEqual(2, state.step)

  def test_train_model_with_adhoc_serialization(self):
    # Serializer handles discretization of the data.
    srl = space_serializer.BoxSpaceSerializer(
        space=gym.spaces.Box(shape=(1,), low=0.0, high=16.0),
        vocab_size=16,
        precision=2,
    )

    # Serialization layer - serializes the two sequences and interleaves their
    # representations.
    serialize = lambda: tl.Serial(  # pylint: disable=g-long-lambda
        # (series_a, series_b)
        tl.Parallel(
            serialization_utils.Serialize(serializer=srl),
            serialization_utils.Serialize(serializer=srl),
        ),
        serialization_utils.Interleave(),
    )

    # Double the weights to go from time series weights to symbol weights.
    # Notice that for precision=2 we add 4 instead of 2 copies of x
    # in the line below
    weights = tl.Fn('DoubleWeights', lambda x: jnp.concatenate(  # pylint: disable=g-long-lambda
        (x, x, x, x), axis=1))

    def model(mode):
      return tl.Serial(
          # (input_n_boxes, input_cpu, target_n_boxes, target_cpu, weights)
          tl.Parallel(serialize(), serialize(), weights),
          # (input_repr, target_repr, weights)
          trax_models.TransformerLM(mode=mode,
                                    vocab_size=16,
                                    d_model=16,
                                    d_ff=8,
                                    n_layers=1,
                                    n_heads=1),
      )

    output_dir = self.create_tempdir().full_path
    state = trainer_lib.train(
        output_dir=output_dir,
        model=model,
        inputs=functools.partial(signal_inputs, seq_len=5, batch_size=64),
        steps=2)
    self.assertEqual(2, state.step)

  def test_serialized_model_continuous(self):
    precision = 3
    gin.bind_parameter('BoxSpaceSerializer.precision', precision)

    vocab_size = 32
    obs = np.array([[[1.5, 2], [-0.3, 1.23], [0.84, 0.07], [0, 0]]])
    act = np.array([[0, 1, 0]])
    mask = np.array([[1, 1, 1, 0]])

    obs_serializer = space_serializer.create(
        gym.spaces.Box(shape=(2,), low=-2, high=2), vocab_size=vocab_size
    )
    act_serializer = space_serializer.create(
        gym.spaces.Discrete(2), vocab_size=vocab_size
    )
    serialized_model = serialization_utils.SerializedModel(
        TestModel(extra_dim=vocab_size),  # pylint: disable=no-value-for-parameter
        observation_serializer=obs_serializer,
        action_serializer=act_serializer,
        significance_decay=0.9,
    )

    example = (obs, act, obs, mask)
    serialized_model.init(shapes.signature(example))
    (obs_logits, obs_repr, weights) = serialized_model(example)
    self.assertEqual(obs_logits.shape, obs_repr.shape + (vocab_size,))
    self.assertEqual(
        obs_repr.shape, (1, obs.shape[1], obs.shape[2] * precision)
    )
    self.assertEqual(obs_repr.shape, weights.shape)

  def test_extract_inner_model(self):
    vocab_size = 3

    inner_model = transformer.TransformerLM(
        vocab_size=vocab_size, d_model=2, d_ff=2, n_layers=0
    )
    obs_serializer = space_serializer.create(
        gym.spaces.Discrete(2), vocab_size=vocab_size
    )
    act_serializer = space_serializer.create(
        gym.spaces.Discrete(2), vocab_size=vocab_size
    )
    serialized_model = serialization_utils.SerializedModel(
        inner_model,
        observation_serializer=obs_serializer,
        action_serializer=act_serializer,
        significance_decay=0.9,
    )

    obs_sig = shapes.ShapeDtype((1, 2))
    act_sig = shapes.ShapeDtype((1, 1))
    (weights, state) = serialized_model.init(
        input_signature=(obs_sig, act_sig, obs_sig, obs_sig),
    )
    (inner_weights, inner_state) = map(
        serialization_utils.extract_inner_model, (weights, state)
    )
    inner_model(jnp.array([[0]]), weights=inner_weights, state=inner_state)

  @parameterized.named_parameters(('raw', None), ('serialized', 32))
  def test_wrapped_policy_continuous(self, vocab_size):
    precision = 3
    n_controls = 2
    n_actions = 4
    gin.bind_parameter('BoxSpaceSerializer.precision', precision)

    obs = np.array([[[1.5, 2], [-0.3, 1.23], [0.84, 0.07], [0.01, 0.66]]])
    act = np.array([[[0, 1], [2, 0], [1, 3]]])

    wrapped_policy = serialization_utils.wrap_policy(
        TestModel(extra_dim=vocab_size),  # pylint: disable=no-value-for-parameter
        observation_space=gym.spaces.Box(shape=(2,), low=-2, high=2),
        action_space=gym.spaces.MultiDiscrete([n_actions] * n_controls),
        vocab_size=vocab_size,
    )

    example = (obs, act)
    wrapped_policy.init(shapes.signature(example))
    (act_logits, values) = wrapped_policy(example)
    self.assertEqual(act_logits.shape, obs.shape[:2] + (n_controls, n_actions))
    self.assertEqual(values.shape, obs.shape[:2])

  def test_analyzes_discrete_action_space(self):
    space = gym.spaces.Discrete(n=5)
    (n_controls, n_actions) = serialization_utils.analyze_action_space(space)
    self.assertEqual(n_controls, 1)
    self.assertEqual(n_actions, 5)

  def test_analyzes_multi_discrete_action_space_with_equal_categories(self):
    space = gym.spaces.MultiDiscrete(nvec=(3, 3))
    (n_controls, n_actions) = serialization_utils.analyze_action_space(space)
    self.assertEqual(n_controls, 2)
    self.assertEqual(n_actions, 3)

  def test_doesnt_analyze_multi_disccrete_action_space_with_inequal_categories(
      self
  ):
    space = gym.spaces.MultiDiscrete(nvec=(2, 3))
    with self.assertRaises(AssertionError):
      serialization_utils.analyze_action_space(space)

  def test_doesnt_analyze_box_action_space(self):
    space = gym.spaces.Box(shape=(2, 3), low=0, high=1)
    with self.assertRaises(AssertionError):
      serialization_utils.analyze_action_space(space)


if __name__ == '__main__':
  test.main()

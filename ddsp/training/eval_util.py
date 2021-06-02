# Copyright 2021 The DDSP Authors.
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

# Lint as: python3
"""Library of evaluation functions."""

import os
import time

from absl import logging
import ddsp
from ddsp.training import data
import gin
import tensorflow.compat.v2 as tf


# ---------------------- Evaluation --------------------------------------------
def evaluate_or_sample(data_provider,
                       model,
                       evaluator_classes,
                       mode='eval',
                       save_dir='/tmp/ddsp/training',
                       restore_dir='',
                       batch_size=32,
                       num_batches=50,
                       ckpt_delay_secs=0,
                       run_once=False,
                       run_until_step=0,
                       save_csv=False):
  """Run evaluation loop.

  Args:
    data_provider: DataProvider instance.
    model: Model instance.
    evaluator_classes: List of BaseEvaluators subclasses (not instances).
    mode: Whether to 'eval' with metrics or create 'sample' s.
    save_dir: Path to directory to save summary events.
    restore_dir: Path to directory with checkpoints, defaults to save_dir.
    batch_size: Size of each eval/sample batch.
    num_batches: How many batches to eval from dataset. -1 denotes all batches.
    ckpt_delay_secs: Time to wait when a new checkpoint was not detected.
    run_once: Only run evaluation or sampling once.
    run_until_step: Run until we see a checkpoint with a step greater or equal
      to the specified value. Ignored if <= 0.
    save_csv: Whether to save a CSV file of the raw results. Used with
      restore_dir for running a final eval run after training.

  Returns:
    If the mode is 'eval', then returns a dictionary of Tensors keyed by loss
    type. Otherwise, returns None.
  """
  # Default to restoring from the save directory.
  restore_dir = save_dir if not restore_dir else restore_dir

  # Set up the summary writer and metrics.
  summary_dir = os.path.join(save_dir, 'summaries', 'eval')
  summary_writer = tf.summary.create_file_writer(summary_dir)

  # Sample continuously and load the newest checkpoint each time
  checkpoints_iterator = tf.train.checkpoints_iterator(restore_dir,
                                                       ckpt_delay_secs)

  # Get the dataset.
  dataset = data_provider.get_batch(batch_size=batch_size,
                                    shuffle=False,
                                    repeats=-1)
  # Set number of batches
  # If num_batches >=1 set it to a huge value
  num_batches = num_batches if num_batches >= 1 else int(1e12)

  # Get audio sample rate
  sample_rate = data_provider.sample_rate
  # Get feature frame rate
  frame_rate = data_provider.frame_rate

  latest_losses = None

  # Initialize evaluators.
  evaluators = [
      evaluator_class(sample_rate, frame_rate)
      for evaluator_class in evaluator_classes
  ]

  with summary_writer.as_default():
    for checkpoint_path in checkpoints_iterator:
      step = int(checkpoint_path.split('-')[-1])

      # Redefine thte dataset iterator each time to make deterministic.
      dataset_iter = iter(dataset)

      # Load model.
      try:
        model.restore(checkpoint_path)
      except FileNotFoundError:
        logging.warn('No existing checkpoint found in %s, skipping '
                     'checkpoint loading.', restore_dir)

      # Iterate through dataset and make predictions
      checkpoint_start_time = time.time()

      for batch_idx in range(1, num_batches + 1):
        try:
          logging.info('Predicting batch %d of size %d', batch_idx, batch_size)
          start_time = time.time()
          batch = next(dataset_iter)

          if isinstance(data_provider, data.SyntheticNotes):
            batch['audio'] = model.generate_synthetic_audio(batch)
            batch['f0_confidence'] = tf.ones_like(batch['f0_hz'])[:, :, 0]
            batch['loudness_db'] = ddsp.spectral_ops.compute_loudness(
                batch['audio'])

          # TODO(jesseengel): Find a way to add losses with training=False.
          outputs, losses = model(batch, return_losses=True, training=False)
          outputs['audio_gen'] = model.get_audio_from_outputs(outputs)
          for evaluator in evaluators:
            if mode == 'eval':
              evaluator.evaluate(batch, outputs, losses)
            if mode == 'sample':
              evaluator.sample(batch, outputs, step)
          logging.info('Metrics for batch %i with size %i took %.1f seconds',
                       batch_idx, batch_size, time.time() - start_time)

        except tf.errors.OutOfRangeError:
          logging.info('End of dataset.')
          break

      logging.info('All %d batches in checkpoint took %.1f seconds',
                   num_batches, time.time() - checkpoint_start_time)

      if mode == 'eval':
        for evaluator in evaluators:

          if save_csv:
            try:
              df = evaluator.as_dataframe()
              csv_dir = os.path.join(restore_dir, 'results')
              tf.io.gfile.makedirs(csv_dir)
              csv_path = os.path.join(csv_dir, evaluator.csv_filename)
              with tf.io.gfile.GFile(csv_path, 'w') as f:
                df.to_csv(f)
            except NotImplementedError:
              continue

          evaluator.flush(step)

      summary_writer.flush()

      if run_once:
        break

      if 0 < run_until_step <= step:
        logging.info(
            'Saw checkpoint with step %d, which is greater or equal to'
            ' `run_until_step` of %d. Exiting.', step, run_until_step)
        break
  return latest_losses


@gin.configurable
def evaluate(data_provider,
             model,
             evaluator_classes,
             save_dir='/tmp/ddsp/training',
             restore_dir='',
             batch_size=32,
             num_batches=50,
             ckpt_delay_secs=0,
             run_once=False,
             run_until_step=0,
             save_csv=False):
  """Run evaluation loop.

  Args:
    data_provider: DataProvider instance.
    model: Model instance.
    evaluator_classes: List of BaseEvaluators subclasses (not instances).
    save_dir: Path to directory to save summary events.
    restore_dir: Path to directory with checkpoints, defaults to save_dir.
    batch_size: Size of each eval/sample batch.
    num_batches: How many batches to eval from dataset. -1 denotes all batches.
    ckpt_delay_secs: Time to wait when a new checkpoint was not detected.
    run_once: Only run evaluation or sampling once.
    run_until_step: Run until we see a checkpoint with a step greater or equal
      to the specified value. Ignored if <= 0.
    save_csv: Whether to save a CSV file of the raw results. Used with
      restore_dir for running a final eval run after training.

  Returns:
    A dictionary of tensors containing the loss values, keyed by loss type.

  """
  return evaluate_or_sample(
      data_provider=data_provider,
      model=model,
      evaluator_classes=evaluator_classes,
      mode='eval',
      save_dir=save_dir,
      restore_dir=restore_dir,
      batch_size=batch_size,
      num_batches=num_batches,
      ckpt_delay_secs=ckpt_delay_secs,
      run_once=run_once,
      run_until_step=run_until_step,
      save_csv=save_csv)


@gin.configurable
def sample(data_provider,
           model,
           evaluator_classes,
           save_dir='/tmp/ddsp/training',
           restore_dir='',
           batch_size=16,
           num_batches=1,
           ckpt_delay_secs=0,
           run_once=False,
           run_until_step=0):
  """Run sampling loop.

  Args:
    data_provider: DataProvider instance.
    model: Model instance.
    evaluator_classes: List of BaseEvaluators subclasses (not instances).
    save_dir: Path to directory to save summary events.
    restore_dir: Path to directory with checkpoints, defaults to save_dir.
    batch_size: Size of each eval/sample batch.
    num_batches: How many batches to eval from dataset. -1 denotes all batches.
    ckpt_delay_secs: Time to wait when a new checkpoint was not detected.
    run_once: Only run evaluation or sampling once.
    run_until_step: Run until we see a checkpoint with a step greater or equal
      to the specified value. Ignored if <= 0.
  """
  evaluate_or_sample(
      data_provider=data_provider,
      model=model,
      evaluator_classes=evaluator_classes,
      mode='sample',
      save_dir=save_dir,
      restore_dir=restore_dir,
      batch_size=batch_size,
      num_batches=num_batches,
      ckpt_delay_secs=ckpt_delay_secs,
      run_once=run_once,
      run_until_step=run_until_step)



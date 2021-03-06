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

"""ResNet Train/Eval module.
"""
import time
import six
import sys

import cifar_input
import numpy as np
import rres_model
import tensorflow as tf

FLAGS = tf.app.flags.FLAGS
tf.app.flags.DEFINE_string('dataset', 'cifar10',
                           'cifar10 or cifar100.')
tf.app.flags.DEFINE_string('mode', 'train',
                           'train or eval.')
tf.app.flags.DEFINE_string('data_path', '',
                           'Filepattern for training data.')
tf.app.flags.DEFINE_integer('image_size', 32,
                            'Image side length.')
tf.app.flags.DEFINE_integer('eval_batch_count', 50,
                            'Number of batches to eval.')
tf.app.flags.DEFINE_bool('eval_once', False,
                         'Whether evaluate the model only once.')
tf.app.flags.DEFINE_string('log_root', '',
                           'Directory to keep the checkpoints.')
tf.app.flags.DEFINE_integer('num_gpus', 1,
                            'Number of gpus used for training. (0 or 1)')


def train(hps):
    """Training loop"""

    # Build input data with batches
    images, labels = cifar_input.build_input(FLAGS.dataset, FLAGS.data_path,
                                             hps.batch_size, FLAGS.mode)

    # Load a model and assign it to the default graph
    model = rres_model.RResNet(hps, images, labels, FLAGS.mode)
    model.build_graph()

    # Print out basic analysis for the model
    param_stats = tf.contrib.tfprof.model_analyzer.print_model_analysis(
        tf.get_default_graph(),
        tfprof_options=tf.contrib.tfprof.model_analyzer.TRAINABLE_VARS_PARAMS_STAT_OPTIONS)
    sys.stdout.write('Total # of Parameters: %d\n' % param_stats.total_parameters)

    tf.contrib.tfprof.model_analyzer.print_model_analysis(
        tf.get_default_graph(),
        tfprof_options=tf.contrib.tfprof.model_analyzer.FLOAT_OPS_OPTIONS)

    # Ops for calculating precision from the current model state
    truth = tf.argmax(model.labels, axis=1)
    predictions = tf.argmax(model.predictions, axis=1)
    precision = tf.reduce_mean(tf.to_float(tf.equal(predictions, truth)))

    # Define some hooks
    summary_hook = tf.train.SummarySaverHook(
        save_steps=100,
        output_dir=FLAGS.log_root + '/train',
        summary_op=tf.summary.merge([model.summaries,
                                     tf.summary.scalar('precision', precision)])
    )
    logging_hook = tf.train.LoggingTensorHook(
        tensors={'step': model.global_step,
                 'loss': model.cost,
                 'precision': precision},
        every_n_iter=100
    )

    class _LearningRateSetterHook(tf.train.SessionRunHook):
        """Sets learning_rate based on global step."""

        def begin(self):
            self._lrn_rate = hps.lrn_rate

        def before_run(self, run_context):
            return tf.train.SessionRunArgs(
                model.global_step,  # Asks for global step value.
                feed_dict={model.lrn_rate: self._lrn_rate})  # Sets learning rate

        def after_run(self, run_context, run_values):
            train_step = run_values.results
            if train_step < 40000:
                self._lrn_rate = hps.lrn_rate
            elif train_step < 60000:
                self._lrn_rate = hps.lrn_rate / 10
            elif train_step < 80000:
                self._lrn_rate = hps.lrn_rate / 100
            else:
                self._lrn_rate = hps.lrn_rate / 1000

    # Open a new session and run train_op indefinitely
    with tf.train.MonitoredTrainingSession(
            checkpoint_dir=FLAGS.log_root,
            hooks=[logging_hook, _LearningRateSetterHook()],
            chief_only_hooks=[summary_hook],
            # Since we provide a SummarySaverHook, we need to disable default
            # SummarySaverHook. To do that we set save_summaries_steps to 0.
            save_summaries_steps=0,
            config=tf.ConfigProto(allow_soft_placement=True)) as mon_sess:
        while not mon_sess.should_stop():
            mon_sess.run(model.train_op)


def evaluate(hps):
    """Evaluation loop"""

    # Build input data with batches
    images, labels = cifar_input.build_input(FLAGS.dataset, FLAGS.data_path,
                                             hps.batch_size, FLAGS.mode)

    # Load a model and assign it to the default graph
    model = rres_model.RResNet(hps, images, labels, FLAGS.mode)
    model.build_graph()

    saver = tf.train.Saver()
    summary_writer = tf.summary.FileWriter(FLAGS.log_root + "/eval")

    sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True))
    tf.train.start_queue_runners(sess)

    best_precision = 0.0
    while True:
        # Reload the evaluation score every 60 seconds
        time.sleep(60)

        # Load a checkpoint file
        try:
            ckpt_state = tf.train.get_checkpoint_state(FLAGS.log_root)
        except tf.errors.OutOfRangeError as e:
            tf.logging.error('Cannot restore checkpoint: %s', e)
            continue
        if not (ckpt_state and ckpt_state.model_checkpoint_path):
            tf.logging.info('No model to eval yet at %s', FLAGS.log_root)
            continue
        tf.logging.info('Loading checkpoint %s', ckpt_state.model_checkpoint_path)

        # Restore the saved model from the checkpoint file
        saver.restore(sess, ckpt_state.model_checkpoint_path)

        # Calculate precision value and update best_precision
        total_prediction, correct_prediction = 0, 0
        for _ in six.moves.range(FLAGS.eval_batch_count):
            summaries, loss, predictions, truth, train_step = sess.run([model.summaries, model.cost,
                                                                        model.predictions, model.labels,
                                                                        model.global_step])

            truth = np.argmax(truth, axis=1)
            predictions = np.argmax(predictions, axis=1)
            correct_prediction += np.sum(truth == predictions)
            total_prediction += predictions.shape[0]
        precision = 1.0 * correct_prediction / total_prediction
        best_precision = max(precision, best_precision)

        # Write a summary
        precision_summ = tf.Summary()
        precision_summ.value.add(tag='precision', simple_value=precision)
        summary_writer.add_summary(precision_summ, train_step)

        best_precision_summ = tf.Summary()
        best_precision_summ.value.add(tag='best_precision', simple_value=best_precision)
        summary_writer.add_summary(best_precision_summ, train_step)

        summary_writer.add_summary(summaries, train_step)

        tf.logging.info('Loss: %.3f, Precision: %.3f, Best precision: %.3f\n' %
                        (loss, precision, best_precision))
        summary_writer.flush()

        if FLAGS.eval_once:
            break


def main(_):
    if FLAGS.num_gpus == 0:
        dev = '/cpu:0'
    elif FLAGS.num_gpus == 1:
        dev = '/gpu:0'
    else:
        raise ValueError('Only support 0 or 1 gpu.')

    if FLAGS.mode == 'train':
        batch_size = 128
    elif FLAGS.mode == 'eval':
        batch_size = 100
    else:
        raise ValueError('Only support train or eval.')

    if FLAGS.dataset == 'cifar10':
        num_classes = 10
    elif FLAGS.dataset == 'cifar100':
        num_classes = 100
    else:
        raise ValueError('Only support cifar10 or cifar100.')

    hps = rres_model.HyperParameters(batch_size=batch_size,
                                     num_classes=num_classes,
                                     lrn_rate=0.1,
                                     num_residual_units=5,
                                     use_bottleneck=False,
                                     weight_decay_rate=0.0002,
                                     relu_leakiness=0.1,
                                     optimizer='mom')

    with tf.device(dev):
        if FLAGS.mode == 'train':
            train(hps)
        elif FLAGS.mode == 'eval':
            evaluate(hps)


if __name__ == '__main__':
    tf.app.run()

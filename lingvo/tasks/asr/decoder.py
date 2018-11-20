# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
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
"""Decoders for the speech model."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import math

from six.moves import range
from six.moves import zip
import tensorflow as tf

from lingvo.core import attention
from lingvo.core import base_decoder
from lingvo.core import base_layer
from lingvo.core import cluster_factory
from lingvo.core import layers
from lingvo.core import plot
from lingvo.core import py_utils
from lingvo.core import recurrent
from lingvo.core import rnn_cell
from lingvo.core import summary_utils
from lingvo.tasks.asr import contextualizer_base
from lingvo.tasks.asr import decoder_utils
from lingvo.tasks.asr import fusion


def _ToTensorArray(name, v, max_seq_length, clear_after_read=None):
  """Create TensorArray from v, of size max_seq_length."""
  ta = tf.TensorArray(
      v.dtype, max_seq_length, name=name, clear_after_read=clear_after_read)
  ta = ta.unstack(v)
  return ta


def _NewTensorArray(name, max_seq_length, dtype=None):
  """Create empty TensorArray which can store max_seq_length elements."""
  return tf.TensorArray(dtype, max_seq_length, name=name)


class AsrDecoderBase(base_decoder.BaseBeamSearchDecoder):
  """Base class for RNN-based speech decoders which operate in a ...

  'step-by-step' fashion. The model encapsulates all information which should
  persist from one step to the next in the DecoderStepState NestedMap, which
  provides a 'misc_states' NestedMap which can store arbitrary information
  required by the specific decoder sub-class.

  A 'step' in training consists of the following sequence of steps which compute
  the outputs from the decoder given the current input target (and the state
  of the model after making the previous predictions):

  1. Compute the input target at the current time step:
    cur_target_info = self.TargetsToBeFedAtCurrentDecodeStep(...)

  2. Update state and compute outputs by running SingleDecodeStep:
    step_outs, new_state = self.SingleDecodeStep(...)

  3. Update state based on the logits computed at this step:
    new_state = self.PostStepDecoderStateUpdate(old_state, logits)

  4. Display summaries based on the accumulated information across all steps.
    self.AddAdditionalDecoderSummaries(seq_out_tas)

  Sub-classes can customize behavior by implementing the following functions,
  which will modify the behavior of the decoder:

    GetTargetLabelSequencesWithBeamSearch  # Needed for EMBR training.
    _InitBeamSearchStateCallback           # Needed by beam search decoder.
    _PreBeamSearchStepCallback
    _PostBeamSearchStepCallback

    MiscZeroState: NestedMap which represents the initial state for the
      'misc_states' in the DecoderStepState. The default implementation returns
      an empty NestedMap.

    SingleDecodeStep: This corresponds to the computation which happens in each
      step of the model. The function should return the outputs of the decoder
      as well as the updated state.

    PostStepDecoderStateUpdate: A function which updates the DecoderStepState
      after the output logits from the decoder have been computed. By default,
      this returns the DecoderStepState unchanged.

    TargetsToBeFedAtCurrentDecodeStep: Returns a TargetInfo namedtuple, which
      represents information about the targets which should be input at the
      current step, as well as the output label which should be predicted.
      The default implementation uses the values in the batched 'targets'
      provided by the InputGenerator.

    AddAdditionalDecoderSummaries: A function which can be used to add any
      decoder specific information as part of the summaries displayed during
      training. By default this is a no-op.

    CreateTargetInfoMisc: A function which can be used to store arbitrary
      information as required by a sub-classes in the target info arrays used
      to determine the current label at each step during training. By default,
      this creates an empty NestedMap.

    A few other functions that control how the decoder initializes and computes
    attention during the initial step, and during each step can also be
    modified, if need be:
    _GetAttenContextDim: The dimensionality of the attention context vector.
    _CreateAtten: Controls how the attention module is configured. Most
      subclasses will not have to change this unless it changes how attention
      works.
    BaseZeroState: Returns initial state of RNNs, and attention.
    _InitAttention: Initializes Tensors used by the attention module.
    _GetInitialSeqStateTensorArrays: Get intitial tensor arrays for
      ComputePredictionsDynamic.
    _GetNewAttenProbs: Update atten probs for a timestep and return the
      updated tensor array.
  """
  # pylint: disable=invalid-name
  # DecoderStepState encapsulates everything that needs to persist from one
  # 'step' of next label prediction to the next. This interface is also used
  # while performing inference in the model, so any changes here should be
  # undertaken with care. Although the presence of the fields listed below is
  # not enforced explicitly in the code, all sub-classes should use the same
  # structure.
  #
  # rnn_states: List of NestedMaps, corresponding to states of all RNNs in the
  #   decoder.
  # atten_context, atten_probs, atten_states: See attention.py for details.
  # misc_states: NestedMap, which can contain anything the decoder needs to
  #   persist from one step to another.
  #
  # DecoderStepState = py_utils.NestedMap(
  #   rnn_states=...,
  #   atten_context=...,
  #   atten_probs=...,
  #   atten_states=...,
  #   misc_states=...,
  # )

  # TargetInfo encapsulates information about the input target sequence
  # available during training.
  # These are only used during training, so sub-classes are free to add any
  # additional target specific information in the 'misc' field, which can be
  # used to represent any model specific information.
  # misc: NestedMap, that can contain any model specific target information. By
  #   default, this is an empty NestedMap.
  TargetInfo = collections.namedtuple(
      'TargetInfo', ['id', 'label', 'weight', 'emb', 'padding', 'misc'])

  # SequenceOutTensorArrays encapsulates the various outputs generated as we
  # step through the decoder. These are used to display statistics during
  # training, and the information in these arrays can be used to modify the
  # decoder steps, e.g., by using previously predicted outputs to modify targets
  # fed at the next step in the ScheduledSampling decoder.
  # These are only used during training, so modifications to these to add
  # additional components are fine. In particular, the 'misc' field is a list of
  # TensorArrays corresponding to the 'misc_states' in the DecoderStepState
  # across all steps.
  SequenceOutTensorArrays = collections.namedtuple(
      'SequenceOutTensorArrays',
      ['rnn_outs', 'step_outs', 'atten_probs', 'logits', 'fusion', 'misc'])
  # pylint: enable=invalid-name

  @classmethod
  def Params(cls):
    p = super(AsrDecoderBase, cls).Params()
    p.Define('dropout_prob', 0.0, 'Prob at which we do dropout.')
    p.Define('emb', layers.EmbeddingLayer.Params(), 'Embedding layer params.')
    p.Define('emb_dim', 0, 'dimension of the embedding layer.')
    p.Define('label_smoothing', None, 'Label smoothing class.')
    p.Define('rnn_cell_tpl', rnn_cell.LSTMCellSimple.Params(),
             'RNNCell params template.')
    p.Define('rnn_cell_dim', 0, 'size of the rnn cells.')
    p.Define(
        'rnn_cell_hidden_dim', 0, 'internal size of the rnn cells. When '
        'set to > 0 it enables a projection layer at the output of the '
        'rnn cell (see call to SetRnnCellNodes).')
    p.Define('attention', attention.AdditiveAttention.Params(),
             'Additive attention params.')
    p.Define('softmax', layers.SimpleFullSoftmax.Params(), 'Softmax params.')
    p.Define('softmax_uses_attention', True,
             'Controls whether attention is fed to the softmax or not.')
    p.Define('source_dim', 0, 'Dimension of the source encodings.')
    p.Define('atten_context_dim', 0,
             'Depth of the attention context vector output.')
    p.Define(
        'first_rnn_input_dim', 0,
        'The input dim to the first RNN layer. If 0, it is '
        'assumed to be p.emb_dim')
    p.Define('rnn_layers', 1, 'Number of rnn layers.')
    p.Define(
        'residual_start', 0,
        'Start residual connections from this layer. For this and higher '
        'layers, the layer output is the sum of the RNN cell output and '
        'input; if the layer also normalizes its output, then the '
        'normalization is done over this sum. Set to 0 to disable '
        'residual connections.')
    p.Define('fusion', fusion.NullFusion.Params(), 'Fusion class params.')
    p.Define('parallel_iterations', 30,
             'Max number of iterations to run in parallel for while loop.')
    p.Define(
        'per_token_avg_loss', True,
        'Use per-token average loss when set to True (default); when set '
        'to False use sequence average loss (sum logP across tokens in an '
        'output sequence) and average across all sequences in the batch.')
    # Configs for scheduled sampling.
    p.Define(
        'min_ground_truth_prob', 1.0,
        'The min probability of using the ground truth as the previous '
        'prediction.')
    p.Define('min_prob_step', 1e6, 'Step to reach min_ground_truth_prob.')
    p.Define(
        'prob_decay_start_step', 1e4,
        'The step to starts linearly decrease the probability of sampling '
        'ground truth.')
    p.Define(
        'use_while_loop_based_unrolling', True,
        'Whether or not to use while loop based unrolling for training.'
        ' If false, we use a functional while based unrolling.')
    p.Define(
        'logit_types', {'logits': 1.0},
        'A dict of logit_name -> loss_weight. logit_name must be a field in '
        'the predictions NestedMap. loss_weight should add up to 1.0.')
    p.Define(
        'use_unnormalized_logits_as_log_probs', True,
        'If true, decoder beam search may return unnormalized logits as '
        'log_probs. Used for backwards-compatibility.')
    p.Define(
        'contextualizer', contextualizer_base.NullContextualizer.Params(),
        'A contextualizer that can be used'
        'to inject context into the decoder. The default NullContextualizer '
        'does not add parameters to the model nor changes the '
        'computation.')

    # Set some reasonable default values.
    # Default config for the embedding layer.
    vocab = 96
    p.emb_dim = 96
    p.emb.vocab_size = vocab
    p.emb.max_num_shards = 1
    p.emb.params_init = py_utils.WeightInit.Uniform(1.0)
    # Default config for the rnn layer.
    p.rnn_cell_dim = 256
    p.rnn_cell_tpl.params_init = py_utils.WeightInit.Uniform(0.1)
    # Default config for the attention model.
    p.attention.hidden_dim = 128
    p.attention.params_init = py_utils.WeightInit.UniformSqrtDim(math.sqrt(3.0))
    # Default config for the softmax part.
    p.softmax.num_classes = vocab
    p.softmax.params_init = py_utils.WeightInit.Uniform(0.1)
    # LM config, if used.
    p.fusion.lm.vocab_size = vocab
    # Other configs.
    p.target_seq_len = 300
    p.source_dim = 512
    return p

  @base_layer.initializer
  def __init__(self, params):
    params = params.Copy()
    if params.min_ground_truth_prob < 1:
      # Move embedding lookup onto worker.
      params.emb.on_ps = False
    super(AsrDecoderBase, self).__init__(params)

    p = self.params
    assert p.packed_input is False, ('Packed inputs are not yet supported for '
                                     'AsrDecoderBase.')

    self._max_label_prob = 1 - p.min_ground_truth_prob
    self._decay_interval = p.min_prob_step - p.prob_decay_start_step
    if self._decay_interval <= 0:
      raise ValueError('min_prob_step (%d) <= prob_decay_start_step (%d)' %
                       (p.min_prob_step, p.prob_decay_start_step))

    if p.random_seed:
      self._prng_seed = p.random_seed
    else:
      self._prng_seed = py_utils.GenerateSeedFromName(p.name)

    name = p.name
    with tf.variable_scope(name):
      self.CreateChild('contextualizer', p.contextualizer)
      atten_context_dim = self._GetAttenContextDim()
      assert atten_context_dim > 0

      p.emb.dtype = p.dtype
      p.emb.embedding_dim = p.emb_dim
      self.CreateChild('emb', p.emb)

      p.softmax.dtype = p.dtype
      if p.softmax_uses_attention:
        p.softmax.input_dim = p.rnn_cell_dim + atten_context_dim
      else:
        p.softmax.input_dim = p.rnn_cell_dim
      self.CreateChild('softmax', p.softmax)

      p.fusion.base_model_logits_dim = p.softmax.input_dim
      self.CreateChild('fusion', p.fusion)

      first_rnn_input_dim = p.first_rnn_input_dim
      if not first_rnn_input_dim:
        first_rnn_input_dim = self.fusion.FusedEmbDim(p.emb_dim)

      params_rnn_cells = []
      for i in range(p.rnn_layers):
        rnn_cell_params = p.rnn_cell_tpl.Copy()
        rnn_cell_params.dtype = p.dtype
        rnn_cell_params.inputs_arity = 2
        decoder_utils.SetRnnCellNodes(p, rnn_cell_params)
        if i == 0:
          rnn_cell_params.name = 'rnn_cell'
          rnn_cell_params.num_input_nodes = (
              first_rnn_input_dim + atten_context_dim)
        else:
          rnn_cell_params.name = 'rnn_cell_%d' % i
          rnn_cell_params.num_input_nodes = p.rnn_cell_dim + atten_context_dim
        params_rnn_cells.append(rnn_cell_params)
      self.CreateChildren('rnn_cell', params_rnn_cells)

      self._CreateAtten()

      if p.label_smoothing is not None:
        p.label_smoothing.name = 'smoother'
        if p.label_smoothing.num_classes == 0:
          p.label_smoothing.num_classes = p.softmax.num_classes
        elif p.label_smoothing.num_classes != p.softmax.num_classes:
          raise ValueError('label_smoothing.num_classes ({}) does not match '
                           'softmax.num_classes ({})'.format(
                               p.label_smoothing.num_classes,
                               p.softmax.num_classes))
        self.CreateChild('smoother', p.label_smoothing)

  def _CreateAtten(self):
    p = self.params
    p.attention.dtype = p.dtype
    p.attention.source_dim = (
        p.attention.source_dim if p.attention.source_dim else p.source_dim)
    p.attention.query_dim = (
        p.attention.query_dim if p.attention.query_dim else p.rnn_cell_dim)
    self.CreateChild('atten', p.attention)

  def _GetAttenContextDim(self):
    p = self.params
    audio_context_dim = (
        p.atten_context_dim if p.atten_context_dim else p.source_dim)
    additional_context_dim = self.contextualizer.GetContextDim()
    return audio_context_dim + additional_context_dim

  def _ApplyDropout(self,
                    x_in,
                    deterministic=False,
                    extra_seed=None,
                    step_state=None):
    p = self.params
    assert 0 <= p.dropout_prob and p.dropout_prob < 1.0
    if p.is_eval or p.dropout_prob == 0.0:
      return x_in

    seed = self._prng_seed
    if extra_seed:
      seed += extra_seed
    if deterministic:
      assert isinstance(step_state, py_utils.NestedMap)
      assert 'global_step' in step_state, step_state.DebugString()
      assert 'time_step' in step_state, step_state.DebugString()
      seeds = seed + tf.stack([step_state.global_step, step_state.time_step])
      return py_utils.DeterministicDropout(x_in, 1.0 - p.dropout_prob, seeds)
    else:
      if not p.random_seed:
        seed = None
      return tf.nn.dropout(x_in, 1.0 - p.dropout_prob, seed=seed)

  def _InitAttention(self, theta, source_encs, source_paddings):
    """Intializes attention and returns a NestedMap with those values."""
    packed_src = self.atten.InitForSourcePacked(theta.atten, source_encs,
                                                source_encs, source_paddings)
    self.contextualizer.InitAttention(theta.contextualizer, packed_src)
    return packed_src

  def BaseZeroState(self,
                    theta,
                    source_encs,
                    source_paddings,
                    bs,
                    misc_zero_states,
                    per_step_source_padding=None):
    """Returns initial state of RNNs, and attention."""
    p = self.params
    step_state = misc_zero_states.step_state
    rnn_states = []
    for i in range(p.rnn_layers):
      rnn_states.append(self.rnn_cell[i].zero_state(bs))

    packed_src = self._InitAttention(theta, source_encs, source_paddings)
    zero_atten_state = self.atten.ZeroAttentionState(
        tf.shape(source_encs)[0], bs)
    (atten_context, atten_probs, atten_states) = (
        self.atten.ComputeContextVectorWithSource(
            theta.atten,
            packed_src,
            tf.zeros([bs, p.rnn_cell_dim], dtype=py_utils.FPropDtype(p)),
            zero_atten_state,
            per_step_source_padding=per_step_source_padding,
            step_state=step_state))
    atten_context = self.contextualizer.ZeroAttention(
        theta.contextualizer, bs, misc_zero_states, atten_context, packed_src)

    return rnn_states, atten_context, atten_probs, atten_states, packed_src

  def AddAdditionalDecoderSummaries(self, source_encs, source_paddings, targets,
                                    seq_out_tas, softmax_input):
    """Additional model-specific summaries which should be displayed."""
    pass

  def DecoderStepZeroState(self, theta, source_encs, source_paddings,
                           target_ids, bs):
    misc_zero_states = self.MiscZeroState(source_encs, source_paddings,
                                          target_ids, bs)
    rnn_states, atten_context, atten_probs, atten_states, packed_src = (
        self.BaseZeroState(theta, source_encs, source_paddings, bs,
                           misc_zero_states))
    return py_utils.NestedMap(
        rnn_states=rnn_states,
        atten_context=atten_context,
        atten_probs=atten_probs,
        atten_states=atten_states,
        fusion_states=self.fusion.zero_state(bs),
        misc_states=misc_zero_states), packed_src

  def _AddDecoderActivationsSummary(self,
                                    source_encs,
                                    source_paddings,
                                    targets,
                                    atten_probs,
                                    rnn_outs,
                                    softmax_input,
                                    additional_atten_probs=None,
                                    target_alignments=None):
    """Adds summary about decoder activations.

    For each of the args, a TensorArray can also be a Tensor representing
    the stacked array.

    Args:
      source_encs: a Tensor of shape [max_source_length, batch, source_dims].
      source_paddings: a Tensor of shape [max_source_length, batch].
      targets: a NestedMap, usually input_batch.tgt.
      atten_probs: a TensorArray of max_target_length elements, each of shape
        [batch, max_source_length].
      rnn_outs: a list of TensorArray, one for each RNN layer. Each
        TensorArray has max_target_length elements, each of shape [batch,
        rnn_output_dim].
      softmax_input: a Tensor of shape [batch, max_target_length, vocab_size].
      additional_atten_probs: an optional list of (name, TensorArray) to display
        along with atten_probs.
      target_alignments: an optional Tensor of shape [batch, max_target_length]
        where every value is an int32 in the range of [1, max_source_length],
        representing number of source frames by which a target label should be
        emitted.

    Returns:
      A finalized figure.
    """
    if not self.cluster.add_summary:
      return tf.summary.scalar('disabled_decoder_example', 0)

    def _ToTensor(t):
      return t.stack() if isinstance(t, tf.TensorArray) else t

    atten_probs = _ToTensor(atten_probs)
    rnn_outs = [_ToTensor(ta) for ta in rnn_outs]
    if additional_atten_probs:
      additional_atten_probs = [
          (name, _ToTensor(ta)) for name, ta in additional_atten_probs
      ]

    num_cols = 2 + len(rnn_outs)
    fig = plot.MatplotlibFigureSummary(
        'decoder_example',
        figsize=(2.3 * (3 + num_cols - 1), 6),
        max_outputs=1,
        subplot_grid_shape=(2, num_cols),
        gridspec_kwargs=dict(
            width_ratios=[3] + [1] * (num_cols - 1), height_ratios=(4, 1)))

    # Attention needs a custom plot_func to allow for clean y-axis label for
    # very long transcripts
    def PlotAttention(fig, axes, transcript, atten_probs, title):
      plot.AddImage(fig, axes, atten_probs, title=title)
      axes.set_ylabel(
          plot.ToUnicode(transcript + '\nOutput token'),
          size='x-small',
          wrap=True)

    index = 0
    if 'transcripts' not in targets:
      return tf.summary.scalar('disabled_decoder_example', 0)
    transcript = targets.transcripts[:index + 1]

    srclen = tf.cast(tf.reduce_sum(1 - source_paddings[:, index]), tf.int32)
    tgtlen = tf.cast(tf.reduce_sum(1 - targets.paddings[index, :]), tf.int32)

    def PlotAttentionForOneExample(atten_probs,
                                   target_fig,
                                   title,
                                   alignments=None):
      """Plots attention for one example."""
      tf.logging.info('Plotting attention for %s: %s %s', title,
                      atten_probs.shape, alignments)
      atten_probs = atten_probs[:tgtlen, index, :srclen]
      if alignments is not None:
        # [tgtlen].
        alignment_positions = alignments[index, :tgtlen] - 1
        # [tgtlen, srclen].
        alignment_probs = tf.one_hot(alignment_positions, depth=srclen, axis=-1)

        # The summary image will use red bars to represent target label
        # alignments and purple shades for attention probabilities.
        atten_probs = 1 - tf.stack(
            [
                atten_probs,
                # Overlay atten_probs and alignment_probs on the green channel
                # so that colors are visible on a white background.
                tf.minimum(atten_probs + alignment_probs, 1.),
                alignment_probs
            ],
            axis=-1)
      probs = tf.expand_dims(atten_probs, 0)
      target_fig.AddSubplot([transcript, probs], PlotAttention, title=title)

    PlotAttentionForOneExample(atten_probs, fig, title=u'atten_probs')
    # rnn_outs and softmax_input have transposed shapes of [tgtlen, dim]
    # compared to source_encs [dim, srclen].
    for i in range(len(rnn_outs)):
      rnn_out = tf.expand_dims(rnn_outs[i][:tgtlen, index, :], 0)
      fig.AddSubplot([rnn_out], title=u'rnn_outs/%d' % i)
    fig.AddSubplot(
        [softmax_input[:index + 1, :tgtlen, :]], title=u'softmax_input')
    source_encs = tf.expand_dims(
        tf.transpose(source_encs[:srclen, index, :]), 0)
    fig.AddSubplot([source_encs], title=u'source_encs', xlabel=u'Encoder frame')
    finalized_fig = fig.Finalize()

    if additional_atten_probs:
      all_atten_probs = [('atten_probs', atten_probs)] + additional_atten_probs
      num_atten_images = len(all_atten_probs)
      atten_fig = plot.MatplotlibFigureSummary(
          'decoder_attention', figsize=(6, 3 * num_atten_images), max_outputs=1)
      for key, probs in all_atten_probs:
        PlotAttentionForOneExample(
            probs, atten_fig, title=key, alignments=target_alignments)
      atten_fig.Finalize()
    return finalized_fig

  def _ComputeMetrics(self,
                      logits,
                      target_labels,
                      target_weights,
                      target_probs=None):
    """Compute loss and misc metrics.

    Args:
      logits: Tensor of shape [batch, time, num_classes].
      target_labels: Tensor of shape [batch, time].
      target_weights: Tensor of shape [batch, time].
      target_probs: Tensor of shape [batch, time, num_classes].
    Returns:
      A (metrics, per_sequence_loss) pair.
    """
    p = self.params
    target_weights_sum = tf.reduce_sum(target_weights)
    # add 0.000001 to avoid divide-by-zero.
    target_weights_sum_eps = target_weights_sum + 0.000001
    if not py_utils.use_tpu():
      correct_preds = tf.cast(
          tf.equal(tf.argmax(logits, 2, output_type=tf.int32), target_labels),
          py_utils.FPropDtype(p))
      correct_next_preds = tf.reduce_sum(correct_preds * target_weights)
      accuracy = tf.identity(
          correct_next_preds / target_weights_sum_eps,
          name='fraction_of_correct_next_step_preds')
    # Pad zeros so that we can stack them.
    if target_probs is not None:
      per_example_loss = tf.nn.softmax_cross_entropy_with_logits(
          labels=target_probs, logits=logits)
    else:
      per_example_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(
          labels=target_labels, logits=logits)
    per_sequence_loss = tf.reduce_sum(per_example_loss * target_weights, 1)
    per_token_avg_loss = (
        tf.reduce_sum(per_sequence_loss) / target_weights_sum_eps)
    if p.per_token_avg_loss:
      loss = per_token_avg_loss
      loss_weight = target_weights_sum
    else:  # per-sequence average loss
      loss = tf.reduce_mean(per_sequence_loss)
      loss_weight = tf.shape(per_sequence_loss)[0]
    metrics = {
        'loss': (loss, loss_weight),
        # add log_pplx for compatibility with the mt/decoder.py
        'log_pplx': (per_token_avg_loss, target_weights_sum)
    }
    if not py_utils.use_tpu():
      metrics['fraction_of_correct_next_step_preds'] = (accuracy,
                                                        target_weights_sum)

    return metrics, per_sequence_loss

  def InitDecoder(self, theta, source_encs, source_paddings, dec_bs):
    decoder_step_zero_state, _ = self.DecoderStepZeroState(
        theta, source_encs, source_paddings,
        tf.ones([dec_bs, 1], dtype=tf.int32) * self.params.target_sos_id,
        dec_bs)

    return (decoder_step_zero_state.rnn_states,
            decoder_step_zero_state.atten_context,
            decoder_step_zero_state.atten_probs,
            decoder_step_zero_state.atten_states,
            decoder_step_zero_state.fusion_states,
            decoder_step_zero_state.misc_states)

  # TODO(rpang): add 'theta' as an arg to BeamSearchDecode().
  def BeamSearchDecode(self,
                       source_encs,
                       source_paddings,
                       num_hyps_per_beam_override=0):
    """Performs beam-search based decoding.

    Args:
      source_encs: source encoding, of shape [time, batch, depth].
      source_paddings: source encoding's padding, of shape [time, batch].
      num_hyps_per_beam_override: If set to a value <= 0, this parameter is
        ignored. If set to a value > 0, then this value will be used to
        override p.num_hyps_per_beam.

    Returns:
      BeamSearchDecodeOutput, a namedtuple containing the decode results
    """
    return self.beam_search.BeamSearchDecode(
        self.theta, source_encs, source_paddings, num_hyps_per_beam_override,
        self._InitBeamSearchStateCallback, self._PreBeamSearchStepCallback,
        self._PostBeamSearchStepCallback)

  def _InitBeamSearchStateCallback(self,
                                   theta,
                                   source_encs,
                                   source_paddings,
                                   num_hyps_per_beam,
                                   additional_source_info=None):
    raise NotImplementedError('_InitBeamSearchStateCallback')

  def _PreBeamSearchStepCallback(self,
                                 theta,
                                 source_encs,
                                 source_paddings,
                                 step_ids,
                                 states,
                                 num_hyps_per_beam,
                                 additional_source_info=None):
    raise NotImplementedError('_PreBeamSearchStepCallback')

  def _PostBeamSearchStepCallback(self,
                                  theta,
                                  source_encs,
                                  source_paddings,
                                  new_step_ids,
                                  states,
                                  additional_source_info=None):
    raise NotImplementedError('_PostBeamSearchStepCallback')

  def FProp(self, theta, source_encs, source_paddings, targets, src_segment_id):
    with tf.device(self.cluster.WorkerDeviceInModelSplit(0)):
      predictions = self.ComputePredictions(theta, source_encs, source_paddings,
                                            targets, src_segment_id)
      return self.ComputeLoss(theta, predictions, targets)

  def FPropWithPerExampleLoss(self,
                              source_encs,
                              source_paddings,
                              targets,
                              src_segment_id,
                              targets_per_batch_element=1):
    predictions = self.ComputePredictions(
        self.theta, source_encs, source_paddings, targets, src_segment_id,
        targets_per_batch_element)
    return self.ComputeMetricsAndPerSequenceLoss(
        self.theta, predictions, targets, targets_per_batch_element)

  def FPropWithPredictions(self, source_encs, source_paddings, targets,
                           src_segment_id):
    """Returns FProp() results together with predictions."""
    predictions = self.ComputePredictions(
        self.theta, source_encs, source_paddings, targets, src_segment_id)
    metrics, _ = self.ComputeMetricsAndPerSequenceLoss(self.theta, predictions,
                                                       targets)
    return metrics, predictions

  def ComputeLoss(self, theta, predictions, targets):
    metrics, _ = self.ComputeMetricsAndPerSequenceLoss(theta, predictions,
                                                       targets)
    return metrics

  def ComputeMetricsAndPerSequenceLoss(self,
                                       theta,
                                       predictions,
                                       targets,
                                       targets_per_batch_element=1):
    """Computes loss metrics and per-sequence losses.

    Args:
      theta: A NestedMap object containing weights' values of this
        layer and its children layers.
      predictions: A NestedMap containing logits (and possibly other fields).
      targets: A dict of string to tensors representing the targets one is
          trying to predict. Each tensor in targets is of shape [batch, time].
      targets_per_batch_element: Number of target sequences per utterance.

    Returns:
      (metrics, per_sequence_loss), where metrics is a dictionary containing
      metrics for the xent loss and prediction accuracy. per_sequence_loss is a
      (-log(p)) vector of size [bs].
    """
    del targets_per_batch_element
    p = self.params
    with tf.name_scope(p.name):
      if p.label_smoothing is not None:
        target_probs = self.smoother.FProp(theta.smoother, targets.paddings,
                                           targets.labels, targets.ids)
      else:
        target_probs = None
      merged_metrics = {}
      merged_per_sequence_loss = 0.

      def AddToMetric(acc, scale, metric):
        assert len(acc) == 2
        assert len(metric) == 2
        return (acc[0] + scale * tf.cast(metric[0], py_utils.FPropDtype(p)),
                acc[1] + scale * tf.cast(metric[1], py_utils.FPropDtype(p)))

      for logit_name, loss_weight in p.logit_types.iteritems():
        metrics, per_sequence_loss = self._ComputeMetrics(
            getattr(predictions, logit_name), targets.labels, targets.weights,
            target_probs)
        for k, v in metrics.iteritems():
          tf.logging.info('Merging metric %s: %s', k, v)
          merged_metrics[k + '/' + logit_name] = v
          if k not in merged_metrics:
            merged_metrics[k] = (tf.zeros(
                shape=[], dtype=py_utils.FPropDtype(p)),
                                 tf.zeros(
                                     shape=[], dtype=py_utils.FPropDtype(p)))
          merged_metrics[k] = AddToMetric(merged_metrics[k], loss_weight, v)
        merged_per_sequence_loss += loss_weight * per_sequence_loss
      return merged_metrics, merged_per_sequence_loss

  def CreateTargetInfoMisc(self, targets):
    """Return a NestedMap corresponding to the 'misc' field in TargetInfo."""
    if 'fst_bias_probs' in targets:
      return py_utils.NestedMap({
          'fst_bias_probs': targets.fst_bias_probs,
      })
    else:
      return py_utils.NestedMap()

  def ComputePredictions(self,
                         theta,
                         source_encs,
                         source_paddings,
                         targets,
                         src_segment_id,
                         targets_per_batch_element=1):
    """Computes logits.

    Args:
      theta: A NestedMap object containing weights values of this layer and its
        child layers.
      source_encs: a Tensor of shape [max_source_length, batch, source_dims].
      source_paddings: a Tensor of shape [max_source_length, batch].
      targets: A dict of string to tensors representing the targets one is
        trying to predict. Each tensor in targets is of shape [batch, time].
      src_segment_id: (unused).
      targets_per_batch_element: (unused).

    Returns:
      A NestedMap object containing logit tensors as values, each of shape
      [target_batch, max_target_length, vocab_size]. One of the keys must be
      'logits'.
    """
    del src_segment_id, targets_per_batch_element
    p = self.params
    self.contextualizer.SetContextMap(targets)
    if p.use_while_loop_based_unrolling:
      predictions = self.ComputePredictionsDynamic(theta, source_encs,
                                                   source_paddings, targets)
    else:
      predictions = self.ComputePredictionsFunctional(theta, source_encs,
                                                      source_paddings, targets)
    if isinstance(source_paddings, tf.Tensor):
      # source_padding is of shape [time, batch]. Compute source_enc_len, which
      # is used for computing attention loss.
      predictions.source_enc_len = tf.reduce_sum(1 - source_paddings, axis=0)
    return predictions

  def _GetInitialSeqStateTensorArrays(self, max_seq_length,
                                      decoder_step_state_zero_fusion_flat,
                                      decoder_step_state_zero_misc_flat):
    """Get intitial tensor arrays for ComputePredictionsDynamic."""
    p = self.params
    # TensorArrays for sequence outputs.
    return AsrDecoder.SequenceOutTensorArrays(
        rnn_outs=[
            _NewTensorArray(
                name='rnn%d_outs' % i,
                max_seq_length=max_seq_length,
                dtype=py_utils.FPropDtype(p)) for i in range(p.rnn_layers)
        ],
        step_outs=_NewTensorArray(
            name='step_outs',
            max_seq_length=max_seq_length,
            dtype=py_utils.FPropDtype(p)),
        atten_probs=_NewTensorArray(
            name='atten_probs',
            max_seq_length=max_seq_length,
            dtype=py_utils.FPropDtype(p)),
        logits=_NewTensorArray(
            name='logits',
            max_seq_length=max_seq_length,
            dtype=py_utils.FPropDtype(p)),
        fusion=[
            _NewTensorArray(
                name='fusion_states%d' % i,
                max_seq_length=max_seq_length,
                dtype=decoder_step_state_zero_fusion_flat[i].dtype)
            for i in range(len(decoder_step_state_zero_fusion_flat))
        ],
        misc=[
            _NewTensorArray(
                name='misc_states%d' % i,
                max_seq_length=max_seq_length,
                dtype=decoder_step_state_zero_misc_flat[i].dtype)
            for i in range(len(decoder_step_state_zero_misc_flat))
        ])

  def _GetNewAttenProbs(self, seq_out_tas, time, decoder_step_state):
    """Update atten probs for a timestep and return the updated tensor array."""
    return seq_out_tas.atten_probs.write(time, decoder_step_state.atten_probs)

  def _UpdateSequenceOutTensorArrays(self, decoder_step_state, time, step_outs,
                                     seq_out_tas):
    """Update SequenceOutTensorArrays at each time step."""
    new_rnn_outs = []
    assert len(seq_out_tas.rnn_outs) == len(decoder_step_state.rnn_states)
    for i in range(len(seq_out_tas.rnn_outs)):
      new_rnn_outs.append(seq_out_tas.rnn_outs[i].write(
          time, decoder_step_state.rnn_states[i].m))
    new_logits_ta = seq_out_tas.logits.write(time, decoder_step_state.logits)
    new_step_outs_ta = seq_out_tas.step_outs.write(time, step_outs)
    new_atten_probs_ta = self._GetNewAttenProbs(seq_out_tas, time,
                                                decoder_step_state)
    new_seq_outs_fusion_states = []
    new_fusion_states_flat = decoder_step_state.fusion_states.Flatten()
    for i in range(len(new_fusion_states_flat)):
      new_seq_outs_fusion_states.append(seq_out_tas.fusion[i].write(
          time, new_fusion_states_flat[i]))
    new_seq_outs_misc_states = []
    new_misc_states_flat = decoder_step_state.misc_states.Flatten()
    for i in range(len(new_misc_states_flat)):
      new_seq_outs_misc_states.append(seq_out_tas.misc[i].write(
          time, new_misc_states_flat[i]))

    return AsrDecoder.SequenceOutTensorArrays(
        rnn_outs=new_rnn_outs,
        step_outs=new_step_outs_ta,
        atten_probs=new_atten_probs_ta,
        logits=new_logits_ta,
        fusion=new_seq_outs_fusion_states,
        misc=new_seq_outs_misc_states)

  def _GetAttenProbsFromSequenceOutTensorArrays(self, atten_probs):
    return tf.transpose(atten_probs.stack(), [1, 0, 2])

  def _GetPredictionFromSequenceOutTensorArrays(self, seq_out_tas):
    # [max_target_length, batch, dim] -> [batch, max_target_length, dim].
    return py_utils.NestedMap(
        logits=tf.transpose(seq_out_tas.logits.stack(), [1, 0, 2]),
        attention=py_utils.NestedMap(
            probs=self._GetAttenProbsFromSequenceOutTensorArrays(seq_out_tas
                                                                 .atten_probs)))

  def _GetInitialTargetInfo(self, targets, max_seq_length, target_embs):
    return AsrDecoderBase.TargetInfo(
        id=_ToTensorArray(
            'target_ids_ta',
            tf.transpose(targets.ids),
            max_seq_length,
            clear_after_read=False),
        label=_ToTensorArray(
            'target_labels_ta',
            tf.transpose(targets.labels),
            max_seq_length,
            clear_after_read=False),
        weight=_ToTensorArray('target_weights_ta',
                              tf.transpose(targets.weights), max_seq_length),
        emb=_ToTensorArray('target_embs_ta', tf.transpose(
            target_embs, [1, 0, 2]), max_seq_length),
        padding=_ToTensorArray(
            'target_paddings_ta',
            tf.expand_dims(tf.transpose(targets.paddings), -1), max_seq_length),
        misc=self.CreateTargetInfoMisc(targets),
    )

  def ComputePredictionsDynamic(self, theta, source_encs, source_paddings,
                                targets):
    p = self.params
    with tf.name_scope(p.name):
      # Create TensorArrays corresponding to the targets to be used for
      # training.
      dec_bs = tf.shape(targets.ids)[0]
      max_seq_length = tf.shape(targets.ids)[1]

      target_embs = self.emb.EmbLookup(theta.emb, tf.reshape(targets.ids, [-1]))
      target_embs = tf.reshape(target_embs, [dec_bs, max_seq_length, p.emb_dim])
      target_embs = self._ApplyDropout(target_embs)
      target_info_tas = self._GetInitialTargetInfo(targets, max_seq_length,
                                                   target_embs)

      # Initialize all loop variables.
      time = tf.constant(0, tf.int32)
      # Decoder state.
      decoder_step_state_zero, packed_src = self.DecoderStepZeroState(
          theta, source_encs, source_paddings, targets.ids, dec_bs)
      decoder_step_state_zero_fusion_flat = (
          decoder_step_state_zero.fusion_states.Flatten())
      decoder_step_state_zero_misc_flat = (
          decoder_step_state_zero.misc_states.Flatten())

      # TensorArrays for sequence outputs.
      seq_out_tas = self._GetInitialSeqStateTensorArrays(
          max_seq_length, decoder_step_state_zero_fusion_flat,
          decoder_step_state_zero_misc_flat)

      def _LoopContinue(time, decoder_step_state, target_info_tas, seq_out_tas):
        del decoder_step_state, target_info_tas, seq_out_tas
        return time < max_seq_length

      def _LoopBody(time, old_decoder_step_state, target_info_tas, seq_out_tas):
        """Computes decoder outputs and updates decoder_step_state."""
        cur_target_info = self.TargetsToBeFedAtCurrentDecodeStep(
            time, theta, old_decoder_step_state, target_info_tas, seq_out_tas)

        lm_output, fusion_states = self.fusion.FPropLm(
            theta.fusion, old_decoder_step_state.fusion_states,
            cur_target_info.id, cur_target_info.padding)

        fused_emb, fusion_states = self.fusion.FuseEmb(
            theta.fusion, fusion_states, lm_output, cur_target_info.emb)
        cur_target_info = self.OverrideEmbedding(cur_target_info, fused_emb)

        step_outs, decoder_step_state = self.SingleDecodeStep(
            theta, packed_src, cur_target_info, old_decoder_step_state)

        step_outs, decoder_step_state.fusion_states = self.fusion.FuseOutput(
            theta.fusion, fusion_states, lm_output, step_outs)

        # Compute logits.
        xent_loss = self.softmax.FProp(
            theta.softmax, [step_outs],
            class_weights=cur_target_info.weight,
            class_ids=cur_target_info.label)

        decoder_step_state = self.PostStepDecoderStateUpdate(
            decoder_step_state, xent_loss.logits)

        decoder_step_state.logits = self.fusion.ComputeLogitsWithLM(
            decoder_step_state.fusion_states, decoder_step_state.logits)

        # Update SequenceOutTensorArrays.
        new_seq_out_tas = self._UpdateSequenceOutTensorArrays(
            decoder_step_state, time, step_outs, seq_out_tas)
        del decoder_step_state.logits
        return (time + 1, decoder_step_state, target_info_tas, new_seq_out_tas)

      loop_vars = time, decoder_step_state_zero, target_info_tas, seq_out_tas
      # NOTE(skyewm): this could be more specific, but for now don't verify
      # while_loop input/output shapes at all.
      shape_invariants = tf.contrib.framework.nest.map_structure(
          lambda t: tf.TensorShape(None), loop_vars)

      (time, _, target_info_tas, seq_out_tas) = tf.while_loop(
          _LoopContinue,
          _LoopBody,
          loop_vars=loop_vars,
          shape_invariants=shape_invariants,
          parallel_iterations=p.parallel_iterations,
          swap_memory=False)

      softmax_input = seq_out_tas.step_outs.stack()
      softmax_input = tf.transpose(softmax_input, [1, 0, 2])
      self._AddDecoderActivationsSummary(source_encs, source_paddings, targets,
                                         seq_out_tas.atten_probs,
                                         seq_out_tas.rnn_outs, softmax_input)
      self.AddAdditionalDecoderSummaries(source_encs, source_paddings, targets,
                                         seq_out_tas, softmax_input)
      return self._GetPredictionFromSequenceOutTensorArrays(seq_out_tas)

  def ComputePredictionsFunctional(self, theta, source_encs, source_paddings,
                                   targets):
    p = self.params
    # Currently, scheduled sampling is not supported.
    assert p.min_ground_truth_prob == 1.0
    with tf.name_scope(p.name):
      dec_bs = tf.shape(targets.ids)[0]

      # Decoder state.
      state0, packed_src = self.DecoderStepZeroState(
          theta, source_encs, source_paddings, targets.ids, dec_bs)

      atten_context_dim = self._GetAttenContextDim()
      out_dim = p.rnn_cell_dim + atten_context_dim
      state0.step_outs = tf.zeros([dec_bs, out_dim],
                                  dtype=py_utils.FPropDtype(p))
      target_embs = self.emb.EmbLookup(theta.emb, targets.ids)
      target_embs = self._ApplyDropout(target_embs)
      inputs = py_utils.NestedMap(
          id=tf.transpose(targets.ids),
          label=tf.transpose(targets.labels),
          weight=tf.transpose(targets.weights),
          emb=tf.transpose(target_embs, [1, 0, 2]),
          padding=tf.expand_dims(tf.transpose(targets.paddings), -1),
          misc=self.CreateTargetInfoMisc(targets),
      )

      lm_output, state0.fusion_states = self.fusion.FPropLm(
          theta.fusion, state0.fusion_states, inputs.id, inputs.padding,
          inputs.misc)

      inputs.emb, state0.fusion_states = self.fusion.FuseEmb(
          theta.fusion, state0.fusion_states, lm_output, inputs.emb)

      # If the theta in the recurrent loop contains fusion related variables,
      # it will allocate a large amount of memory even though it is not being
      # used and exceed current TPU HBM limit. Thus remove fusion theta from
      # the recurrent loop, and performs fusion outside the recurrent loop.
      theta_no_fusion = theta.copy()
      del theta_no_fusion.fusion
      recurrent_theta = py_utils.NestedMap(
          theta=theta_no_fusion,
          packed_src=packed_src,
          source_paddings=source_paddings)
      state0_no_fusion = state0.copy()
      del state0_no_fusion.fusion_states

      def RnnStep(recurrent_theta, state0, inputs):
        """Computes one rnn step."""
        with tf.name_scope('single_decode_step'):
          step_outs, state1 = self.SingleDecodeStep(
              recurrent_theta.theta,
              recurrent_theta.packed_src,
              inputs,
              state0,
              use_deterministic_random=True)
          state1.step_outs = step_outs
        # TODO(syzhang, tsainath): Add SS into Functional Decoder, which
        # requires computing softmax logits.
        state1 = self.PostStepDecoderStateUpdate(state1, inputs.label)
        return state1, py_utils.NestedMap()

      accumulated_states, _ = recurrent.Recurrent(
          recurrent_theta, state0_no_fusion, inputs, RnnStep)
      # Give them names, so that they can be fetched in unit tests.
      tf.identity(
          accumulated_states.misc_states.step_state.global_step,
          name='accumulated_global_steps')
      tf.identity(
          accumulated_states.misc_states.step_state.time_step,
          name='accumulated_time_steps')

      if not p.softmax_uses_attention:
        step_out, _ = tf.split(
            accumulated_states.step_outs, [p.rnn_cell_dim, atten_context_dim],
            axis=-1)
      else:
        step_out = accumulated_states.step_outs
      softmax_input, state0.fusion_states = self.fusion.FuseOutput(
          theta.fusion, state0.fusion_states, lm_output, step_out)
      # TODO(syzhang): understand why we have to construct softmax outside the
      # recurrent loop; otherwise, the BProp numbers don't match.
      xent_loss = self.softmax.FProp(
          theta.softmax, [softmax_input],
          class_weights=inputs.weight,
          class_ids=inputs.label)
      # TODO(syzhang): supports AddAdditionalDecoderSummaries().
      atten_states = accumulated_states.atten_states
      if isinstance(atten_states, py_utils.NestedMap):
        additional_atten_probs = sorted(
            [(name, tensor)
             for name, tensor in atten_states.FlattenItems()
             if name.endswith('probs')])
      else:
        additional_atten_probs = []
      rnn_outs = [
          cell.GetOutput(accumulated_states.rnn_states[i])
          for i, cell in enumerate(self.rnn_cell)
      ]
      self._AddDecoderActivationsSummary(
          source_encs,
          source_paddings,
          targets,
          accumulated_states.atten_probs,
          rnn_outs,
          softmax_input,
          additional_atten_probs=additional_atten_probs,
          target_alignments=getattr(targets, 'alignments', None))
      # seq_logits: [time, batch, num_classes].
      seq_logits = xent_loss.logits
      adjusted_logits = self.fusion.ComputeLogitsWithLM(state0.fusion_states,
                                                        seq_logits)
      predictions = py_utils.NestedMap(
          # Transpose to [batch, time, num_classes].
          logits_without_bias=tf.transpose(seq_logits, [1, 0, 2]),
          logits=tf.transpose(adjusted_logits, [1, 0, 2]))
      attention_map = py_utils.NestedMap(probs=accumulated_states.atten_probs)
      for k, v in additional_atten_probs:
        attention_map[k] = v
      # Transpose attention probs from [target_length, batch, source_length] to
      # [batch, target_length, source_length].
      predictions.attention = attention_map.Transform(
          lambda x: tf.transpose(x, [1, 0, 2]))
      return predictions

  def SingleDecodeStep(self,
                       theta,
                       packed_src,
                       cur_target_info,
                       decoder_step_state,
                       per_step_src_padding=None,
                       use_deterministic_random=False):
    """Computes one 'step' of computation for the decoder.

    Must be implemented by sub-classes. Residual connections must also be taken
    care of in sub-classes.

    Args:
      theta: A NestedMap object containing weights' values of this
        layer and its children layers.
      packed_src: A NestedMap to represent the packed source tensors generated
        by the attention model.
      cur_target_info: TargetInfo namedtuple, which represents the targets
        which represents information about the target at this step. It is up
        to the various sub-classes to determine how to process the current
        target.
      decoder_step_state: DecoderStepState which encapsulates the state of the
        decoder before computing outputs at the current step.
      per_step_src_padding: Optional padding to be applied to the source_encs
        which overrides the default padding in source_paddings. Used, for
        example, by the Neural Transducer (NT) decoder.
      use_deterministic_random: whether to use deterministic random numbers when
        needed. Must be set to True if called from functional recurrent.

    Returns:
      A tuple (step_out, new_decoder_state) which represent the outputs of the
      decoder (usually logits), and the new decoder state after processing the
      current step.
    """
    # TODO(syzhang): unify the API to always pass in packed_src.
    raise NotImplementedError('Must be implemented by sub-classes.')

  def MiscZeroState(self, source_encs, source_paddings, target_ids, bs):
    """Returns initial state for other miscellaneous states, if any."""
    del source_encs, source_paddings
    misc_zero_state = py_utils.NestedMap(
        step_state=py_utils.NestedMap(
            global_step=py_utils.GetOrCreateGlobalStep(),
            time_step=tf.constant(0, dtype=tf.int64)))
    p = self.params
    if self._max_label_prob > 0:
      misc_zero_state.prev_predicted_ids = tf.reshape(target_ids[:, 0], [bs])
      step = tf.to_float(py_utils.GetOrCreateGlobalStep())
      sampling_p = (step - p.prob_decay_start_step) / self._decay_interval
      groundtruth_p = 1 - (self._max_label_prob * sampling_p)
      groundtruth_p = tf.maximum(groundtruth_p, p.min_ground_truth_prob)
      groundtruth_p = tf.minimum(groundtruth_p, 1.0)
      summary_utils.scalar(p, 'ground_truth_sampling_probability',
                           groundtruth_p)
      misc_zero_state.groundtruth_p = groundtruth_p
    return misc_zero_state

  def TargetsToBeFedAtCurrentDecodeStep(self, time, theta, decoder_step_state,
                                        target_info_tas, seq_out_tas):
    del seq_out_tas

    target_id = target_info_tas.id.read(time)
    label = target_info_tas.label.read(time)
    weight = tf.squeeze(target_info_tas.weight.read(time))
    emb = target_info_tas.emb.read(time)
    padding = target_info_tas.padding.read(time)
    misc = py_utils.NestedMap()

    # Use different id and embedding for scheduled sampling.
    if self._max_label_prob > 0:
      dec_bs = tf.shape(decoder_step_state.misc_states.prev_predicted_ids)[0]
      pick_groundtruth = tf.less(
          tf.random_uniform([dec_bs], seed=self.params.random_seed),
          decoder_step_state.misc_states.groundtruth_p)
      emb = tf.where(
          pick_groundtruth, target_info_tas.emb.read(time),
          self.emb.EmbLookup(
              theta.emb,
              tf.stop_gradient(
                  decoder_step_state.misc_states.prev_predicted_ids)))
      target_id = tf.where(pick_groundtruth, target_info_tas.id.read(time),
                           decoder_step_state.misc_states.prev_predicted_ids)
    return AsrDecoderBase.TargetInfo(
        id=target_id,
        label=label,
        weight=weight,
        emb=emb,
        padding=padding,
        misc=misc)

  def OverrideEmbedding(self, target_info, new_emb):
    """Replaces target_info.emb with new_emb and returns the result tuple."""
    return AsrDecoderBase.TargetInfo(
        id=target_info.id,
        label=target_info.label,
        weight=target_info.weight,
        emb=new_emb,
        padding=target_info.padding,
        misc=target_info.misc)

  def PostStepDecoderStateUpdate(self, decoder_step_state, logits=None):
    """Update decoder states and logits after SingleDecodeStep.

    Args:
      decoder_step_state: A NestedMap object which encapsulates decoder states.
      logits: a tensor, predicted logits.

    Returns:
      decoder_step_state.

    Raises:
      ValueError: if scheduled sampling is used for functional unrolling or
                  if logits is None for while loop based unrolling.
    """
    if not self.params.use_while_loop_based_unrolling:
      if self.params.min_ground_truth_prob < 1.0:
        raise ValueError('SS is not yet supported')
    else:
      if logits is None:
        raise ValueError('logits cannot be None')
      decoder_step_state.logits = logits

      if self._max_label_prob > 0:
        bs = tf.shape(logits)[0]
        # log_probs: [bs, num_classes]
        log_probs = tf.nn.log_softmax(logits)
        # log_prob_sample: [bs, 1]
        log_prob_sample = tf.multinomial(
            log_probs, 1, seed=self.params.random_seed)
        # pred_ids: [bs]
        pred_ids = tf.reshape(tf.to_int32(log_prob_sample), [bs])
        decoder_step_state.misc_states.prev_predicted_ids = pred_ids

    decoder_step_state.misc_states.step_state.time_step += 1
    return decoder_step_state


class AsrDecoder(AsrDecoderBase):
  """Step-by-step decoder with LM fusion."""

  @classmethod
  def Params(cls):
    p = super(AsrDecoder, cls).Params()
    return p

  def AddAdditionalDecoderSummaries(self, source_encs, source_paddings, targets,
                                    seq_out_tas, softmax_input):
    """Add summaries not covered by the default activations summaries.

    Args:
      source_encs: a tensor of shape [time, batch_size, source_dim].
      source_paddings: a tensor of shape [time, batch_size].
      targets: a NestedMap containing target info.
      seq_out_tas: a SequenceOutTensorArrays.
      softmax_input: a tensor of shape [batch, time, vocab_size].
    """
    if cluster_factory.Current().add_summary:
      self.fusion.AddAdditionalDecoderSummaries(
          source_encs, source_paddings, targets, seq_out_tas, softmax_input)

  def _ComputeAttention(self,
                        theta,
                        rnn_out,
                        packed_src,
                        attention_state,
                        per_step_src_padding=None,
                        step_state=None,
                        query_segment_id=None):
    """Runs attention and computes context vector.

    Can be overridden by a child class if attention is computed differently.

    Args:
      theta: A NestedMap object containing weights for the attention layers.
        Expects a member named 'atten'.
      rnn_out: A Tensor of shape [batch_size, query_dim]; output of the
        first layer of decoder RNN, which is the query vector used for
        attention.
      packed_src: A NestedMap returned by self.atten.InitForSourcePacked.
      attention_state: The attention state computed at the previous timestep.
        Varies with the type of attention, but is usually a Tensor or a
        NestedMap of Tensors of shape [batch_size, <state_dim>].
      per_step_src_padding: Source sequence padding to apply at this step.
      step_state: A NestedMap containing 'global_step' and 'time_step'.
      query_segment_id: a tensor of shape [batch_size].

    Returns:
      The attention context vector: A Tensor of shape [batch_size, context_dim].
      The attention probability vector: A Tensor of shape [batch_size, seq_len]
      The attention state: A Tensor or a NestedMap of Tensors of shape
        [batch_size, <state_dim>].
    """
    return self.atten.ComputeContextVectorWithSource(
        theta.atten,
        packed_src,
        rnn_out,
        attention_state=attention_state,
        per_step_source_padding=per_step_src_padding,
        step_state=step_state,
        query_segment_id=query_segment_id)

  def SingleDecodeStep(self,
                       theta,
                       packed_src,
                       cur_target_info,
                       decoder_step_state,
                       per_step_src_padding=None,
                       use_deterministic_random=False):
    """Decode one step.

    Note that the implementation of attention here follows the model in
    https://arxiv.org/pdf/1609.08144.pdf, detailed more in
    https://arxiv.org/pdf/1703.08581.pdf.

    Args:
      theta: A NestedMap object containing weights' values of this
        layer and its children layers.
      packed_src: A NestedMap to represent the packed source tensors generated
        by the attention model.
      cur_target_info: TargetInfo namedtuple, which represents the targets
        which represents information about the target at this step. It is up
        to the various sub-classes to determine how to process the current
        target.
      decoder_step_state: DecoderStepState which encapsulates the state of the
        decoder before computing outputs at the current step.
      per_step_src_padding: Optional padding to be applied to the source_encs
        which overrides the default padding in source_paddings. Used, for
        example, by the Neural Transducer (NT) decoder.
      use_deterministic_random: whether to use deterministic random numbers when
        needed. Must be set to True if called from functional recurrent.

    Returns:
      A tuple (step_out, new_decoder_state) which represent the outputs of the
      decoder (usually logits), and the new decoder state after processing the
      current step.
    """
    misc_states = decoder_step_state.misc_states
    new_rnn_states = []
    new_rnn_states_0, _ = self.rnn_cell[0].FProp(
        theta.rnn_cell[0], decoder_step_state.rnn_states[0],
        py_utils.NestedMap(
            act=[cur_target_info.emb, decoder_step_state.atten_context],
            padding=cur_target_info.padding))
    new_rnn_states.append(new_rnn_states_0)
    rnn_out = self.rnn_cell[0].GetOutput(new_rnn_states_0)

    (new_atten_context, new_atten_probs,
     new_atten_states) = self._ComputeAttention(
         theta,
         rnn_out,
         packed_src,
         decoder_step_state.atten_states,
         per_step_src_padding=per_step_src_padding,
         step_state=misc_states.step_state)
    # Here the attention context is being updated according to the
    # contextualizer (the default contextualizer is a no-op).
    new_atten_context = self.contextualizer.QueryAttention(
        theta.contextualizer, rnn_out, misc_states, new_atten_context,
        packed_src)
    for i, cell in enumerate(self.rnn_cell[1:], 1):
      new_rnn_states_i, _ = cell.FProp(
          theta.rnn_cell[i], decoder_step_state.rnn_states[i],
          py_utils.NestedMap(
              act=[rnn_out, new_atten_context],
              padding=cur_target_info.padding))
      new_rnn_states.append(new_rnn_states_i)
      new_rnn_out = cell.GetOutput(new_rnn_states_i)
      new_rnn_out = self._ApplyDropout(
          new_rnn_out,
          deterministic=use_deterministic_random,
          # Use i * 1000 as extra seed to make dropout at every layer different.
          extra_seed=i * 1000,
          step_state=misc_states.step_state)
      if i + 1 >= self.params.residual_start > 0:
        rnn_out += new_rnn_out
      else:
        rnn_out = new_rnn_out

    step_out = tf.concat([rnn_out, new_atten_context], 1)

    return step_out, py_utils.NestedMap(
        rnn_states=new_rnn_states,
        atten_context=new_atten_context,
        atten_probs=new_atten_probs,
        atten_states=new_atten_states,
        misc_states=misc_states)

  def _GetNumHypsForBeamSearch(self, source_encs, num_hyps_per_beam):
    """Returns number of hypothesis times batch_size.

    This function can be overridden by a child class if the total number of
    hyps are to be computed in a different way, e.g., when the format of inputs
    change.
    Args:
      source_encs: A Tensor of [dim, batch] dimension with source encodings.
      num_hyps_per_beam: Int, the number of hypothesis per example in the beam.
    Returns:
      A Tensor with value batch * num_hyps_per_beam.
    """
    return tf.shape(source_encs)[1] * num_hyps_per_beam

  def _PostProcessAttenProbsForBeamSearch(self, atten_probs):
    """Returns the attention probabilities after optional post processing.

    This is a noop for the base class. But this function can be overridden
    by a child class, e.g., when the format of probabilities change.
    Args:
      atten_probs: A Tensor of [batch, source_len] dimension with atten probs.
    Returns:
      A Tensor with processed atten_probs. The same as input in this case.
    """
    return atten_probs

  def _InitBeamSearchStateCallback(self,
                                   theta,
                                   source_encs,
                                   source_paddings,
                                   num_hyps_per_beam,
                                   additional_source_info=None):
    # additional_source_info is currently not used.
    del additional_source_info
    num_hyps = self._GetNumHypsForBeamSearch(source_encs, num_hyps_per_beam)
    (rnn_states, atten_context, atten_probs,
     atten_states, fusion_states, misc_states) = self.InitDecoder(
         theta, source_encs, source_paddings, num_hyps)
    atten_probs = self._PostProcessAttenProbsForBeamSearch(atten_probs)
    all_atten_states = py_utils.NestedMap({
        'atten_context': atten_context,
        'atten_probs': atten_probs,
        'atten_states': atten_states
    })

    initial_results = py_utils.NestedMap({'atten_probs': atten_probs})
    other_states = py_utils.NestedMap({
        'rnn_states': rnn_states,
        'all_atten_states': all_atten_states,
        'fusion_states': fusion_states,
        'misc_states': misc_states,
    })
    return initial_results, other_states

  def _PreBeamSearchStepCallback(self,
                                 theta,
                                 source_encs,
                                 source_paddings,
                                 step_ids,
                                 states,
                                 num_hyps_per_beam,
                                 additional_source_info=None):
    p = self.params
    # additional_source_info is currently not used.
    del additional_source_info
    fake_step_labels = tf.identity(step_ids)
    step_paddings = tf.zeros(tf.shape(step_ids), dtype=p.dtype)
    step_weights = tf.ones(tf.shape(step_ids), dtype=p.dtype)
    embs = self.emb.EmbLookup(theta.emb, tf.reshape(step_ids, [-1]))
    prev_rnn_states = states.rnn_states
    prev_atten_states = states.all_atten_states.atten_states
    prev_atten_context = states.all_atten_states.atten_context
    prev_atten_probs = states.all_atten_states.atten_probs
    prev_fusion_states = states.fusion_states
    prev_misc_states = states.misc_states

    prev_decoder_step_state = py_utils.NestedMap(
        rnn_states=prev_rnn_states,
        atten_context=prev_atten_context,
        atten_probs=prev_atten_probs,
        atten_states=prev_atten_states,
        misc_states=prev_misc_states)
    # TODO(prabhavalkar): Must handle CreateMiscTargetInfo during beam search
    # eval.
    cur_target_info = AsrDecoderBase.TargetInfo(
        id=tf.reshape(step_ids, [-1]),
        label=None,
        weight=None,
        emb=embs,
        padding=step_paddings,
        misc=py_utils.NestedMap())

    lm_output, fusion_states = self.fusion.FPropLm(
        theta.fusion, prev_fusion_states, cur_target_info.id,
        cur_target_info.padding)

    fused_emb, fusion_states = self.fusion.FuseEmb(
        theta.fusion, fusion_states, lm_output, cur_target_info.emb)
    cur_target_info = self.OverrideEmbedding(cur_target_info, fused_emb)

    packed_src = self._InitAttention(theta, source_encs, source_paddings)
    step_out, new_decoder_step_state = self.SingleDecodeStep(
        theta,
        packed_src,
        cur_target_info=cur_target_info,
        decoder_step_state=prev_decoder_step_state)
    (atten_context, atten_probs, rnn_states, atten_states,
     misc_states) = (new_decoder_step_state.atten_context,
                     new_decoder_step_state.atten_probs,
                     new_decoder_step_state.rnn_states,
                     new_decoder_step_state.atten_states,
                     new_decoder_step_state.misc_states)

    if p.softmax_uses_attention:
      # [batch, dims]
      softmax_input = step_out
    else:
      # Strip the attention context from the last dimension of softmax_input.
      # TODO(prabhavalkar): This currently assumes that the context is appended
      # to the end, see tf.concat in
      # AsrDecoderBase.ComputePredictionsFunctional().RnnStep().  Refactor the
      # code so as to remove this assumption.
      atten_context_dim = self._GetAttenContextDim()
      softmax_input, _ = tf.split(
          step_out, [p.rnn_cell_dim, atten_context_dim], axis=-1)

    softmax_input, fusion_states = self.fusion.FuseOutput(
        theta.fusion, fusion_states, lm_output, softmax_input)

    xent_loss = self.softmax.XentLoss(
        [softmax_input],
        class_weights=step_weights,
        class_ids=fake_step_labels,
    )

    logits = self.fusion.ComputeLogitsWithLM(
        fusion_states, xent_loss.logits, is_eval=True)
    if p.use_unnormalized_logits_as_log_probs:
      log_probs = logits
    else:
      log_probs = tf.nn.log_softmax(logits)

    atten_probs = self._PostProcessAttenProbsForBeamSearch(atten_probs)
    bs_results = py_utils.NestedMap({
        'atten_probs': atten_probs,
        'log_probs': log_probs,
    })
    all_atten_states = py_utils.NestedMap({
        'atten_context': atten_context,
        'atten_probs': atten_probs,
        'atten_states': atten_states
    })
    new_states = py_utils.NestedMap({
        'rnn_states': rnn_states,
        'all_atten_states': all_atten_states,
        'fusion_states': fusion_states,
        'misc_states': misc_states,
    })
    return bs_results, new_states

  def _PostBeamSearchStepCallback(self,
                                  theta,
                                  source_encs,
                                  source_paddings,
                                  new_step_ids,
                                  states,
                                  additional_source_info=None):
    del source_encs, source_paddings, new_step_ids, additional_source_info
    return states

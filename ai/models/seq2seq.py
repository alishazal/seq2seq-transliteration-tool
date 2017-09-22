"""TODO: add descriptive docstring."""

from __future__ import division

from six.moves import xrange
import tensorflow as tf
# pylint: disable=no-name-in-module
from tensorflow.python.layers.core import Dense
from tensorflow.python.ops.init_ops import Initializer

from ai.models import BaseModel


class Seq2Seq(BaseModel):
  """Sequence to Sequence model with an attention mechanism. Note that for the
     best results, the model assumes that the inputs and targets are
     preprocessed with the following conventions:
     1. All the inputs are padded with a unique `pad_id`,
     2. All the labels have a unique `eos_id` as the final token,
     3. A `go_id` is reserved for the model to provide to the decoder."""
  
  def __init__(self, num_types=0, max_encoder_length=99, max_decoder_length=99,
               pad_id=0, eos_id=1, go_id=2,
               adam_lr=1e-3, adam_lr_decay=1.,
               gd_lr=.1, gd_lr_decay=1.,
               batch_size=32, embedding_size=128, train_embeddings=True,
               default_embedding_matrix=None, rnn_layers=2,
               bidirectional_encoder=False, bidirectional_mode='add',
               pyramid_encoder=False, use_lstm=False, use_residual=False,
               attention=None, feed_inputs=False, dropout=1.,
               max_grad_norm=5., epsilon=1e-8, beam_size=1., **kw):
    """Keyword args:
       `num_types`: number of unique types (e.g. vocabulary or alphabet size),
       `max_encoder_length`: max length of the encoder,
       `max_decoder_length`: max length of the decoder,
       `pad_id`: the integer id that represents padding (defaults to 0),
       `eos_id`: the integer id that represents the end of the sequence,
       `go_id`: the integer id fed to the decoder as the first input,
       `adam_lr`: initial learning rate for Adam optimizer,
       `adam_lr_decay`: learning rate decay for Adam optimizer,
       `gd_lr`: initial learning rate for gradient descent optimizer,
       `gd_lr_decay`: learning rate decay for gradient descent optimizer,
       `batch_size`: minibatch size,
       `embedding_size`: integer number of hidden units,
       `train_embeddings`: whether to do backprop on the embeddings,
       `default_embedding_matrix`: if None, set to a random uniform
        distribution with mean 0 and variance 1,
       `rnn_layers`: number of RNN layers for the encoder and decoder,
       `bidirectional_encoder`: whether to use a bidirectional encoder RNN,
       `bidirectional_mode`: string for the bidirectional RNN architecture:
        'add' (default): add the forward and backward hidden states,
        'project': use a projection matrix to resize the concatenation of the
                   forward and backward hidden states to `embedding_size`,
        'concat': concatenate the forward and backward inputs and pass that
                  as the input to the next RNN (note: this will not allow the
                  use of residual connections),
       `pyramid_encoder`: whether to use a pyramid encoder that halves the
        number of time steps per layer (Xie et al.,
        https://arxiv.org/pdf/1603.09727),
       `use_lstm`: set to False to use a GRU cell (Cho et al.,
        https://arxiv.org/abs/1406.1078),
       `use_residual`: whether to use residual connections between RNN cells
        (Wu et al., https://arxiv.org/pdf/1609.08144.pdf),
       `attention`: 'bahdanau', or 'luong' (none by default),
       `feed_inputs`: set to True to feed attention-based inputs to the
        decoder RNN (Luong et al., https://arxiv.org/abs/1508.04025),
       `dropout`: keep probability for the non-recurrent connections between
        RNN cells. Defaults to 1.0; i.e. no dropout,
       `max_grad_norm`: clip gradients to maximally this norm,
       `epsilon`: small numerical constant for AdamOptimizer (default 1e-8)."""
    self.num_types = num_types
    self.max_encoder_length = max_encoder_length
    self.max_decoder_length = max_decoder_length
    self.pad_id = pad_id
    self.eos_id = eos_id
    self.go_id = go_id
    self.adam_lr_decay = adam_lr_decay
    self.gd_lr_decay = gd_lr_decay
    self.batch_size = batch_size
    self.embedding_size = embedding_size
    self.train_embeddings = train_embeddings
    self.default_embedding_matrix = default_embedding_matrix
    self.rnn_layers = rnn_layers
    self.bidirectional_encoder = bidirectional_encoder
    self.bidirectional_mode = bidirectional_mode
    self.pyramid_encoder = pyramid_encoder
    self.use_lstm = use_lstm
    self.use_residual = use_residual
    self.attention = attention
    self.feed_inputs = feed_inputs
    self.dropout = dropout
    self.max_grad_norm = max_grad_norm
    self.epsilon = epsilon
    self.beam_size = beam_size
    # Use graph variables for learning rates to allow them to be modified/saved
    self.adam_lr = tf.Variable(
      adam_lr, trainable=False, dtype=tf.float32, name='adam_learning_rate')
    self.gd_lr = tf.Variable(
      gd_lr, trainable=False, dtype=tf.float32, name='gd_learning_rate')
    # Sampling probability variable that can be manually changed. See scheduled
    # sampling paper (Bengio et al., https://arxiv.org/abs/1506.03099)
    self.p_sample = tf.Variable(
      0., trainable=False, dtype=tf.float32, name='sampling_probability')
    super(Seq2Seq, self).__init__(**kw)
  
  
  def build_graph(self):
    
    # Placeholders
    self.inputs = tf.placeholder(
      tf.int32, name='inputs',
      shape=[self.batch_size, self.max_encoder_length])
    self.labels = tf.placeholder(
      tf.int32, name='labels',
      shape=[self.batch_size, self.max_decoder_length])
    self.temperature = tf.placeholder_with_default(
      1., name='temperature', shape=[])
    
    # Sequence lengths - used throughout model
    with tf.name_scope('input_lengths'):
      self.input_lengths = tf.reduce_sum(
        tf.sign(tf.abs(self.inputs - self.pad_id)), reduction_indices=1)
    
    # Prepare training decoder inputs
    with tf.name_scope('train_decoder_inputs'):
      decoder_ids = tf.concat(
        [tf.tile([[self.go_id]], [self.batch_size, 1]), self.labels], 1)
    
    # Embedding matrix
    with tf.variable_scope('embeddings'):
      if self.default_embedding_matrix is not None:
        if isinstance(self.default_embedding_matrix, Initializer):
          initializer = self.default_embedding_matrix
        else:
          initializer = tf.constant_initializer(self.default_embedding_matrix)
      else:
        sq3 = 3 ** .5  # Uniform(-sqrt3, sqrt3) has variance 1
        # pylint: disable=redefined-variable-type
        initializer = tf.random_uniform_initializer(minval=-sq3, maxval=sq3)
      
      self.embedding_kernel = tf.get_variable(
        'kernel', [self.num_types, self.embedding_size],
        trainable=self.train_embeddings, initializer=initializer)
    
    # Look up the embeddings for the encoder and decoder inputs
    encoder_input = self.get_embeddings(self.inputs)
    decoder_input = self.get_embeddings(decoder_ids)
    
    with tf.variable_scope('encoder'):
      encoder_output = self.build_encoder(encoder_input)
    
    with tf.variable_scope('decoder'):
      logits, self.generative_output = self.build_decoder(
        encoder_output, decoder_input)
    
    # Softmax cross entropy loss masked by the target sequence lengths
    with tf.name_scope('loss'):
      mask = tf.cast(tf.sign(self.labels), tf.float32)
      loss = tf.contrib.seq2seq.sequence_loss(logits, self.labels, mask)
    
    self.perplexity = tf.exp(loss, name='perplexity')
    tf.summary.scalar('perplexity', self.perplexity)
    
    # Index outputs (greedy)
    self.output = tf.argmax(
      logits, axis=2, name='output', output_type=tf.int32)
    
    # Compute the edit distance for evaluations
    hypothesis = self.make_eval_tensor(self.output)
    truth = self.make_eval_tensor(self.labels)
    self.edit_distance = tf.reduce_mean(tf.edit_distance(hypothesis, truth))
    tf.summary.scalar('edit_distance', self.edit_distance)
    
    # Adam and gradient descent optimizers with norm clipping. This prevents
    # exploding gradients and allows a switch from Adam to SGD when the model
    # is reaching convergence (Wu et al., https://arxiv.org/pdf/1609.08144.pdf)
    with tf.name_scope('train_ops'):
      tvars = tf.trainable_variables()
      grads, _ = tf.clip_by_global_norm(
        tf.gradients(loss, tvars), self.max_grad_norm)
      adam_optimizer = tf.train.AdamOptimizer(
        self.adam_lr, epsilon=self.epsilon)
      gradient_descent = tf.train.GradientDescentOptimizer(self.gd_lr)
      self.adam = adam_optimizer.apply_gradients(
        zip(grads, tvars), global_step=self.global_step)
      self.sgd = gradient_descent.apply_gradients(
        zip(grads, tvars), global_step=self.global_step)
    
    # Runnable ops for decaying the learning rates
    with tf.name_scope('decay_lr'):
      self.decay_adam_lr = tf.assign(
        self.adam_lr, self.adam_lr * self.adam_lr_decay,
        name='decay_adam_learning_rate')
      self.decay_gd_lr = tf.assign(
        self.gd_lr, self.gd_lr * self.gd_lr_decay,
        name='decay_gd_learning_rate')
  
  
  def get_embeddings(self, ids):
    """Performs embedding lookup. Useful as a method for decoder helpers.
       Note this method requires the `embedding_kernel` attribute to be
       declared before being called."""
    return tf.nn.embedding_lookup(self.embedding_kernel, ids)
  
  
  def rnn_cell(self, num_units=None, attention_mechanism=None):
    """Get a new RNN cell with wrappers according to the initial config."""
    cell = None
    
    # Allow custom number of hidden units
    if num_units is None:
      num_units = self.embedding_size
    
    # Check to use LSTM or GRU
    if self.use_lstm:
      cell = tf.contrib.rnn.LSTMBlockCell(num_units)
    else:
      cell = tf.contrib.rnn.GRUBlockCell(num_units)
    
    # Check whether to add an attention mechanism
    if attention_mechanism is not None:
      cell = tf.contrib.seq2seq.AttentionWrapper(cell, attention_mechanism)
    
    # Check whether to add residual connections
    if self.use_residual:
      cell = tf.contrib.rnn.ResidualWrapper(cell)
    
    # Note: dropout should always be the last wrapper
    if self.dropout < 1:
      cell = tf.contrib.rnn.DropoutWrapper(
        cell, input_keep_prob=self.dropout, output_keep_prob=self.dropout)
    
    return cell
  
  
  def build_encoder(self, encoder_input):
    """Build the RNN stack for the encoder, depending on the initial config."""
    
    # We make only the first encoder layer bidirectional to capture the context
    # (Wu et al., https://arxiv.org/pdf/1609.08144.pdf)
    if self.bidirectional_encoder:
      (encoder_fw_out, encoder_bw_out), _ = tf.nn.bidirectional_dynamic_rnn(
        self.rnn_cell(), self.rnn_cell(), encoder_input, dtype=tf.float32,
        sequence_length=self.input_lengths)
      
      # Postprocess the bidirectional output according to the initial config
      if self.bidirectional_mode == 'add':
        encoder_output = encoder_fw_out + encoder_bw_out
      else:
        encoder_output = tf.concat([encoder_fw_out, encoder_bw_out], 2)
        if self.bidirectional_mode == 'project':
          encoder_output = tf.layers.dense(
            encoder_output, self.embedding_size, activation=tf.tanh,
            name='bidirectional_projection')
    else:
      encoder_output, _ = tf.nn.dynamic_rnn(
        self.rnn_cell(), encoder_input, dtype=tf.float32,
        sequence_length=self.input_lengths)
    
    # Only for deep RNN architectures
    if self.rnn_layers > 1:
      
      if self.pyramid_encoder:
        for i in xrange(1, self.rnn_layers):
          # TODO: inspect this
          # Concatenate adjacent pairs and reshape them to their original size
          eo_sh = encoder_output.get_shape()
          concat_shape = map(int, [eo_sh[0], eo_sh[1] / 2, eo_sh[2] * 2])
          encoder_output = tf.layers.dense(
            tf.reshape(encoder_output, concat_shape), self.embedding_size,
            name='pyramid_projection_{}'.format(i))
          # Run the next layer with half as many time steps
          encoder_output, _ = tf.nn.dynamic_rnn(
            self.rnn_cell(self.embedding_size / (2 ** i)),
            tf.reshape(encoder_output, concat_shape), dtype=tf.float32)
      else:
        encoder_cells = tf.contrib.rnn.MultiRNNCell(
          [self.rnn_cell() for _ in xrange(self.rnn_layers - 1)])
        encoder_output, _ = tf.nn.dynamic_rnn(
          encoder_cells, encoder_output, dtype=tf.float32,
          sequence_length=self.input_lengths)
    
    return tf.contrib.seq2seq.tile_batch(encoder_output, self.beam_size)
  
  
  def build_decoder(self, encoder_output, decoder_input):
    """Build the decoder RNN stack and the final prediction layer."""
    
    final_encoder_lengths = self.input_lengths
    if self.pyramid_encoder and self.rnn_layers > 1:
      time_steps = int(self.max_encoder_length / (2 ** (self.rnn_layers - 1)))
      final_encoder_lengths = tf.ones([self.batch_size, time_steps])
    
    # The first RNN is wrapped with the attention mechanism
    # TODO: make Luong attention actually follow the computational steps
    # described in the paper and implement input-feeding approach
    # (Luong et al., https://arxiv.org/abs/1508.04025)
    attention_mechanism = None
    if self.attention == 'bahdanau':
      attention_mechanism = tf.contrib.seq2seq.BahdanauAttention(
        self.embedding_size, encoder_output,
        memory_sequence_length=final_encoder_lengths)
    elif self.attention == 'luong':
      attention_mechanism = tf.contrib.seq2seq.LuongAttention(
        self.embedding_size, encoder_output,
        memory_sequence_length=final_encoder_lengths)
    
    decoder_cell = self.rnn_cell(attention_mechanism=attention_mechanism)
    
    # Use the first output of the encoder to learn an initial decoder state
    initial_state_pass = tf.split(tf.layers.dense(
      encoder_output[:, 0], self.embedding_size * self.rnn_layers,
      activation=tf.tanh, name='initial_decoder_state'
    ), self.rnn_layers, axis=1)
    beam_batch_size = self.batch_size * self.beam_size  # shortcut
    
    if self.attention:
      initial_state = tf.contrib.seq2seq.AttentionWrapperState(
        cell_state=initial_state_pass[0],
        attention=tf.zeros([beam_batch_size, self.embedding_size]),
        alignments=tf.zeros([beam_batch_size, self.max_decoder_length]),
        time=tf.zeros(()), alignment_history=())
    else:
      initial_state = initial_state_pass[0]
    
    # For deep RNNs, stack the cells and use an initial state that merges the
    # initial state (maybe with attention) with the rest of the learned states
    if self.rnn_layers > 1:
      decoder_cell = tf.contrib.rnn.MultiRNNCell(
        [decoder_cell] + [self.rnn_cell()
                          for _ in xrange(self.rnn_layers - 1)])
      initial_state = tuple([initial_state] + list(initial_state_pass[1:]))
    
    # Training decoder with optional scheduled sampling. If sampling occurs,
    # the output will be fed through the final prediction layer and then fed
    # back to the embedding layer for consistency.
    # TODO: allow option to choose between embedding and output helpers
    sampling_helper = tf.contrib.seq2seq.ScheduledEmbeddingTrainingHelper(
      tf.contrib.seq2seq.tile_batch(decoder_input, self.beam_size),
      tf.tile([self.max_decoder_length], [beam_batch_size]),
      self.get_embeddings, self.p_sample)
    dense = Dense(
      self.num_types, name='dense', activation=lambda x: x / self.temperature)
    decoder = tf.contrib.seq2seq.BasicDecoder(
      decoder_cell, sampling_helper, initial_state, output_layer=dense)
    decoder_output = tf.contrib.seq2seq.dynamic_decode(decoder)
    logits = decoder_output[0].rnn_output
    
    # TODO: make beam search work
    # generative_decoder = tf.contrib.seq2seq.BeamSearchDecoder(
    #   decoder_cell, self.get_embeddings,
    #   tf.tile([self.go_id], [self.batch_size]), self.eos_id,
    #   initial_state, self.beam_size, output_layer=dense)
    ### TEMPORARY
    ghelper = tf.contrib.seq2seq.GreedyEmbeddingHelper(
      self.get_embeddings, tf.tile([self.go_id], [self.batch_size]),
      self.eos_id)
    generative_decoder = tf.contrib.seq2seq.BasicDecoder(
      decoder_cell, ghelper, initial_state, output_layer=dense)
    ### END TEMPORARY
    tf.get_variable_scope().reuse_variables()
    generative_output = tf.contrib.seq2seq.dynamic_decode(
      generative_decoder, maximum_iterations=self.max_decoder_length)
    return logits, generative_output
  
  
  def make_eval_tensor(self, sequences):
    """Given a tensor of arrays of sequences, make a `SparseTensor` to feed to
       the `tf.edit_distance` method."""
    indices = tf.where(tf.not_equal(sequences, 0))
    return tf.SparseTensor(
      indices=indices,
      values=tf.gather_nd(sequences, indices),
      dense_shape=[self.batch_size, self.max_decoder_length])

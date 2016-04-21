
import tensorflow as tf
from random import randrange
from ale_python_interface import ALEInterface
import threading
from skimage.transform import resize
import numpy as np
from collections import deque
from model import build_network, loss

ACTIONS = 4
NUM_CONCURRENT = 4
GAMMA = 0.01

class Environment(object):
  """
  Object that wraps each actor-learner's individual game environment.
  Responsible for:
   - Safely initializing and loading game
   - Returning preprocessed game state to the actor-learner
   - Excecuting actions on behalf of the actor-learner, returning the reward
  """
  def __init__(self, ale_io_lock):
    self.ale = self.init_ale(ale_io_lock)
    self.frames_to_skip = 4
    self.min_action_set = self.ale.getMinimalActionSet()
    self.buffer_length = 2
    self.width, self.height = self.ale.getScreenDims()
    self.screen_buffer = np.empty((self.buffer_length,
                                   self.height, self.width),
                                  dtype=np.uint8)
    self.state_buffer = deque()

    self.index = 0
    self.max_start_nullops = 30
    self.rng = np.random.RandomState(123456)
    self.resized_width = 84
    self.resized_height = 84
    self.agent_history_length = 4

  def init_ale(self, ale_io_lock):
    ale_io_lock.acquire()
    ale = ALEInterface()
    ale.setInt('random_seed', 123)
    ale.loadROM('/Users/coreylynch/dev/atari_roms/breakout.bin')
    ale_io_lock.release()
    return ale

  def num_actions(self):
    return len(self.min_action_set)

  def _repeat_action(self, action):
    """
    From Mnih et al., "...the agent sees and selects actions on
    every kth frame instead of every frame, and its last action
    is repeated on skipped frames." 
    This repeats the chosen action the appopriate number of times 
    and returns the summed reward. 
    """
    reward = 0
    for _ in range(self.frames_to_skip):
        reward += self._act(action)
    return reward

  def _act(self, action):
    """
    Perform the indicated action for a single frame, return the
    resulting reward and store the resulting screen image in the
    buffer.
    """
    reward = self.ale.act(action)
    self.ale.getScreenGrayscale(self.screen_buffer[self.index, ...])
    self.index = (0 if self.index == 1 else 1)
    return reward

  def _init_new_episode(self):
    """ Resets the game if needed, performs enough null
    actions to ensure that the screen buffer is ready and optionally
    performs a randomly determined number of null action to randomize
    the initial game state."""
    self.ale.reset_game()
    self.start_lives = self.ale.lives()
    self.state_buffer = deque() # clear the state buffer
    if self.max_start_nullops > 0:
        random_actions = self.rng.randint(0, self.max_start_nullops+1)
        for _ in range(random_actions):
            self._act(0) # Null action

    # Make sure the screen buffer and state buffer is filled at the beginning of
    # each episode...
    for i in range(self.agent_history_length-1):
        self._act(0) # Null action
        self.state_buffer.append(self.get_preprocessed_frame())

  def get_initial_state(self):
    """ Inits a new episode, returns the current state (np array with agent_history_length
    most recent frames."""
    self._init_new_episode()
    return self.get_new_state()

  def _did_episode_end(self):
    """Returns a boolean indicating whether or not
    the game ended"""
    loss_of_life = self.ale.lives() < self.start_lives
    terminal = self.ale.game_over() or loss_of_life
    return terminal

  def get_new_state(self):
    """
    State is the agent_history_length recent frames presented to the agent. Mnih et al. set
    this to 4. This method creates a numpy array of size [agent_history_length, resized_height, 
    resized_width]. First, get the current preprocessed frame. Then grab the previous 
    agent_history_length-1 frames from the state_buffer (a deque) and put all agent_history_length
    into a state
    """
    # Get the current preprocessed frame
    current_frame = self.get_preprocessed_frame()
    
    # Get the most recent agent_history_length-1 frames from the self.state_buffer deque
    # Concatenate w/ current frame to get full current state: numpy array
    # of shape [agent_history_length, resized_height, resized_width]
    previous_frames = np.array(self.state_buffer)
    new_state = np.empty((self.agent_history_length, self.resized_height, self.resized_width))
    new_state[:self.agent_history_length-1, ...] = previous_frames
    new_state[self.agent_history_length-1] = current_frame

    # Pop the oldest frame, add the current frame to the queue
    self.state_buffer.popleft()
    self.state_buffer.append(current_frame)

    return new_state

  def execute(self, action):
    """Executes an action in the environment on behalf of
    the agent. 
    Returns: - The instantaneous reward
             - The resulting preprocessed game state 
             - A boolean indicating whether or 
               not the executed action ended the game."""
    reward = self._repeat_action(self.min_action_set[action])
    new_state = self.get_new_state() # Rotates the state buffer by one
    terminal = self._did_episode_end()
    return (reward, new_state, terminal)

  def get_preprocessed_frame(self):
    """ 
    See Methods->Preprocessing in Mnih et al.
    1) Get image grayscale
    2) Take the maximum value for each pixel color
       value over the frame being encoded and the previous frame
       (to avoid flickering issues)
    3) Rescale image
    """
    max_image = np.maximum(self.screen_buffer[self.index, ...],
                           self.screen_buffer[(0 if self.index == 1 else 1), ...])
    return resize(max_image, (self.resized_width, self.resized_height))

class GlobalThread(object):
  """
  Main global thread responsible for:
    - Initializing shared global network and target network parameters
    - Kicking off actor-learner threads
  """
  def __init__(self, session, graph, ale_io_lock):
    self._session = session
    self._graph = graph
    self._ale_io_lock = ale_io_lock
    self.build_graph()
    self.rng = np.random.RandomState(123456)
  
  def build_graph(self):
    """ Placeholder for the function that builds up the model 
    """
    # Input Placeholders
    state = tf.placeholder("float", [None, 84, 84, 4]) # Previous agent_history_length frames
    a = tf.placeholder("float", [None, ACTIONS]) # One hot vector representing chosen action at time t
    y = tf.placeholder("float", [None]) # Float holding y_t, the target value for chosen action at time t
    self._state=state
    self._a = a
    self._y = y

    # Set up online network and target network.
    with tf.variable_scope('network_params') as scope:
      network = build_network(state)
    with tf.variable_scope('target_network_params') as scope:
      target_network = build_network(state)
    self._network = network
    self._target_network = target_network
    
    # Set up loss
    cost = loss(network, a, y)
    self._cost = cost

    # Op for periodically updating target network with online network weights
    network_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                     "network_params")
    target_network_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                        "target_network_params")
    self._reset_target_network_params = [target_network_params[i].assign(network_params[i]) for i in range(len(target_network_params))]

    # One gradients list per thread
    for i in range(NUM_CONCURRENT):
      with tf.variable_scope('grads_'+str(i)) as scope:
        grad_vars = [tf.Variable(np.zeros(var.get_shape().as_list(), dtype=np.float32), trainable=False) for var in network_params]

    # One optimizer per thread    
    optimizers = []
    for i in range(NUM_CONCURRENT):
        optimizers.append(tf.train.AdamOptimizer(1e-4))
    self._optimizers = optimizers

    # One compute_gradients op per thread
    compute_gradients_ops = []
    for i in range(NUM_CONCURRENT):
        compute_gradients_ops.append(self._optimizers[i].compute_gradients(cost, var_list=network_params))
    self._compute_gradients_ops = compute_gradients_ops
    self._grads_and_vars = compute_gradients_ops

    # One gradient assign add (+=) op per thread
    assign_add_gradients_ops = []
    for i in range(NUM_CONCURRENT):
        namespace = 'grads_'+str(i)
        grad_vars = tf.get_collection(tf.GraphKeys.VARIABLES, namespace)
        assign_add_gradients_ops.append([tf.assign_add(v,g) for v,g in zip(grad_vars, [grad[0] for grad in self._compute_gradients_ops[i]])])
    self._assign_add_gradients_ops = assign_add_gradients_ops

    # "Async apply gradients" consists of two operations:
    # 1) Copy thread-local gradients to global thread's gradient placeholders
    # 2) Have global thread call apply_gradients on it's network_params using the
    #    gradients currently in the placeholders

    # Set up global optimizer (just to apply gradients from threads to shared network_params)
    apply_grads_optimizer = tf.train.AdamOptimizer(1e-4)
    grads_and_vars = apply_grads_optimizer.compute_gradients(cost, network_params)

    # Op for applying whatever is in the placeholder gradients
    # placeholder_gradients = []
    # for grad_var in grads_and_vars:
    #    placeholder_gradients.append((tf.placeholder('float', shape=grad_var[1].get_shape()), grad_var[1]))
    placeholder_gradients = []
    for var in network_params:
      variable = tf.Variable(np.zeros(var.get_shape().as_list(), dtype=np.float32), trainable=False)
      tensor = variable.value()
      placeholder_gradients.append((tensor, variable))
    self._placeholder_gradients = placeholder_gradients

    copy_gradients_ops = []
    for i in range(NUM_CONCURRENT):
        namespace = 'grads_'+str(i)
        grad_vars = tf.get_collection(tf.GraphKeys.VARIABLES, namespace) # list of just variables
        copy_gradients_i = [placeholder_gradients[i][1].assign(grad_vars[i]) for i in range(len(placeholder_gradients))]
        copy_gradients_ops.append(copy_gradients_i)
    self._copy_gradients_ops = copy_gradients_ops

    apply_gradients = apply_grads_optimizer.apply_gradients(placeholder_gradients)
    self.apply_gradients = apply_gradients

    # Zero out gradients
    zero_out_gradients_ops = []
    for i in range(NUM_CONCURRENT):
        namespace = 'grads_'+str(i)
        grad_vars = tf.get_collection(tf.GraphKeys.VARIABLES, namespace)
        zero_out_gradients_ops.append([tf.assign(grad,np.zeros(grad.get_shape().as_list(), dtype=np.float32)) for grad in grad_vars])
    self._zero_out_gradients_ops = zero_out_gradients_ops

    tf.initialize_all_variables().run()
    print("VARIABLES INITIALIZED")

  def _actor_learner_thread(self, i):
    """
    Main actor learner thread:
    - Initialize thread-specific environment
    - Run an infinite training loop, periodically
      sending asyncronous gradient updates to main model
    """
    # Initialize this thread's agent's environment
    environment = Environment(self._ale_io_lock)
    
    # Get initial game state
    state = environment.get_initial_state()

    # Main learning loop
    T = 0
    T_max = 100
    while T < T_max:
      # Get predictions
      Q_s_a = self._session.run(self._network, feed_dict={self._state : np.reshape(state, (1, 84, 84, 4)) })

      # Take action a according to the e-greedy policy
      # TODO: thread-specific exploration policy
      # (periodically sample epsilon from some dist.)
      epsilon = 1
      if self.rng.rand() < epsilon:
        action = self.rng.randint(0, environment.num_actions())
      else:
        action =  np.argmax(Q_s_a)

      # Execute the chosen action
      (reward, new_state, terminal) = environment.execute(action)
      reward = np.clip(reward, -1, 1)

      # Compute y using target network
      target_Q_s_a = self._session.run(self._target_network, feed_dict={self._state : np.reshape(new_state, (1, 84, 84, 4)) })
      if terminal:
        y = reward
      else:
        y = reward + GAMMA * np.max(target_Q_s_a)

      print np.max(target_Q_s_a)

      actions = np.zeros(environment.num_actions())
      actions[action]=1.0

      # loss = self._session.run(self._cost, feed_dict={self._state : np.reshape(state, (1, 84, 84, 4)), self._a: np.reshape(actions, (1, 4)), self._y: np.reshape(reward, (1,))})
      # print(loss)

      # Accumulate gradients
      # self._session.run(self._assign_add_gradients_ops[i], feed_dict={self._state : np.reshape(state, (1, 84, 84, 4)), self._a: np.reshape(actions, (1, 4)), self._y: np.reshape(1.0, (1,))})

      # Apply gradients
      if T % 5 == 0:
        # self._session.run(self._copy_gradients_ops[i])
        # self._session.run(self.apply_gradients)

        # Zero out accumulated gradients
        self._session.run(self._zero_out_gradients_ops[i])

      # Apply gradients
      # feed_dict = {}
      # for j, grad_var in enumerate(self._grads_and_vars[i]):
      #   with self._session.as_default():
      #     print type(grad_var[0].eval())
        # feed_dict[self._placeholder_gradients[i][0]] = self._session.run(self._grads_and_vars[i][0])
      # self._session.run(self.apply_gradients, feed_dict=feed_dict)

      if not terminal:
        state = new_state
      else:
        print ("Episode ended")
        # Episode ended, so next state is the next episode's initial state
        state = environment.get_initial_state()

      T += 1
      # if T % opt.target_frequency == 0:
      #   self.update_target_network()
      # if t % opt.async_update_frequency == 0:
      #   self.async_update()
      #   self.clear_gradients()

    self._session.run(self._reset_target_network_params) # Update the target network periodically
    # self._increment(self._epoch)
    print("=======================================done==============================")

  def train(self):
    """
    Creates and kicks off num_concurrent actor-learner threads
    """
    workers = []
    for thread_id in xrange(NUM_CONCURRENT):
      t = threading.Thread(target=self._actor_learner_thread, args=(thread_id,))
      t.start()
      workers.append(t)
    for t in workers:
      t.join()

  def init_ale(self):
    """
    Thread-safe way to have each actor-learner init ALE and load the game file
    """
    self._ale_io_lock.acquire()
    ale = ALEInterface()
    ale.setInt('random_seed', 123)
    ale.loadROM('/Users/coreylynch/dev/atari_roms/breakout.bin')
    self._ale_io_lock.release()
    return ale

def main(_):
  g = tf.Graph()
  with g.as_default(), tf.Session() as session:
    with tf.device("/cpu:0"):
      ale_io_lock = threading.Lock() # using a lock to avoid race condition on ALE init
      model = GlobalThread(session, g, ale_io_lock)
      model.train()


      # Just some temporary stuff to make sure target network is being copied correctly.
      # Delete later.
      network_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                       "network_params")
      target_network_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                       "target_network_params")

      # small test to ensure copy worked
      assert (False not in (network_params[2].eval() == target_network_params[2].eval()).flatten())
      print "=========COPY SUCCEEDED=========="

if __name__ == "__main__":
  tf.app.run()

import os
from absl import app, flags
from collections import defaultdict
from functools import partial
import numpy as np
import jax
import tqdm
import torch
import gym

import jaxrl_m.learners.sac as learner
import jaxrl_m.envs
from jaxrl_m.wandb import setup_wandb, default_wandb_config, get_flag_dict
import wandb
from jaxrl_m.evaluation import supply_rng, evaluate, flatten, EpisodeMonitor
from jaxrl_m.dataset import ReplayBuffer

from ml_collections import config_flags
import pickle
from flax.training import checkpoints

import d4rl

FLAGS = flags.FLAGS
flags.DEFINE_string("env_name", "HalfCheetah-v2", "Environment name.")

flags.DEFINE_string("save_dir", None, "Logging dir.")

flags.DEFINE_integer("seed", np.random.choice(1000000), "Random seed.")
flags.DEFINE_integer("eval_episodes", 10, "Number of episodes used for evaluation.")
flags.DEFINE_integer("log_interval", 1000, "Logging interval.")
flags.DEFINE_integer("eval_interval", 10000, "Eval interval.")
flags.DEFINE_integer("save_interval", 25000, "Eval interval.")
flags.DEFINE_integer("batch_size", 256, "Mini batch size.")
flags.DEFINE_integer("max_steps", int(1e6), "Number of training steps.")
flags.DEFINE_integer("start_steps", int(1e4), "Number of initial exploration steps.")
flags.DEFINE_bool("save_video", False, "Save video of evaluation.")
flags.DEFINE_string("model_type", "MLP", "Path to reward model.")
flags.DEFINE_string("ckpt", "", "Path to reward model.")

wandb_config = default_wandb_config()
wandb_config.update(
    {
        "project": os.environ.get("WANDB_PROJECT", "sac_test"),
        "group": os.environ.get("WANDB_GROUP", "sac"),
        "name": "sac_{env_name}",
    }
)

config_flags.DEFINE_config_dict("wandb", wandb_config, lock_config=False)
config_flags.DEFINE_config_dict(
    "config", learner.get_default_config(), lock_config=False
)


def get_reward_model(model_type, ckpt):
    if os.path.isdir(ckpt):
        models = [f for f in os.listdir(ckpt) if os.path.isfile(os.path.join(ckpt, f)) and f.startswith('model_')]
        max_epoch = -1
        max_epoch_model = None
        
        for model in models:
            try:
                epoch = int(model.split('_')[1].split('.')[0])
                if epoch > max_epoch:
                    max_epoch = epoch
                    max_epoch_model = model
            except ValueError:
                pass
        ckpt = os.path.join(ckpt, max_epoch_model)

    print(f"Loading {model_type} reward model")
    reward_model = torch.load(ckpt)
    return reward_model


def relabel_trajectory(trajectory, reward_model, model_type):
    trajectory = (
        torch.from_numpy(trajectory).float().to(next(reward_model.parameters()).device)
    )
    with torch.no_grad():
        if model_type == "MLP":
            rewards = reward_model.get_reward(trajectory[:, 0:2])
        elif model_type == "Categorical" or model_type == "MeanVar":
            rewards = reward_model.sample_reward(trajectory)
        else:
            z = reward_model.sample_prior(size=1).repeat(trajectory.shape[0], 1)
            trajectory = torch.cat([trajectory, z], dim=-1)
            rewards = reward_model.get_reward(trajectory)
    return rewards.cpu().numpy()

def main(_):
    # Create wandb logger
    setup_wandb(FLAGS.config.to_dict(), **FLAGS.wandb)

    if FLAGS.save_dir is not None:
        FLAGS.save_dir = os.path.join(
            FLAGS.save_dir,
            wandb.run.project,
            wandb.config.exp_prefix,
            wandb.config.experiment_id,
        )
        os.makedirs(FLAGS.save_dir, exist_ok=True)
        print(f"Saving config to {FLAGS.save_dir}/config.pkl")
        with open(os.path.join(FLAGS.save_dir, "config.pkl"), "wb") as f:
            pickle.dump(get_flag_dict(), f)

    env = EpisodeMonitor(gym.make(FLAGS.env_name))
    eval_env = EpisodeMonitor(gym.make(FLAGS.env_name))

    example_transition = dict(
        observations=env.observation_space.sample(),
        actions=env.action_space.sample(),
        rewards=0.0,
        masks=1.0,
        next_observations=env.observation_space.sample(),
    )

    replay_buffer = ReplayBuffer.create(example_transition, size=int(1e6))

    agent = learner.create_learner(
        FLAGS.seed,
        example_transition["observations"][None],
        example_transition["actions"][None],
        max_steps=FLAGS.max_steps,
        **FLAGS.config,
    )

    reward_model = get_reward_model(FLAGS.model_type, FLAGS.ckpt)

    exploration_metrics = dict()
    obs = env.reset()
    exploration_rng = jax.random.PRNGKey(0)

    trajectory = defaultdict(list)
    for i in tqdm.tqdm(
        range(1, FLAGS.max_steps + 1), smoothing=0.1, dynamic_ncols=True
    ):
        if i < FLAGS.start_steps:
            action = env.action_space.sample()
        else:
            exploration_rng, key = jax.random.split(exploration_rng)
            action = agent.sample_actions(obs, seed=key)

        next_obs, reward, done, info = env.step(action)
        mask = float(not done or "TimeLimit.truncated" in info)

        trajectory['observations'].append(obs)
        trajectory['actions'].append(action)
        trajectory['rewards'].append(reward)
        trajectory['masks'].append(mask)
        trajectory['next_observations'].append(next_obs)
        obs = next_obs

        if done:
            exploration_metrics = {
                f"exploration/{k}": v for k, v in flatten(info).items()
            }
            observations = np.array(trajectory['observations'])
            trajectory['rewards'] = relabel_trajectory(observations, reward_model, FLAGS.model_type)
            for i in range(len(trajectory['observations'])):
                replay_buffer.add_transition(
                    dict(
                        observations=trajectory['observations'][i],
                        actions=trajectory['actions'][i],
                        rewards=trajectory['rewards'][i],
                        masks=trajectory['masks'][i],
                        next_observations=trajectory['next_observations'][i],
                    )
                )
            
            obs = env.reset()
            trajectory = defaultdict(list)

        if replay_buffer.size < FLAGS.start_steps:
            continue

        batch = replay_buffer.sample(FLAGS.batch_size)
        agent, update_info = agent.update(batch)

        if i % FLAGS.log_interval == 0:
            train_metrics = {f"training/{k}": v for k, v in update_info.items()}
            wandb.log(train_metrics, step=i)
            wandb.log(exploration_metrics, step=i)
            exploration_metrics = dict()

        if i % FLAGS.eval_interval == 0:
            policy_fn = partial(supply_rng(agent.sample_actions), temperature=0.0)
            eval_info = evaluate(
                policy_fn,
                eval_env,
                num_episodes=FLAGS.eval_episodes,
                save_video=FLAGS.save_video,
            )

            eval_metrics = {f"evaluation/{k}": v for k, v in eval_info.items()}
            wandb.log(eval_metrics, step=i)

        if i % FLAGS.save_interval == 0 and FLAGS.save_dir is not None:
            checkpoints.save_checkpoint(FLAGS.save_dir, agent, i)


if __name__ == "__main__":
    app.run(main)
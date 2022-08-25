import os
import rospy
from typing import Callable, Dict, List, Optional, Tuple, Type, Union

import gym
import rospkg
import torch as th
import yaml
import json

from torch import nn
from stable_baselines3.common.policies import ActorCriticPolicy


from .agent_factory import AgentFactory
from ..utils.utils import get_observation_space
from .custom_policy_utils.utils import readJson
from .custom_policy_utils.utils import createBodyNetwork


__all__ = ["CUSTOM"]


""" 
_RS: Robot state size - placeholder for robot related inputs to the NN
_L: Number of laser beams - placeholder for the laser beam data 
"""
_L, _RS = get_observation_space()


class CUSTOM_NETWORK(nn.Module):
    """
    Custom Multilayer Perceptron for policy and value function.

    :param path: path to json file containing neural network
    :param feature_dim: dimension of the features extracted with the features_extractor (e.g. features from a CNN)
    :param last_layer_dim_pi: (int) number of units for the last layer of the policy network
    :param last_layer_dim_vf: (int) number of units for the last layer of the value network
    """

    def __init__(
        self,
        path: str,
        feature_dim: int,
        last_layer_dim_pi: int = 32,
        last_layer_dim_vf: int = 32,
        
    ):
        super(CUSTOM_NETWORK, self).__init__()

        # Read file
        data = readJson(path)

        # Create the network based on JSON
        self.body_net = createBodyNetwork(data)

        # Save output dimensions, used to create the distributions
        self.latent_dim_pi = last_layer_dim_pi
        self.latent_dim_vf = last_layer_dim_vf

        # Policy network
        self.policy_net = nn.Sequential()

        # Value network
        self.value_net = nn.Sequential()

    def forward(self, features: th.Tensor) -> Tuple[th.Tensor, th.Tensor]:
        """
        :return: (th.Tensor, th.Tensor) latent_policy, latent_value of the specified network.
            If all layers are shared, then ``latent_policy == latent_value``
        """
        body_x = self.body_net(features)
        return self.policy_net(body_x), self.value_net(body_x)


@AgentFactory.register("CUSTOM")
class CUSTOM(ActorCriticPolicy):
    """
    Policy using the custom Multilayer Perceptron.
    """

    def __init__(
        self,
        observation_space: gym.spaces.Space,
        action_space: gym.spaces.Space,
        lr_schedule: Callable[[float], float],
        net_arch: Optional[List[Union[int, Dict[str, List[int]]]]] = None,
        activation_fn: Type[nn.Module] = nn.ReLU,
        *args,
        **kwargs,
        
    ):
        # Getting the path to NN with rosparams
        self.path=rospy.get_param("/custom_network_path")

        super(CUSTOM, self).__init__(
            observation_space,
            action_space,
            lr_schedule,
            net_arch,
            activation_fn,
            *args,
            **kwargs,
        )
        # Enable orthogonal initialization
        self.ortho_init = True
        
    def _build_mlp_extractor(self) -> None:
        self.mlp_extractor = CUSTOM_NETWORK(self.path, 64)

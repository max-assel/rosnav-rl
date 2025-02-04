"""
This file contains the definition of a custom CNN model `MID_FUSION_BOTTLENECK_EXTRACTOR_1` 
for feature extraction in a reinforcement learning environment. 

Source:
    https://ieeexplore.ieee.org/document/10089196

Details:
    - The model is implemented using PyTorch and inherits from the BaseFeaturesExtractor class.
    - It includes a Bottleneck class that implements a variant of the ResNet architecture known as 
        ResNet V1.5, designed to improve accuracy for image recognition tasks.
    - The MID_FUSION_BOTTLENECK_EXTRACTOR_1 class defines a custom feature extractor that is part of 
        a middle-fusion-network.
    - The feature extractor takes input observations and performs a series of convolutional and 
        batch normalization operations, followed by fusion and goal networks to extract features.
"""

from copy import deepcopy
from typing import List, Callable, Tuple

import gymnasium as gym
from gymnasium.spaces.box import Box
from torch.nn.modules import BatchNorm2d, Module
import rospy
import torch
import torch.nn as nn
from rosnav.utils.observation_space.observation_space_manager import (
    ObservationSpaceManager,
)
import rosnav.utils.observation_space as SPACE

from ..base_extractor import RosnavBaseExtractor, TensorDict
from .bottleneck import Bottleneck
from .utils import conv1x1, conv3x3

__all__ = [
    "RESNET_MID_FUSION_EXTRACTOR_1",
    "RESNET_MID_FUSION_EXTRACTOR_2",
    "RESNET_MID_FUSION_EXTRACTOR_3",
    "RESNET_MID_FUSION_EXTRACTOR_4",
]


class RESNET_MID_FUSION_EXTRACTOR_1(RosnavBaseExtractor):
    """
    Feature extractor class that implements a mid-fusion ResNet-based architecture.

    Args:
        observation_space (gym.spaces.Box): The observation space of the environment
        observation_space_manager (ObservationSpaceManager): The observation space manager
        features_dim (int, optional): The dimensionality of the output features. Defaults to 256.
        stack_size (bool, optional): Whether the observations are stacked. Defaults to False.
        block (nn.Module, optional): The block type to use in the ResNet architecture. Defaults to Bottleneck.
        layers (list, optional): The number of layers in each block of the ResNet architecture. Defaults to [2, 1, 1].
        zero_init_residual (bool, optional): Whether to zero-initialize the last batch normalization in each residual branch. Defaults to True.
        groups (int, optional): The number of groups to use in the ResNet architecture. Defaults to 1.
        width_per_group (int, optional): The width of each group in the ResNet architecture. Defaults to 64.
        replace_stride_with_dilation (List[bool], optional): Whether to replace stride with dilation in each block of the ResNet architecture. Defaults to None.
        norm_layer (nn.Module, optional): The normalization layer to use in the ResNet architecture. Defaults to nn.BatchNorm2d.
    """

    REQUIRED_OBSERVATIONS = [
        SPACE.StackedLaserMapSpace,
        SPACE.PedestrianVelXSpace,
        SPACE.PedestrianVelYSpace,
        SPACE.DistAngleToSubgoalSpace,
    ]

    def __init__(
        self,
        observation_space: gym.spaces.Box,
        observation_space_manager: ObservationSpaceManager,
        features_dim: int = 256,
        stack_size: int = 1,
        block: nn.Module = Bottleneck,
        layers: list = [2, 1, 1],
        zero_init_residual: bool = True,
        groups: int = 1,
        width_per_group: int = 64,
        replace_stride_with_dilation: List[bool] = None,
        norm_layer: nn.Module = nn.BatchNorm2d,
        *arg,
        **kwargs,
    ):
        self._block = block
        self._groups = groups
        self._layers = layers
        self._width_per_group = width_per_group
        self._replace_stride_with_dilation = replace_stride_with_dilation
        self._norm_layer = norm_layer
        self._zero_init_residual = zero_init_residual

        self._observation_space_manager = observation_space_manager

        self._num_pedestrian_feature_maps = self._get_num_pedestrian_feature_maps()
        self._get_input_sizes()

        super(RESNET_MID_FUSION_EXTRACTOR_1, self).__init__(
            observation_space=observation_space,
            observation_space_manager=observation_space_manager,
            features_dim=features_dim,
            stack_size=stack_size,
        )

        self._init_layer_weights()

    def _get_num_pedestrian_feature_maps(self):
        num_pedestrian_feature_maps = 0
        for space in self._observation_space_manager:
            if "PEDESTRIAN" in space.name:
                num_pedestrian_feature_maps += 1

        return num_pedestrian_feature_maps

    @property
    def num_pedestrian_feature_maps(self):
        """
        Returns the number of pedestrian feature maps.

        Returns:
            int: Number of pedestrian feature maps
        """
        return self._num_pedestrian_feature_maps

    def _get_input_sizes(self):
        """
        Calculate the input sizes for the feature extraction process.

        This method calculates the input sizes required for the feature extraction process
        based on the observation space manager. It sets the values for the feature map size,
        scan map size, pedestrian map size, and goal size.

        Returns:
            None
        """
        self._feature_map_size = self._observation_space_manager[
            SPACE.StackedLaserMapSpace
        ].feature_map_size
        self._scan_map_size = self._observation_space_manager[
            SPACE.StackedLaserMapSpace
        ].shape[-1]

        self._goal_size = 2

        self._last_action_size = 0
        if SPACE.LastActionSpace in self._observation_space_manager:
            self._last_action_size = self._observation_space_manager[
                SPACE.LastActionSpace
            ].shape[-1]

        self._ped_map_size = 0
        for obs in self._observation_space_manager:
            if "PEDESTRIAN" in obs.name:
                self._ped_map_size += self._observation_space_manager[obs].shape[-1]

    def _setup_network(self, inplanes: int = 64):
        """
        Sets up the network architecture for feature extraction.
        """
        ################## ped_pos net model: ###################
        if self._norm_layer is None:
            self._norm_layer = nn.BatchNorm2d

        self.inplanes = inplanes
        self.dilation = 1
        if self._replace_stride_with_dilation is None:
            # each element in the tuple indicates if we should replace
            # the 2x2 stride with a dilated convolution instead
            self._replace_stride_with_dilation = [False] * len(self._layers)
        if len(self._replace_stride_with_dilation) != len(self._layers):
            raise ValueError(
                "replace_stride_with_dilation should be None "
                f"or a {len(self._layers)}-element tuple, got {self._replace_stride_with_dilation}"
            )
        self.base_width = self._width_per_group
        self.conv1 = nn.Conv2d(
            self.num_pedestrian_feature_maps + 1,
            self.inplanes,
            kernel_size=3,
            stride=1,
            padding=1,
            bias=False,
        )
        self.bn1 = self._norm_layer(self.inplanes)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=1, padding=1)
        self.layer1 = self._make_layer(self._block, 64, self._layers[0])
        self.layer2 = self._make_layer(
            self._block,
            128,
            self._layers[1],
            stride=2,
            dilate=self._replace_stride_with_dilation[0],
        )
        self.layer3 = self._make_layer(
            self._block,
            256,
            self._layers[2],
            stride=2,
            dilate=self._replace_stride_with_dilation[1],
        )

        self.conv2_2 = nn.Sequential(
            nn.Conv2d(
                in_channels=256,
                out_channels=128,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=128,
                out_channels=128,
                kernel_size=(3, 3),
                stride=(1, 1),
                padding=(1, 1),
            ),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=128,
                out_channels=256,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(256),
        )
        self.downsample2 = nn.Sequential(
            nn.Conv2d(
                in_channels=128,
                out_channels=256,
                kernel_size=(1, 1),
                stride=(2, 2),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(256),
        )
        self.relu2 = nn.ReLU(inplace=True)

        self.conv3_2 = nn.Sequential(
            nn.Conv2d(
                in_channels=512,
                out_channels=256,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=256,
                out_channels=256,
                kernel_size=(3, 3),
                stride=(1, 1),
                padding=(1, 1),
            ),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=256,
                out_channels=512,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(512),
        )
        self.downsample3 = nn.Sequential(
            nn.Conv2d(
                in_channels=64,
                out_channels=512,
                kernel_size=(1, 1),
                stride=(4, 4),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(512),
        )
        self.relu3 = nn.ReLU(inplace=True)

        # self.layer4 = self._make_layer(block, 512, layers[3], stride=2,
        #                               dilate=replace_stride_with_dilation[2])
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))

        _robot_state = self._goal_size + self._last_action_size
        self.linear_fc = nn.Sequential(
            nn.Linear(
                256 * self._block.expansion + _robot_state,
                self._features_dim,
            ),
            # nn.BatchNorm1d(features_dim),
            nn.ReLU(),
        )

    def _init_layer_weights(self):
        """
        Initialize the layers of the ResNet model.

        This function initializes the convolutional layers, batch normalization layers, and linear layers
        of the ResNet model. It uses different initialization methods for different types of layers.

        Returns:
            None
        """
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm1d):  # add by xzt
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                nn.init.xavier_normal_(m.weight)

        # Zero-initialize the last BN in each residual branch,
        # so that the residual branch starts with zeros, and each residual block behaves like an identity.
        # This improves the model by 0.2~0.3% according to https://arxiv.org/abs/1706.02677
        if self._zero_init_residual:
            for m in self.modules():
                if isinstance(m, Bottleneck):
                    nn.init.constant_(m.bn3.weight, 0)

    def _make_layer(
        self,
        block: Bottleneck,
        planes: int,
        blocks: int,
        stride: int = 1,
        dilate: bool = False,
    ):
        """
        Constructs a layer using the specified block type and parameters.

        Args:
            block (Bottleneck): Type of block to use
            planes (int): Number of output channels
            blocks (int): The number of block layers
            stride (int, optional): Stride for the layer. Defaults to 1.
            dilate (bool, optional): Whether to apply dilation. Defaults to False.

        Returns:
            nn.Sequential: A sequential layer constructed using the specified parameters
        """
        norm_layer = self._norm_layer
        downsample = None
        previous_dilation = self.dilation
        if dilate:
            self.dilation *= stride
            stride = 1
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                conv1x1(self.inplanes, planes * block.expansion, stride),
                norm_layer(planes * block.expansion),
            )

        layers = []
        layers.append(
            block(
                self.inplanes,
                planes,
                stride,
                downsample,
                self._groups,
                self.base_width,
                previous_dilation,
                norm_layer,
            )
        )
        self.inplanes = planes * block.expansion
        for _ in range(1, blocks):
            layers.append(
                block(
                    self.inplanes,
                    planes,
                    groups=self._groups,
                    base_width=self.base_width,
                    dilation=self.dilation,
                    norm_layer=norm_layer,
                )
            )

        return nn.Sequential(*layers)

    def _forward_impl(
        self,
        ped_map: torch.Tensor,
        scan: torch.Tensor,
        goal: torch.Tensor,
        last_action: torch.Tensor = None,
    ) -> torch.Tensor:
        """
        Implements the forward pass for the feature extractor.

        Args:
            ped_map (torch.Tensor): Pedestrian position tensor
            scan (torch.Tensor): Scan tensor
            goal (torch.Tensor): Goal tensor

        Returns:
            torch.Tensor: Output tensor after forward pass
        """
        ###### Start of fusion net ######
        fusion_in = torch.cat((scan, ped_map), dim=1)

        # See note [TorchScript super()]
        # extra layer conv, bn, relu

        x = self.conv1(fusion_in)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        identity3 = self.downsample3(x)

        x = self.layer1(x)

        identity2 = self.downsample2(x)

        x = self.layer2(x)

        x = self.conv2_2(x)
        x += identity2
        x = self.relu2(x)

        x = self.layer3(x)
        # x = self.layer4(x)

        x = self.conv3_2(x)
        x += identity3
        x = self.relu3(x)

        x = self.avgpool(x)
        fusion_out = x.squeeze(-1).squeeze(-1)
        ###### End of fusion net ######

        ###### Start of goal net #######
        # goal_in = goal.reshape(-1, 2)
        # goal_out = goal
        ###### End of goal net #######
        # Combine
        combination = (
            (fusion_out, goal)
            if self._last_action_size == 0
            else (fusion_out, goal, last_action)
        )
        fc_in = torch.cat(combination, dim=1)
        x = self.linear_fc(fc_in)

        return x

    def _get_input(
        self, observations: TensorDict
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        laser_map = observations[SPACE.StackedLaserMapSpace.name].unsqueeze(
            1
        )  # (num_envs, 1, 80, 80)

        goal_key = (
            SPACE.DistAngleToSubgoalSpace.name
            if SPACE.DistAngleToSubgoalSpace.name in observations
            else SPACE.SubgoalInRobotFrameSpace.name
        )
        dist_angle_to_goal = observations[goal_key].squeeze(1)  # (num_envs, 2)

        ped_map = None
        if self.num_pedestrian_feature_maps > 0:
            ped_map = torch.stack(
                [
                    observations[space.name]
                    for space in self._observation_space_manager.space_list
                    if "PEDESTRIAN" in space.name
                ],
                dim=1,
            )  # (num_envs, num_semantic_layers, 80, 80)

        if SPACE.LastActionSpace in self._observation_space_manager:
            last_action = observations[SPACE.LastActionSpace.name].squeeze(
                1
            )  # (num_envs, 3)
            return {
                "ped_map": ped_map,
                "scan": laser_map,
                "goal": dist_angle_to_goal,
                "last_action": last_action,
            }
        return {"ped_map": ped_map, "scan": laser_map, "goal": dist_angle_to_goal}

    def forward(self, observations: TensorDict) -> torch.Tensor:
        """
        Forward pass of the ResNet model.

        Args:
            observations (torch.Tensor): Input observations.

        Returns:
            torch.Tensor: Output tensor after the forward pass.
        """
        return self._forward_impl(**self._get_input(observations))


class DRL_VO_NAV_EXTRACTOR(RESNET_MID_FUSION_EXTRACTOR_1):
    """
    Feature extractor class that implements a mid-fusion ResNet-based architecture.

    Args:
        observation_space (gym.spaces.Box): The observation space of the environment
        observation_space_manager (ObservationSpaceManager): The observation space manager
        features_dim (int, optional): The dimensionality of the output features. Defaults to 256.
        stack_size (bool, optional): Whether the observations are stacked. Defaults to False.
        block (nn.Module, optional): The block type to use in the ResNet architecture. Defaults to Bottleneck.
        layers (list, optional): The number of layers in each block of the ResNet architecture. Defaults to [2, 1, 1].
        zero_init_residual (bool, optional): Whether to zero-initialize the last batch normalization in each residual branch. Defaults to True.
        groups (int, optional): The number of groups to use in the ResNet architecture. Defaults to 1.
        width_per_group (int, optional): The width of each group in the ResNet architecture. Defaults to 64.
        replace_stride_with_dilation (List[bool], optional): Whether to replace stride with dilation in each block of the ResNet architecture. Defaults to None.
        norm_layer (nn.Module, optional): The normalization layer to use in the ResNet architecture. Defaults to nn.BatchNorm2d.
    """

    REQUIRED_OBSERVATIONS = [
        SPACE.StackedLaserMapSpace,
        SPACE.PedestrianVelXSpace,
        SPACE.PedestrianVelYSpace,
        SPACE.PedestrianTypeSpace,
        SPACE.PedestrianSocialStateSpace,
        SPACE.DistAngleToSubgoalSpace,
        SPACE.LastActionSpace,
    ]

    def _setup_network(self, inplanes: int = 64):
        super()._setup_network(inplanes)
        self.linear_fc = nn.Sequential(
            nn.Linear(
                256 * self._block.expansion + self._goal_size + self._last_action_size,
                self._features_dim,
            ),
            nn.BatchNorm1d(self._features_dim),
            nn.ReLU(),
        )


class RESNET_MID_FUSION_EXTRACTOR_2(RESNET_MID_FUSION_EXTRACTOR_1):
    """
    Feature extractor class that implements a mid-fusion ResNet-based architecture.

    Args:
        observation_space (gym.spaces.Box): The observation space of the environment
        observation_space_manager (ObservationSpaceManager): The observation space manager
        features_dim (int, optional): The dimensionality of the output features. Defaults to 256.
        stack_size (bool, optional): Whether the observations are stacked. Defaults to False.
        block (nn.Module, optional): The block type to use in the ResNet architecture. Defaults to Bottleneck.
        layers (list, optional): The number of layers in each block of the ResNet architecture. Defaults to [2, 1, 1].
        zero_init_residual (bool, optional): Whether to zero-initialize the last batch normalization in each residual branch. Defaults to True.
        groups (int, optional): The number of groups to use in the ResNet architecture. Defaults to 1.
        width_per_group (int, optional): The width of each group in the ResNet architecture. Defaults to 64.
        replace_stride_with_dilation (List[bool], optional): Whether to replace stride with dilation in each block of the ResNet architecture. Defaults to None.
        norm_layer (nn.Module, optional): The normalization layer to use in the ResNet architecture. Defaults to nn.BatchNorm2d.
    """

    REQUIRED_OBSERVATIONS = [
        SPACE.StackedLaserMapSpace,
        SPACE.PedestrianVelXSpace,
        SPACE.PedestrianVelYSpace,
        SPACE.PedestrianTypeSpace,
        SPACE.PedestrianSocialStateSpace,
        SPACE.DistAngleToSubgoalSpace,
    ]

    def _setup_network(self, inplanes: int = 64):
        """
        Sets up the network architecture for feature extraction.
        """
        ################## ped_pos net model: ###################
        if len(self._layers) < 4:
            self._layers.append(1)

        if self._norm_layer is None:
            self._norm_layer = nn.BatchNorm2d

        self.inplanes = inplanes
        self.dilation = 1
        if self._replace_stride_with_dilation is None:
            # each element in the tuple indicates if we should replace
            # the 2x2 stride with a dilated convolution instead
            self._replace_stride_with_dilation = [False] * len(self._layers)
        if len(self._replace_stride_with_dilation) != len(self._layers):
            raise ValueError(
                "replace_stride_with_dilation should be None "
                f"or a {len(self._layers)}-element tuple, got {self._replace_stride_with_dilation}"
            )
        self.base_width = self._width_per_group

        # pre conv1
        self.conv1_1 = nn.Conv2d(
            self.num_pedestrian_feature_maps + 1,
            self.inplanes,
            kernel_size=3,
            stride=1,
            padding=1,
            bias=False,
        )
        self.bn1_1 = self._norm_layer(self.inplanes)
        self.relu1_1 = nn.ReLU(inplace=True)

        self.conv1 = nn.Conv2d(
            self.inplanes, self.inplanes, kernel_size=3, stride=1, padding=1, bias=False
        )
        self.bn1 = self._norm_layer(self.inplanes)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=1, padding=1)
        self.layer1 = self._make_layer(self._block, 64, self._layers[0])
        self.layer2 = self._make_layer(
            self._block,
            128,
            self._layers[1],
            stride=2,
            dilate=self._replace_stride_with_dilation[0],
        )
        self.layer3 = self._make_layer(
            self._block,
            256,
            self._layers[2],
            stride=2,
            dilate=self._replace_stride_with_dilation[1],
        )

        self.conv2_2 = nn.Sequential(
            nn.Conv2d(
                in_channels=256,
                out_channels=128,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=128,
                out_channels=128,
                kernel_size=(3, 3),
                stride=(1, 1),
                padding=(1, 1),
            ),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=128,
                out_channels=256,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(256),
        )
        self.downsample2 = nn.Sequential(
            nn.Conv2d(
                in_channels=128,
                out_channels=256,
                kernel_size=(1, 1),
                stride=(2, 2),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(256),
        )
        self.relu2 = nn.ReLU(inplace=True)

        self.conv3_2 = nn.Sequential(
            nn.Conv2d(
                in_channels=512,
                out_channels=256,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=256,
                out_channels=256,
                kernel_size=(3, 3),
                stride=(1, 1),
                padding=(1, 1),
            ),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=256,
                out_channels=512,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(512),
        )
        self.downsample3 = nn.Sequential(
            nn.Conv2d(
                in_channels=64,
                out_channels=512,
                kernel_size=(1, 1),
                stride=(4, 4),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(512),
        )
        self.relu3 = nn.ReLU(inplace=True)

        # self.layer4 = self._make_layer(block, 512, layers[3], stride=2,
        #                               dilate=replace_stride_with_dilation[2])

        # extra block at the end
        self.layer4 = self._make_layer(
            self._block,
            256,
            self._layers[3],
            stride=1,
            dilate=self._replace_stride_with_dilation[2],
        )

        self.conv4_2 = deepcopy(self.conv3_2)
        self.downsample4 = nn.Sequential(
            nn.Conv2d(
                in_channels=256,
                out_channels=512,
                kernel_size=(1, 1),
                stride=(2, 2),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(512),
        )
        self.relu4 = nn.ReLU(inplace=True)

        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))

        _robot_state = self._goal_size + self._last_action_size
        self.linear_fc = nn.Sequential(
            nn.Linear(
                256 * self._block.expansion + _robot_state,
                self._features_dim,
            ),
            # nn.BatchNorm1d(features_dim),
            nn.ReLU(),
        )

    def _forward_impl(
        self,
        ped_map: torch.Tensor,
        scan: torch.Tensor,
        goal: torch.Tensor,
        last_action: torch.Tensor = None,
    ) -> torch.Tensor:
        """
        Implements the forward pass for the feature extractor.

        Args:
            ped_pos (torch.Tensor): Pedestrian position tensor
            scan (torch.Tensor): Scan tensor
            goal (torch.Tensor): Goal tensor

        Returns:
            torch.Tensor: Output tensor after forward pass
        """
        ###### Start of fusion net ######
        fusion_in = torch.cat((scan, ped_map), dim=1)

        # See note [TorchScript super()]
        # extra layer conv, bn, relu
        x = self.conv1_1(fusion_in)
        x = self.bn1_1(x)
        x = self.relu1_1(x)

        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        identity3 = self.downsample3(x)  # in: 64, out: 512

        x = self.layer1(x)  # in: 64, out: 128

        identity2 = self.downsample2(x)  # in: 128, out: 256

        x = self.layer2(x)  # in: 256, out: 256

        identity1 = self.downsample4(x)  # in: 256, out: 512

        x = self.conv2_2(x)  # in: 256, out: 256
        x += identity2
        x = self.relu2(x)

        x = self.layer3(x)  # in: 256, out: 512

        x = self.conv3_2(x)  # in: 512, out: 512
        x += identity1  # 512
        x = self.relu3(x)

        x = self.layer4(x)

        x = self.conv4_2(x)  # in: 512, out: 512
        x += identity3  # 512
        x = self.relu4(x)

        x = self.avgpool(x)
        fusion_out = x.squeeze(-1).squeeze(-1)
        ###### End of fusion net ######

        ###### Start of goal net #######
        # goal_in = goal.reshape(-1, 2)
        # goal_out = goal
        ###### End of goal net #######
        # Combine
        combination = (
            (fusion_out, goal)
            if self._last_action_size == 0
            else (fusion_out, goal, last_action)
        )
        fc_in = torch.cat(combination, dim=1)
        x = self.linear_fc(fc_in)

        return x


class RESNET_MID_FUSION_EXTRACTOR_3(RESNET_MID_FUSION_EXTRACTOR_2):
    """
    Feature extractor class that implements a mid-fusion ResNet-based architecture.

    Args:
        observation_space (gym.spaces.Box): The observation space of the environment
        observation_space_manager (ObservationSpaceManager): The observation space manager
        features_dim (int, optional): The dimensionality of the output features. Defaults to 256.
        stack_size (bool, optional): Whether the observations are stacked. Defaults to False.
        block (nn.Module, optional): The block type to use in the ResNet architecture. Defaults to Bottleneck.
        layers (list, optional): The number of layers in each block of the ResNet architecture. Defaults to [2, 1, 1].
        zero_init_residual (bool, optional): Whether to zero-initialize the last batch normalization in each residual branch. Defaults to True.
        groups (int, optional): The number of groups to use in the ResNet architecture. Defaults to 1.
        width_per_group (int, optional): The width of each group in the ResNet architecture. Defaults to 64.
        replace_stride_with_dilation (List[bool], optional): Whether to replace stride with dilation in each block of the ResNet architecture. Defaults to None.
        norm_layer (nn.Module, optional): The normalization layer to use in the ResNet architecture. Defaults to nn.BatchNorm2d.
    """

    REQUIRED_OBSERVATIONS = [
        SPACE.StackedLaserMapSpace,
        SPACE.PedestrianVelXSpace,
        SPACE.PedestrianVelYSpace,
        SPACE.PedestrianTypeSpace,
        SPACE.PedestrianSocialStateSpace,
        SPACE.DistAngleToSubgoalSpace,
        SPACE.LastActionSpace,
    ]


class RESNET_MID_FUSION_EXTRACTOR_4(RESNET_MID_FUSION_EXTRACTOR_1):
    REQUIRED_OBSERVATIONS = [
        SPACE.StackedLaserMapSpace,
        SPACE.PedestrianVelXSpace,
        SPACE.PedestrianVelYSpace,
        SPACE.PedestrianTypeSpace,
        SPACE.PedestrianSocialStateSpace,
        SPACE.DistAngleToSubgoalSpace,
    ]


class RESNET_MID_FUSION_EXTRACTOR_5(RESNET_MID_FUSION_EXTRACTOR_3):
    REQUIRED_OBSERVATIONS = [
        SPACE.StackedLaserMapSpace,
        SPACE.PedestrianVelXSpace,
        SPACE.PedestrianVelYSpace,
        SPACE.PedestrianTypeSpace,
        SPACE.PedestrianSocialStateSpace,
        SPACE.DistAngleToSubgoalSpace,
        SPACE.LastActionSpace,
    ]

    def _setup_network(self):
        if len(self._layers) < 4:
            self._layers.append(1)
        super()._setup_network()
        self.conv1 = nn.Conv2d(
            self.num_pedestrian_feature_maps + 1,
            64,
            kernel_size=3,
            stride=1,
            padding=1,
            bias=False,
        )

    def _forward_impl(
        self,
        ped_map: torch.Tensor,
        scan: torch.Tensor,
        goal: torch.Tensor,
        last_action: torch.Tensor = None,
    ) -> torch.Tensor:
        """
        Implements the forward pass for the feature extractor.

        Args:
            ped_pos (torch.Tensor): Pedestrian position tensor
            scan (torch.Tensor): Scan tensor
            goal (torch.Tensor): Goal tensor
            last_ac (torch.Tensor): Goal tensor

        Returns:
            torch.Tensor: Output tensor after forward pass
        """
        ###### Start of fusion net ######
        fusion_in = torch.cat((scan, ped_map), dim=1)

        # See note [TorchScript super()]
        # extra layer conv, bn, relu
        # x = self.conv1_1(fusion_in)
        # x = self.bn1_1(x)
        # x = self.relu1_1(x)

        x = self.conv1(fusion_in)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        identity3 = self.downsample3(x)  # in: 128, out: 512

        x = self.layer1(x)  # in: 64, out: 128

        identity2 = self.downsample2(x)  # in: 128, out: 256

        x = self.layer2(x)  # in: 256, out: 256

        identity1 = self.downsample4(x)  # in: 256, out: 512

        x = self.conv2_2(x)  # in: 256, out: 256
        x += identity2
        x = self.relu2(x)

        x = self.layer3(x)  # in: 256, out: 512

        x = self.conv3_2(x)  # in: 512, out: 512
        x += identity1  # 512
        x = self.relu3(x)

        x = self.layer4(x)

        x = self.conv4_2(x)  # in: 512, out: 512
        x += identity3  # 512
        x = self.relu4(x)

        x = self.avgpool(x)
        fusion_out = x.squeeze(-1).squeeze(-1)
        ###### End of fusion net ######

        ###### Start of goal net #######
        # goal_in = goal.reshape(-1, 2)
        # goal_out = goal

        # last_action_out = torch.flatten(last_action)
        ###### End of goal net #######
        # Combine
        combination = (
            (fusion_out, goal)
            if self._last_action_size == 0
            else (fusion_out, goal, last_action)
        )
        fc_in = torch.cat(combination, dim=1)
        x = self.linear_fc(fc_in)

        return x


class RESNET_MID_FUSION_EXTRACTOR_6(RESNET_MID_FUSION_EXTRACTOR_3):
    """
    Feature extractor class that implements a mid-fusion ResNet-based architecture.

    Args:
        observation_space (gym.spaces.Box): The observation space of the environment
        observation_space_manager (ObservationSpaceManager): The observation space manager
        features_dim (int, optional): The dimensionality of the output features. Defaults to 256.
        stack_size (bool, optional): Whether the observations are stacked. Defaults to False.
        block (nn.Module, optional): The block type to use in the ResNet architecture. Defaults to Bottleneck.
        layers (list, optional): The number of layers in each block of the ResNet architecture. Defaults to [2, 1, 1].
        zero_init_residual (bool, optional): Whether to zero-initialize the last batch normalization in each residual branch. Defaults to True.
        groups (int, optional): The number of groups to use in the ResNet architecture. Defaults to 1.
        width_per_group (int, optional): The width of each group in the ResNet architecture. Defaults to 64.
        replace_stride_with_dilation (List[bool], optional): Whether to replace stride with dilation in each block of the ResNet architecture. Defaults to None.
        norm_layer (nn.Module, optional): The normalization layer to use in the ResNet architecture. Defaults to nn.BatchNorm2d.
    """

    REQUIRED_OBSERVATIONS = [
        SPACE.StackedLaserMapSpace,
        SPACE.PedestrianVelXSpace,
        SPACE.PedestrianVelYSpace,
        SPACE.PedestrianTypeSpace,
        SPACE.PedestrianSocialStateSpace,
        SPACE.DistAngleToSubgoalSpace,
        SPACE.LastActionSpace,
    ]

    def _setup_network(self):
        inplanes = 128
        super()._setup_network(inplanes=inplanes)
        self.conv1 = nn.Conv2d(
            self.num_pedestrian_feature_maps + 1,
            inplanes,
            kernel_size=3,
            stride=1,
            padding=1,
            bias=False,
        )

    def _forward_impl(
        self,
        ped_map: torch.Tensor,
        scan: torch.Tensor,
        goal: torch.Tensor,
        last_action: torch.Tensor = None,
    ) -> torch.Tensor:
        """
        Implements the forward pass for the feature extractor.

        Args:
            ped_pos (torch.Tensor): Pedestrian position tensor
            scan (torch.Tensor): Scan tensor
            goal (torch.Tensor): Goal tensor
            last_ac (torch.Tensor): Goal tensor

        Returns:
            torch.Tensor: Output tensor after forward pass
        """
        ###### Start of fusion net ######
        fusion_in = torch.cat((scan, ped_map), dim=1)

        x = self.conv1(fusion_in)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x = self.layer1(x)  # in: 64, out: 128

        identity2 = self.downsample2(x)  # in: 128, out: 256

        x = self.layer2(x)  # in: 256, out: 256

        identity1 = self.downsample4(x)  # in: 256, out: 512

        x = self.conv2_2(x)  # in: 256, out: 256
        x += identity2
        x = self.relu2(x)

        x = self.layer3(x)  # in: 256, out: 512

        x = self.conv3_2(x)  # in: 512, out: 512
        x += identity1  # 512
        x = self.relu3(x)

        x = self.avgpool(x)
        fusion_out = x.squeeze(-1).squeeze(-1)
        ###### End of fusion net ######

        ###### Start of goal net #######
        # goal_in = goal.reshape(-1, 2)
        # goal_out = goal

        # last_action_out = torch.flatten(last_action)
        ###### End of goal net #######
        # Combine
        combination = (
            (fusion_out, goal)
            if self._last_action_size == 0
            else (fusion_out, goal, last_action)
        )
        fc_in = torch.cat(combination, dim=1)
        x = self.linear_fc(fc_in)

        return x


class DRL_VO_NAV_EXTRACTOR_TEST(DRL_VO_NAV_EXTRACTOR):
    REQUIRED_OBSERVATIONS = [
        SPACE.StackedLaserMapSpace,
        SPACE.PedestrianVelXSpace,
        SPACE.PedestrianVelYSpace,
        SPACE.PedestrianTypeSpace,
        SPACE.PedestrianSocialStateSpace,
        SPACE.DistAngleToSubgoalSpace,
        SPACE.LastActionSpace,
    ]

    def _setup_network(self, inplanes: int = 64):
        """
        Sets up the network architecture for feature extraction.
        """
        """
        Sets up the network architecture for feature extraction.
        """
        if len(self._layers) < 4:
            self._layers.append(1)

        ################## ped_pos net model: ###################
        if self._norm_layer is None:
            self._norm_layer = nn.BatchNorm2d

        self.inplanes = inplanes
        self.dilation = 1
        if self._replace_stride_with_dilation is None:
            # each element in the tuple indicates if we should replace
            # the 2x2 stride with a dilated convolution instead
            self._replace_stride_with_dilation = [False] * len(self._layers)
        if len(self._replace_stride_with_dilation) != len(self._layers):
            raise ValueError(
                "replace_stride_with_dilation should be None "
                f"or a {len(self._layers)}-element tuple, got {self._replace_stride_with_dilation}"
            )
        self.base_width = self._width_per_group
        self.conv1 = nn.Conv2d(
            self.num_pedestrian_feature_maps + 1,
            self.inplanes,
            kernel_size=3,
            stride=1,
            padding=1,
            bias=False,
        )
        self.bn1 = self._norm_layer(self.inplanes)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=1, padding=1)
        self.layer1 = self._make_layer(self._block, 64, self._layers[0])
        self.layer2 = self._make_layer(
            self._block,
            128,
            self._layers[1],
            stride=2,
            dilate=self._replace_stride_with_dilation[0],
        )
        self.layer3 = self._make_layer(
            self._block,
            256,
            self._layers[2],
            stride=2,
            dilate=self._replace_stride_with_dilation[1],
        )

        self.conv2_2 = nn.Sequential(
            nn.Conv2d(
                in_channels=256,
                out_channels=128,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=128,
                out_channels=128,
                kernel_size=(3, 3),
                stride=(1, 1),
                padding=(1, 1),
            ),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=128,
                out_channels=256,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(256),
        )
        self.downsample2 = nn.Sequential(
            nn.Conv2d(
                in_channels=128,
                out_channels=256,
                kernel_size=(1, 1),
                stride=(2, 2),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(256),
        )
        self.relu2 = nn.ReLU(inplace=True)

        self.conv3_2 = nn.Sequential(
            nn.Conv2d(
                in_channels=512,
                out_channels=256,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=256,
                out_channels=256,
                kernel_size=(3, 3),
                stride=(1, 1),
                padding=(1, 1),
            ),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=256,
                out_channels=512,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(512),
        )
        self.downsample3 = nn.Sequential(
            nn.Conv2d(
                in_channels=256,
                out_channels=512,
                kernel_size=(1, 1),
                stride=(2, 2),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(512),
        )
        self.relu3 = nn.ReLU(inplace=True)

        self.conv4_2 = nn.Sequential(
            nn.Conv2d(
                in_channels=1024,
                out_channels=512,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=512,
                out_channels=512,
                kernel_size=(3, 3),
                stride=(1, 1),
                padding=(1, 1),
            ),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=512,
                out_channels=1024,
                kernel_size=(1, 1),
                stride=(1, 1),
                padding=(0, 0),
            ),
            nn.BatchNorm2d(1024),
        )
        self.downsample4 = nn.Sequential(
            nn.Conv2d(64, 1024, kernel_size=(1, 1), stride=(8, 8), padding=(0, 0)),
            nn.BatchNorm2d(1024),
        )
        self.relu4 = nn.ReLU(inplace=True)

        self.layer4 = self._make_layer(
            self._block,
            512,
            self._layers[3],
            stride=2,
            dilate=self._replace_stride_with_dilation[2],
        )
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        _robot_state = self._goal_size + self._last_action_size
        self.linear_fc = nn.Sequential(
            nn.Linear(
                512 * self._block.expansion + _robot_state,
                self._features_dim,
            ),
            # nn.BatchNorm1d(features_dim),
            nn.ReLU(),
        )

    def _forward_impl(
        self,
        ped_map: torch.Tensor,
        scan: torch.Tensor,
        goal: torch.Tensor,
        last_action: torch.Tensor = None,
    ) -> torch.Tensor:
        """
        Implements the forward pass for the feature extractor.

        Args:
            ped_pos (torch.Tensor): Pedestrian position tensor
            scan (torch.Tensor): Scan tensor
            goal (torch.Tensor): Goal tensor
            last_action (torch.Tensor): Action tensor

        Returns:
            torch.Tensor: Output tensor after forward pass
        """
        ###### Start of fusion net ######
        fusion_in = torch.cat((scan, ped_map), dim=1)

        # See note [TorchScript super()]
        # extra layer conv, bn, relu

        x = self.conv1(fusion_in)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        identity4 = self.downsample4(x)

        x = self.layer1(x)

        identity2 = self.downsample2(x)

        x = self.layer2(x)

        identity3 = self.downsample3(x)

        x = self.conv2_2(x)
        x += identity2
        x = self.relu2(x)

        x = self.layer3(x)

        x = self.conv3_2(x)
        x += identity3
        x = self.relu3(x)

        x = self.layer4(x)

        x = self.conv4_2(x)
        x += identity4
        x = self.relu4(x)

        x = self.avgpool(x)
        fusion_out = x.squeeze(-1).squeeze(-1)
        ###### End of fusion net ######

        ###### Start of goal net #######
        # goal_in = goal.reshape(-1, 2)
        # goal_out = goal
        ###### End of goal net #######
        # Combine
        combination = (
            (fusion_out, goal)
            if self._last_action_size == 0
            else (fusion_out, goal, last_action)
        )
        fc_in = torch.cat(combination, dim=1)
        x = self.linear_fc(fc_in)

        return x


class _LaserTest(RESNET_MID_FUSION_EXTRACTOR_1):
    REQUIRED_OBSERVATIONS = [
        SPACE.StackedLaserMapSpace,
        SPACE.DistAngleToSubgoalSpace,
    ]

    def _forward_impl(
        self, scan: torch.Tensor, goal: torch.Tensor, *args, **kwargs
    ) -> torch.Tensor:
        """
        Implements the forward pass for the feature extractor.

        Args:
            ped_pos (torch.Tensor): Pedestrian position tensor
            scan (torch.Tensor): Scan tensor
            goal (torch.Tensor): Goal tensor

        Returns:
            torch.Tensor: Output tensor after forward pass
        """
        ###### Start of fusion net ######

        # See note [TorchScript super()]
        # extra layer conv, bn, relu

        x = self.conv1(scan)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        identity3 = self.downsample3(x)

        x = self.layer1(x)

        identity2 = self.downsample2(x)

        x = self.layer2(x)

        x = self.conv2_2(x)
        x += identity2
        x = self.relu2(x)

        x = self.layer3(x)
        # x = self.layer4(x)

        x = self.conv3_2(x)
        x += identity3
        x = self.relu3(x)

        x = self.avgpool(x)
        fusion_out = x.squeeze(-1).squeeze(-1)
        ###### End of fusion net ######

        ###### Start of goal net #######
        # goal_in = goal.reshape(-1, 2)
        # goal_out = goal
        ###### End of goal net #######
        # Combine
        combination = (
            (fusion_out, goal)
            if self._last_action_size == 0
            else (fusion_out, goal, kwargs[SPACE.LastActionSpace.name])
        )
        fc_in = torch.cat(combination, dim=1)
        x = self.linear_fc(fc_in)

        return x


class _LaserTest_deep(DRL_VO_NAV_EXTRACTOR_TEST):
    REQUIRED_OBSERVATIONS = [
        SPACE.StackedLaserMapSpace,
        SPACE.DistAngleToSubgoalSpace,
    ]

    def _forward_impl(
        self,
        scan: torch.Tensor,
        goal: torch.Tensor,
        *args,
        **kwargs,
    ) -> torch.Tensor:
        """
        Implements the forward pass for the feature extractor.

        Args:
            ped_pos (torch.Tensor): Pedestrian position tensor
            scan (torch.Tensor): Scan tensor
            goal (torch.Tensor): Goal tensor
            last_action (torch.Tensor): Action tensor

        Returns:
            torch.Tensor: Output tensor after forward pass
        """
        # See note [TorchScript super()]
        # extra layer conv, bn, relu

        x = self.conv1(scan)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        identity4 = self.downsample4(x)

        x = self.layer1(x)

        identity2 = self.downsample2(x)

        x = self.layer2(x)

        identity3 = self.downsample3(x)

        x = self.conv2_2(x)
        x += identity2
        x = self.relu2(x)

        x = self.layer3(x)

        x = self.conv3_2(x)
        x += identity3
        x = self.relu3(x)

        x = self.layer4(x)

        x = self.conv4_2(x)
        x += identity4
        x = self.relu4(x)

        x = self.avgpool(x)
        fusion_out = x.squeeze(-1).squeeze(-1)
        ###### End of fusion net ######

        ###### Start of goal net #######
        # goal_in = goal.reshape(-1, 2)
        # goal_out = goal
        ###### End of goal net #######
        # Combine
        combination = (
            (fusion_out, goal)
            if self._last_action_size == 0
            else (fusion_out, goal, kwargs[SPACE.LastActionSpace.name])
        )
        fc_in = torch.cat(combination, dim=1)
        x = self.linear_fc(fc_in)

        return x


class DRL_VO_DEEP(DRL_VO_NAV_EXTRACTOR_TEST):
    REQUIRED_OBSERVATIONS = [
        SPACE.StackedLaserMapSpace,
        SPACE.PedestrianVelXSpace,
        SPACE.PedestrianVelYSpace,
        SPACE.PedestrianTypeSpace,
        SPACE.PedestrianSocialStateSpace,
        SPACE.DistAngleToSubgoalSpace,
    ]

class DRL_VO_ROSNAV_EXTRACTOR(RESNET_MID_FUSION_EXTRACTOR_1):
    def _setup_network(self, inplanes: int = 64):
        super()._setup_network(inplanes)
        self.linear_fc = nn.Sequential(
            nn.Linear(
                256 * self._block.expansion + self._goal_size + self._last_action_size,
                self._features_dim,
            ),
            nn.ReLU(),
        )

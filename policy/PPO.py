from typing import Any, Mapping
from gymnasium import spaces
import torch
import torch.nn.functional as F
from ray.rllib.algorithms.ppo.ppo_rl_module import PPORLModule
from ray.rllib.core.models.specs.specs_dict import SpecDict
from ray.rllib.core.rl_module.rl_module import RLModule
from ray.rllib.core.rl_module.torch import TorchRLModule
from ray.rllib.models.torch.torch_distributions import (
    TorchCategorical,
    TorchMultiCategorical,
    TorchMultiDistribution,
)
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.utils.annotations import override
from ray.rllib.utils.nested_dict import NestedDict
from torch import Tensor, nn
from torch_geometric.nn.conv.gcn_conv import GCNConv
from torch_scatter import scatter

class BaseModel(TorchRLModule, PPORLModule):
    framework: str = "torch"

    def get_initial_state(self) -> dict:
        return {}
    
    def _forward_inference(self, batch: NestedDict) -> Mapping[str, Any]:
        with torch.no_grad():
            return self._forward_train(batch)

    def _forward_exploration(self, batch: NestedDict) -> Mapping[str, Any]:
        with torch.no_grad():
            return self._forward_train(batch)


class GraphToGraph(BaseModel):

    def setup(self):
        action_space = self.config.action_space
        self.logit_lens = [int(action_space["accept"].n), int(sum(action_space["outcome"].nvec))]
        self.num_used_agents = self.config.model_config_dict["num_used_agents"]
        self.use_opponent_encoding = self.config.model_config_dict["use_opponent_encoding"]

        self.value_nodes_mask = torch.zeros((1, len(action_space["outcome"].nvec), max(action_space["outcome"].nvec)), dtype=torch.bool)
        for i, n in enumerate(action_space["outcome"].nvec):
            self.value_nodes_mask[0, i, :n] = True

        match self.config.model_config_dict["pooling_op"]:
            case "max":
                self.pooling_op = lambda x, y, z: torch.max(x, y)[0]
            case "mean":
                self.pooling_op = lambda x, y, z: (torch.sum(x, y) / z)
            case "sum":
                self.pooling_op = lambda x, y, z: torch.sum(x, y)
            case _:
                raise ValueError(f"Pooling op {self.config.model_config_dict['pooling_op']} not supported")


        self.val_obj = nn.Linear(6, 30)  # value features + objective features
        self.obj_head = nn.Linear(32, 64)  # prev + objective features
        if self.use_opponent_encoding:
            self.head_encoder = nn.Linear(64 + self.num_used_agents, 64)
        else:
            self.head_encoder = nn.Linear(64, 64)
        self.head_obj = nn.Linear(94, 64)  # prev + output val_obj
        self.obj_val = nn.Linear(68, 1)  # prev + value features

        self.accept_head = nn.Linear(64, 2)
        self.vf = torch.nn.Linear(64, 1)


        
        child_distribution_cls_struct = {
            "accept": TorchCategorical,
            "outcome": TorchMultiCategorical.get_partial_dist_cls(space=action_space["outcome"], input_lens=list(action_space["outcome"].nvec))
        }
        self.action_dist_cls = TorchMultiDistribution.get_partial_dist_cls(
            space=action_space,
            child_distribution_cls_struct=child_distribution_cls_struct,
            input_lens=self.logit_lens,
        )

    @override(RLModule)
    def _forward_train(self, batch: NestedDict) -> Mapping[str, Any]:
        head_node: Tensor = batch["obs"]["head_node"]
        objective_nodes: Tensor = batch["obs"]["objective_nodes"]
        value_nodes: Tensor = batch["obs"]["value_nodes"]
        # value_nodes_mask: Tensor = batch["obs"]["value_nodes_mask"]
        opponent_encoding: Tensor = F.one_hot(batch["obs"]["opponent_encoding"], self.num_used_agents)
        accept_mask: Tensor = batch["obs"]["accept_mask"]

        max_num_values = value_nodes.shape[2]
        num_objectives = objective_nodes.shape[1]

        objective_nodes_expand = objective_nodes.unsqueeze(2).expand(
            -1, -1, max_num_values, -1
        )
        h1 = torch.cat([value_nodes, objective_nodes_expand], 3)
        h2 = self.val_obj(h1)
        h3 = F.relu(h2) * self.value_nodes_mask.unsqueeze(3)

        h_objective_nodes = self.pooling_op(h3, 2, objective_nodes[:, :, :1])

        head_node_expand = head_node.unsqueeze(1).expand(-1, num_objectives, -1)
        h4 = self.obj_head(torch.cat([h_objective_nodes, head_node_expand], 2))
        h5 = F.relu(h4)

        h_head = self.pooling_op(h5, 1, head_node[:, :1])
        if self.use_opponent_encoding:
            h_head = torch.cat((h_head, opponent_encoding), dim=-1)
        h_head = F.relu(self.head_encoder(h_head))

        h_head_expand = h_head.unsqueeze(1).expand(-1, num_objectives, -1)
        h6 = self.head_obj(torch.cat([h_objective_nodes, h_head_expand], 2))
        h7 = F.relu(h6)

        h8 = h7.unsqueeze(2).expand(-1, -1, max_num_values, -1)
        h9 = torch.cat([value_nodes, h8], 3)
        h10 = self.obj_val(h9)
        h11 = F.relu(h10)

        # action_logits = torch.masked_select(h11.squeeze(), value_nodes_mask[:1])
        accept_inf_mask = torch.max(torch.log(accept_mask), torch.Tensor([torch.finfo(torch.float32).min]))
        accept_action_logits = F.relu(self.accept_head(h_head)) + accept_inf_mask
        offer_action_logits = h11.squeeze(-1)[:, self.value_nodes_mask[0]]


        action_logits = torch.cat((accept_action_logits, offer_action_logits), dim=-1)

        vf_out = self.vf(h_head).squeeze(-1)


        return {"vf_preds": vf_out, "action_dist_inputs": action_logits}


class GraphToGraph2(BaseModel):

    def setup(self):
        self.num_used_agents = self.config.model_config_dict["num_used_agents"]
        self.use_opponent_encoding = self.config.model_config_dict["use_opponent_encoding"]
        self.pooling_op = self.config.model_config_dict["pooling_op"]


        match self.pooling_op:
            case "max":
                self.head_pool = lambda x: torch.max(x, 1)[0]
            case "mean":
                self.head_pool = lambda x: torch.mean(x, 1)
            case "sum":
                self.head_pool = lambda x: torch.sum(x, 1)
            case "min":
                self.head_pool = lambda x: torch.min(x, 1)[0]
            case "mul":
                self.head_pool = lambda x: torch.prod(x, 1)
            case _:
                raise ValueError(f"Pooling op {self.pooling_op} not supported")

        self.val_obj = nn.Linear(6, 30)  # value features + objective features
        self.obj_head = nn.Linear(32, 64)  # prev + objective features
        if self.use_opponent_encoding:
            self.head_encoder = nn.Linear(64 + self.num_used_agents, 64)
        else:
            self.head_encoder = nn.Linear(64, 64)
        self.head_obj = nn.Linear(94, 64)  # prev + output val_obj
        self.obj_val = nn.Linear(68, 1)  # prev + value features

        self.accept_head = nn.Linear(64, 2)
        self.vf = torch.nn.Linear(64, 1)

        # def get_action_dist_cls(self):
        action_space = self.config.action_space
        logit_lens = [int(action_space["accept"].n), int(sum(action_space["outcome"].nvec))]
        child_distribution_cls_struct = {
            "accept": TorchCategorical,
            "outcome": TorchMultiCategorical.get_partial_dist_cls(space=action_space["outcome"], input_lens=list(action_space["outcome"].nvec))
        }
        self.action_dist_cls = TorchMultiDistribution.get_partial_dist_cls(
            space=action_space,
            child_distribution_cls_struct=child_distribution_cls_struct,
            input_lens=logit_lens,
        )
        # return action_dist_cls

    def get_action_dist_cls(self):
        return self.action_dist_cls

    def get_train_action_dist_cls(self):
        return self.get_action_dist_cls()

    def get_exploration_action_dist_cls(self):
        return self.get_action_dist_cls()

    def get_inference_action_dist_cls(self):
        return self.get_action_dist_cls()

    @override(RLModule)
    def _forward_train(self, batch: NestedDict) -> Mapping[str, Any]:
        head_node: Tensor = batch["obs"]["head_node"]
        objective_nodes: Tensor = batch["obs"]["objective_nodes"]
        value_nodes: Tensor = batch["obs"]["value_nodes"]
        value_adjacency: Tensor = batch["obs"]["value_adjacency"]
        opponent_encoding: Tensor = F.one_hot(batch["obs"]["opponent_encoding"], self.num_used_agents)
        accept_mask: Tensor = batch["obs"]["accept_mask"]

        self.logit_lens = value_adjacency[0].unique(return_counts=True)[1].tolist()

        # value nodes to objective nodes
        objective_nodes_expand = objective_nodes.gather(1, value_adjacency.unsqueeze(2).expand(-1, -1, objective_nodes.shape[2]))
        h1 = torch.cat((value_nodes, objective_nodes_expand), 2)
        h2 = F.relu(self.val_obj(h1))
        h_objective_nodes_fow = scatter(h2, value_adjacency, dim=1, reduce=self.pooling_op)

        # objective nodes to head node
        num_objectives = objective_nodes.shape[1]
        head_node_expand = head_node.unsqueeze(1).expand(-1, num_objectives, -1)
        h3 = torch.cat([h_objective_nodes_fow, head_node_expand], 2)
        h4 = F.relu(self.obj_head(h3))
        h_head = self.head_pool(h4)
        
        # head node to head node
        if self.use_opponent_encoding:
            h_head = torch.cat((h_head, opponent_encoding), dim=-1)
        h_head = F.relu(self.head_encoder(h_head))

        # head node to objective nodes
        h_head_expand = h_head.unsqueeze(1).expand(-1, num_objectives, -1)
        h5 = torch.cat([h_objective_nodes_fow, h_head_expand], 2)
        h_objective_nodes_back = F.relu(self.head_obj(h5))

        # objective nodes to value nodes
        h8 = h_objective_nodes_back.gather(1, value_adjacency.unsqueeze(2).expand(-1, -1, h_objective_nodes_back.shape[2]))
        h9 = torch.cat([value_nodes, h8], 2)
        offer_action_logits = F.relu(self.obj_val(h9)).squeeze(-1)

        # head node to accept action
        accept_inf_mask = torch.max(torch.log(accept_mask), torch.Tensor([torch.finfo(torch.float32).min]))
        accept_action_logits = F.relu(self.accept_head(h_head)) + accept_inf_mask

        # head node to value function
        vf_out = self.vf(h_head).squeeze(-1)

        # gather action logits
        action_logits = torch.cat((accept_action_logits, offer_action_logits), dim=-1)

        # return {"vf_preds": vf_out, "action_dist": {"accept": None, "outcome": None}}
        return {"vf_preds": vf_out, "action_dist_inputs": action_logits}
        
    # @override(RLModule)
    # def _forward_train(self, batch: NestedDict) -> Mapping[str, Any]:
    #     pass

    # @override(RLModule)
    # def _forward_inference(self, batch: NestedDict) -> Mapping[str, Any]:
    #     with torch.no_grad():
    #         _, action_dist = self._forward_train(batch)
    #     return {"action": action_dist.sample()}

    # @override(RLModule)
    # def _forward_exploration(self, batch: NestedDict) -> Mapping[str, Any]:
    #     with torch.no_grad():
    #         _, action_dist = self._forward_train(batch)
    
    # @override(RLModule)
    # def output_specs_inference(self):
    #     return ["action_dist"]

    # @override(RLModule)
    # def output_specs_exploration(self):
    #     return ["vf_preds", "action_dist"]

    # @override(RLModule)
    # def output_specs_train(self):
    #     return ["vf_preds", "action_dist"]
class GraphToGraphLargeFixedAction(BaseModel):

    def setup(self):
        self.num_used_agents = self.config.model_config_dict["num_used_agents"]
        self.use_opponent_encoding = self.config.model_config_dict["use_opponent_encoding"]
        self.pooling_op = self.config.model_config_dict["pooling_op"]


        match self.pooling_op:
            case "max":
                self.head_pool = lambda x: torch.max(x, 1)[0]
            case "mean":
                self.head_pool = lambda x: torch.mean(x, 1)
            case "sum":
                self.head_pool = lambda x: torch.sum(x, 1)
            case "min":
                self.head_pool = lambda x: torch.min(x, 1)[0]
            case "mul":
                self.head_pool = lambda x: torch.prod(x, 1)
            case _:
                raise ValueError(f"Pooling op {self.pooling_op} not supported")

        self.val_obj = nn.Linear(6, 30)  # value features + objective features
        self.obj_head = nn.Linear(32, 64)  # prev + objective features
        if self.use_opponent_encoding:
            self.head_encoder = nn.Linear(64 + self.num_used_agents, 64)
        else:
            self.head_encoder = nn.Linear(64, 64)
        self.head_obj = nn.Linear(94, 64)  # prev + output val_obj
        self.obj_val = nn.Linear(68, 1)  # prev + value features

        self.accept_head = nn.Linear(64, 2)
        self.vf = torch.nn.Linear(64, 1)

        action_space = self.config.action_space
        logit_lens = [int(action_space["accept"].n), int(sum(action_space["outcome"].nvec))]
        child_distribution_cls_struct = {
            "accept": TorchCategorical,
            "outcome": TorchMultiCategorical.get_partial_dist_cls(space=action_space["outcome"], input_lens=list(action_space["outcome"].nvec))
        }
        self.action_dist_cls = TorchMultiDistribution.get_partial_dist_cls(
            space=action_space,
            child_distribution_cls_struct=child_distribution_cls_struct,
            input_lens=logit_lens,
        )

    @override(RLModule)
    def _forward_train(self, batch: NestedDict) -> Mapping[str, Any]:
        head_node: Tensor = batch["obs"]["head_node"]
        objective_nodes: Tensor = batch["obs"]["objective_nodes"]
        value_nodes: Tensor = batch["obs"]["value_nodes"]
        value_adjacency: Tensor = batch["obs"]["value_adjacency"]
        opponent_encoding: Tensor = F.one_hot(batch["obs"]["opponent_encoding"], self.num_used_agents)
        accept_mask: Tensor = batch["obs"]["accept_mask"]

        # value nodes to objective nodes
        objective_nodes_expand = objective_nodes.gather(1, value_adjacency.unsqueeze(2).expand(-1, -1, objective_nodes.shape[2]))
        h1 = torch.cat((value_nodes, objective_nodes_expand), 2)
        h2 = F.relu(self.val_obj(h1))
        h_objective_nodes_fow = scatter(h2, value_adjacency, dim=1, reduce=self.pooling_op)

        # objective nodes to head node
        num_objectives = objective_nodes.shape[1]
        head_node_expand = head_node.unsqueeze(1).expand(-1, num_objectives, -1)
        h3 = torch.cat([h_objective_nodes_fow, head_node_expand], 2)
        h4 = F.relu(self.obj_head(h3))
        h_head = self.head_pool(h4)
        
        # head node to head node
        if self.use_opponent_encoding:
            h_head = torch.cat((h_head, opponent_encoding), dim=-1)
        h_head = F.relu(self.head_encoder(h_head))

        # head node to objective nodes
        h_head_expand = h_head.unsqueeze(1).expand(-1, num_objectives, -1)
        h5 = torch.cat([h_objective_nodes_fow, h_head_expand], 2)
        h_objective_nodes_back = F.relu(self.head_obj(h5))

        # objective nodes to value nodes
        h8 = h_objective_nodes_back.gather(1, value_adjacency.unsqueeze(2).expand(-1, -1, h_objective_nodes_back.shape[2]))
        h9 = torch.cat([value_nodes, h8], 2)
        offer_action_logits = F.relu(self.obj_val(h9)).squeeze(-1)

        # head node to accept action
        accept_inf_mask = torch.max(torch.log(accept_mask), torch.Tensor([torch.finfo(torch.float32).min]))
        accept_action_logits = F.relu(self.accept_head(h_head)) + accept_inf_mask

        # head node to value function
        vf_out = self.vf(h_head).squeeze(-1)

        # gather action logits
        action_logits = torch.cat((accept_action_logits, offer_action_logits), dim=-1)

        # return {"vf_preds": vf_out, "action_dist": {"accept": None, "outcome": None}}
        return {"vf_preds": vf_out, "action_dist_inputs": action_logits}

class Test(BaseModel):

    def setup(self):
        self.num_used_agents = self.config.model_config_dict["num_used_agents"]
        self.use_opponent_encoding = self.config.model_config_dict["use_opponent_encoding"]
        self.pooling_op = self.config.model_config_dict["pooling_op"]


        match self.pooling_op:
            case "max":
                self.head_pool = lambda x: torch.max(x, 1)[0]
            case "mean":
                self.head_pool = lambda x: torch.mean(x, 1)
            case "sum":
                self.head_pool = lambda x: torch.sum(x, 1)
            case "min":
                self.head_pool = lambda x: torch.min(x, 1)[0]
            case "mul":
                self.head_pool = lambda x: torch.prod(x, 1)
            case _:
                raise ValueError(f"Pooling op {self.pooling_op} not supported")

        self.val_obj = nn.Linear(6, 30)  # value features + objective features
        self.obj_head = nn.Linear(32, 64)  # prev + objective features
        if self.use_opponent_encoding:
            self.head_encoder = nn.Linear(64 + self.num_used_agents, 64)
        else:
            self.head_encoder = nn.Linear(64, 64)
        self.head_obj = nn.Linear(94, 64)  # prev + output val_obj
        self.obj_val = nn.Linear(68, 1)  # prev + value features

        self.accept_head = nn.Linear(64, 2)
        self.vf = torch.nn.Linear(64, 1)

        self.action_dist_cls = None
        # action_space = self.config.action_space
        # logit_lens = [int(action_space["accept"].n), int(sum(action_space["outcome"].nvec))]

    def get_action_dist_cls(self):
        return TorchMultiCategorical.get_partial_dist_cls(input_lens=self.logit_lens)

    def get_train_action_dist_cls(self):
        return self.get_action_dist_cls()

    def get_exploration_action_dist_cls(self):
        return self.get_action_dist_cls()

    def get_inference_action_dist_cls(self):
        return self.get_action_dist_cls()

    @override(RLModule)
    def _forward_train(self, batch: NestedDict) -> Mapping[str, Any]:
        head_node: Tensor = batch["obs"]["head_node"]
        objective_nodes: Tensor = batch["obs"]["objective_nodes"]
        value_nodes: Tensor = batch["obs"]["value_nodes"]
        value_adjacency: Tensor = batch["obs"]["value_adjacency"]
        opponent_encoding: Tensor = F.one_hot(batch["obs"]["opponent_encoding"], self.num_used_agents)
        accept_mask: Tensor = batch["obs"]["accept_mask"]

        self.logit_lens = [2] + value_adjacency[0].unique(return_counts=True)[1].tolist()

        # value nodes to objective nodes
        objective_nodes_expand = objective_nodes.gather(1, value_adjacency.unsqueeze(2).expand(-1, -1, objective_nodes.shape[2]))
        h1 = torch.cat((value_nodes, objective_nodes_expand), 2)
        h2 = F.relu(self.val_obj(h1))
        h_objective_nodes_fow = scatter(h2, value_adjacency, dim=1, reduce=self.pooling_op)

        # objective nodes to head node
        num_objectives = objective_nodes.shape[1]
        head_node_expand = head_node.unsqueeze(1).expand(-1, num_objectives, -1)
        h3 = torch.cat([h_objective_nodes_fow, head_node_expand], 2)
        h4 = F.relu(self.obj_head(h3))
        h_head = self.head_pool(h4)
        
        # head node to head node
        if self.use_opponent_encoding:
            h_head = torch.cat((h_head, opponent_encoding), dim=-1)
        h_head = F.relu(self.head_encoder(h_head))

        # head node to objective nodes
        h_head_expand = h_head.unsqueeze(1).expand(-1, num_objectives, -1)
        h5 = torch.cat([h_objective_nodes_fow, h_head_expand], 2)
        h_objective_nodes_back = F.relu(self.head_obj(h5))

        # objective nodes to value nodes
        h8 = h_objective_nodes_back.gather(1, value_adjacency.unsqueeze(2).expand(-1, -1, h_objective_nodes_back.shape[2]))
        h9 = torch.cat([value_nodes, h8], 2)
        offer_action_logits = F.relu(self.obj_val(h9)).squeeze(-1)

        # head node to accept action
        accept_inf_mask = torch.max(torch.log(accept_mask), torch.Tensor([torch.finfo(torch.float32).min]))
        accept_action_logits = F.relu(self.accept_head(h_head)) + accept_inf_mask

        # head node to value function
        vf_out = self.vf(h_head).squeeze(-1)

        # gather action logits
        action_logits = torch.cat((accept_action_logits, offer_action_logits), dim=-1)

        # return {"vf_preds": vf_out, "action_dist": {"accept": None, "outcome": None}}
        return {"vf_preds": vf_out, "action_dist_inputs": action_logits}

class GraphToFixed(BaseModel):

    def setup(self):
        action_space = self.config.action_space
        logit_lens = [int(action_space["accept"].n), int(sum(action_space["outcome"].nvec))]
        pi_output_layer_dim = sum(logit_lens)
        self.num_used_agents = self.config.model_config_dict["num_used_agents"]

        match self.config.model_config_dict["pooling_op"]:
            case "max":
                self.pooling_op = lambda x, y, z: torch.max(x, y)[0]
            case "mean":
                self.pooling_op = lambda x, y, z: (torch.sum(x, y) / z)
            case "sum":
                self.pooling_op = lambda x, y, z: torch.sum(x, y)
            case _:
                raise ValueError(f"Pooling op {self.config.model_config_dict['pooling_op']} not supported")

        self.val_obj = nn.Linear(6, 30)  # value features + objective features
        self.obj_head = nn.Linear(32, 32)  # prev + objective features

        self.encoder = nn.Linear(32 + self.num_used_agents, 32)
        self.pi = nn.Linear(32, pi_output_layer_dim)
        self.vf = nn.Linear(32, 1)


        
        child_distribution_cls_struct = {
            "accept": TorchCategorical,
            "outcome": TorchMultiCategorical.get_partial_dist_cls(space=action_space["outcome"], input_lens=list(action_space["outcome"].nvec))
        }
        self.action_dist_cls = TorchMultiDistribution.get_partial_dist_cls(
            space=action_space,
            child_distribution_cls_struct=child_distribution_cls_struct,
            input_lens=logit_lens,
        )

    @override(PPORLModule)
    def get_initial_state(self) -> dict:
        return {}

    @override(RLModule)
    def _forward_train(self, batch: NestedDict) -> Mapping[str, Any]:
        head_node: Tensor = batch["obs"]["head_node"]
        objective_nodes: Tensor = batch["obs"]["objective_nodes"]
        value_nodes: Tensor = batch["obs"]["value_nodes"]
        value_nodes_mask: Tensor = batch["obs"]["value_nodes_mask"]
        opponent_encoding: Tensor = F.one_hot(batch["obs"]["opponent_encoding"], self.num_used_agents)
        accept_mask: Tensor = batch["obs"]["accept_mask"]

        max_num_values = value_nodes.shape[2]
        num_objectives = objective_nodes.shape[1]

        objective_nodes_expand = objective_nodes.unsqueeze(2).expand(
            -1, -1, max_num_values, -1
        )
        h1 = torch.cat([value_nodes, objective_nodes_expand], 3)
        h2 = self.val_obj(h1)
        h3 = F.relu(h2) * value_nodes_mask.unsqueeze(3)

        # h_objective_nodes = torch.sum(h3, 2)
        h_objective_nodes = self.pooling_op(h3, 2, objective_nodes[:, :, :1])
        # h_objective_nodes = torch.sum(h3, 2) / (objective_nodes[:, :, :1] + 1)

        head_node_expand = head_node.unsqueeze(1).expand(-1, num_objectives, -1)
        h4 = self.obj_head(torch.cat([h_objective_nodes, head_node_expand], 2))
        h5 = F.relu(h4)
        
        # h_head = torch.sum(h5, 1)
        # h_head, _ = torch.max(h5, 1)
        h_head = self.pooling_op(h5, 1, head_node[:, :1])
        # h_head = torch.sum(h5, 1) / (head_node[:, :1] + 1)
    
        h_head = torch.cat((h_head, opponent_encoding), dim=-1)
        h_head = F.relu(self.encoder(h_head))

        vf_out = self.vf(h_head).squeeze(-1)
        action_logits = self.pi(h_head)

        accept_inf_mask = torch.max(torch.log(accept_mask), torch.Tensor([torch.finfo(torch.float32).min]))
        action_logits[:, :2] += accept_inf_mask

        return {"vf_preds": vf_out, "action_dist_inputs": action_logits}


class PureGCN(BaseModel):
    def setup(self):
        action_space = self.config.action_space
        self.logit_lens = [int(action_space["accept"].n), int(sum(action_space["outcome"].nvec))]
        self.num_used_agents = self.config.model_config_dict["num_used_agents"]

        hidden_size = self.config.model_config_dict["hidden_size"]

        self.gcn_layers = [GCNConv(hidden_size, hidden_size) for _ in range(self.config.model_config_dict["num_gcn_layers"])]

        self.head_encoder = nn.Linear(2 + self.num_used_agents, hidden_size)
        self.objective_encoder = nn.Linear(2, hidden_size)
        self.value_encoder = nn.Linear(4, hidden_size)

        self.accept_head = nn.Linear(hidden_size, 2)
        self.offer_head = nn.Linear(hidden_size, 1)
        self.vf = torch.nn.Linear(hidden_size, 1)
        
        child_distribution_cls_struct = {
            "accept": TorchCategorical,
            "outcome": TorchMultiCategorical.get_partial_dist_cls(space=action_space["outcome"], input_lens=list(action_space["outcome"].nvec))
        }
        self.action_dist_cls = TorchMultiDistribution.get_partial_dist_cls(
            space=action_space,
            child_distribution_cls_struct=child_distribution_cls_struct,
            input_lens=self.logit_lens,
        )

    @override(RLModule)
    def _forward_train(self, batch: NestedDict) -> Mapping[str, Any]:
        head_node: Tensor = batch["obs"]["head_node"]
        objective_nodes: Tensor = batch["obs"]["objective_nodes"]
        value_nodes: Tensor = batch["obs"]["value_nodes"]
        edge_indices: Tensor = batch["obs"]["edge_indices"]
        opponent_encoding: Tensor = F.one_hot(batch["obs"]["opponent_encoding"], self.num_used_agents)
        accept_mask: Tensor = batch["obs"]["accept_mask"]

        h_head_node = F.relu(self.head_encoder(torch.cat((head_node, opponent_encoding), dim=-1)))
        h_objective_nodes = F.relu(self.objective_encoder(objective_nodes))
        h_value_nodes = F.relu(self.value_encoder(value_nodes))

        h_nodes = torch.cat((h_head_node.unsqueeze(1), h_objective_nodes, h_value_nodes), dim=1)

        for gcn_layer in self.gcn_layers:
            # h_nodes_out = []
            # for h, e in zip(h_nodes, edge_indices):
            #     out = gcn_layer(h, e)
            #     h_nodes_out.append(F.relu(out))
            h_nodes = torch.cat([F.relu(gcn_layer(h, e)).unsqueeze(0) for h, e in zip(h_nodes, edge_indices)], dim=0)
            # h_nodes = torch.cat(h_nodes_out, dim=0)


        h_value_nodes_out = h_nodes[:, -value_nodes.shape[1]:, :]
        offer_action_logits = F.relu(self.offer_head(h_value_nodes_out)).squeeze(-1)
        

        accept_inf_mask = torch.max(torch.log(accept_mask), torch.Tensor([torch.finfo(torch.float32).min]))
        accept_action_logits = F.relu(self.accept_head(h_nodes[:, 0, :])) + accept_inf_mask


        action_logits = torch.cat((accept_action_logits, offer_action_logits), dim=-1)

        vf_out = self.vf(h_nodes[:, 0, :]).squeeze(-1)

        return {"vf_preds": vf_out, "action_dist_inputs": action_logits}


class HigaEtAl(BaseModel):
    def setup(self):
        action_space = self.config.action_space
        observation_space = self.config.observation_space
        logit_lens = [int(action_space["accept"].n), int(sum(action_space["outcome"].nvec))]

        self.encoder = nn.Sequential(
            nn.Linear(spaces.flatdim(observation_space), 64),
            nn.Tanh(),
            nn.Linear(64, 64),
            nn.Tanh()
        )

        self.vf = nn.Sequential(
            nn.Linear(64, 64),
            nn.Tanh(),
            nn.Linear(64, 64),
            nn.Tanh(),
            nn.Linear(64, 1)
        )
        self.pi = nn.Sequential(
            nn.Linear(64, 64),
            nn.Tanh(),
            nn.Linear(64, 64),
            nn.Tanh(),
            nn.Linear(64, sum(logit_lens))
        )


        child_distribution_cls_struct = {
            "accept": TorchCategorical,
            "outcome": TorchMultiCategorical.get_partial_dist_cls(space=action_space["outcome"], input_lens=list(action_space["outcome"].nvec))
        }
        self.action_dist_cls = TorchMultiDistribution.get_partial_dist_cls(
            space=action_space,
            child_distribution_cls_struct=child_distribution_cls_struct,
            input_lens=logit_lens,
        )

    @override(PPORLModule)
    def get_initial_state(self) -> dict:
        return {}

    @override(RLModule)
    def _forward_train(self, batch: NestedDict) -> Mapping[str, Any]:
        H = self.encoder(batch["obs"])

        vf_out = self.vf(H).squeeze(-1)
        action_logits = self.pi(H)

        return {"vf_preds": vf_out, "action_dist_inputs": action_logits}
    

class FixedToFixed(BaseModel):
    def setup(self):
        action_space = self.config.action_space
        observation_space = self.config.observation_space
        logit_lens = [int(action_space["accept"].n), int(sum(action_space["outcome"].nvec))]

        self.encoder = nn.Sequential(
            nn.Linear(spaces.flatdim(observation_space), 64),
            nn.ReLU(),
            nn.Linear(64, 64),
            nn.ReLU()
        )

        self.vf = nn.Sequential(
            nn.Linear(64, 64),
            nn.ReLU(),
            nn.Linear(64, 64),
            nn.ReLU(),
            nn.Linear(64, 1)
        )
        self.pi = nn.Sequential(
            nn.Linear(64, 64),
            nn.ReLU(),
            nn.Linear(64, 64),
            nn.ReLU(),
            nn.Linear(64, sum(logit_lens))
        )


        child_distribution_cls_struct = {
            "accept": TorchCategorical,
            "outcome": TorchMultiCategorical.get_partial_dist_cls(space=action_space["outcome"], input_lens=list(action_space["outcome"].nvec))
        }
        self.action_dist_cls = TorchMultiDistribution.get_partial_dist_cls(
            space=action_space,
            child_distribution_cls_struct=child_distribution_cls_struct,
            input_lens=logit_lens,
        )

    @override(PPORLModule)
    def get_initial_state(self) -> dict:
        return {}

    @override(RLModule)
    def _forward_train(self, batch: NestedDict) -> Mapping[str, Any]:
        H = self.encoder(batch["obs"])

        vf_out = self.vf(H).squeeze(-1)
        action_logits = self.pi(H)

        return {"vf_preds": vf_out, "action_dist_inputs": action_logits}


class FixedToFixed2(BaseModel):
    def setup(self):
        action_space = self.config.action_space
        observation_space = self.config.observation_space
        logit_lens = [int(action_space["accept"].n), int(sum(action_space["outcome"].nvec))]

        self.encoder = nn.Sequential(
            nn.Linear(spaces.flatdim(observation_space), 64),
            nn.ReLU(),
            nn.Linear(64, 64),
            nn.ReLU(),
            nn.Linear(64, 64),
            nn.ReLU()
        )

        self.vf = nn.Linear(64, 1)
        self.pi = nn.Linear(64, sum(logit_lens))


        child_distribution_cls_struct = {
            "accept": TorchCategorical,
            "outcome": TorchMultiCategorical.get_partial_dist_cls(space=action_space["outcome"], input_lens=list(action_space["outcome"].nvec))
        }
        self.action_dist_cls = TorchMultiDistribution.get_partial_dist_cls(
            space=action_space,
            child_distribution_cls_struct=child_distribution_cls_struct,
            input_lens=logit_lens,
        )

    @override(PPORLModule)
    def get_initial_state(self) -> dict:
        return {}

    @override(RLModule)
    def _forward_train(self, batch: NestedDict) -> Mapping[str, Any]:
        H = self.encoder(batch["obs"])

        vf_out = self.vf(H).squeeze(-1)
        action_logits = self.pi(H)

        return {"vf_preds": vf_out, "action_dist_inputs": action_logits}
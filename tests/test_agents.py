from collections import deque

import pytest
import numpy as np
from numpy.random import default_rng

from environment.agents.geniusweb import AGENTS
from environment.negotiation import Deadline
from environment.scenario import Scenario


@pytest.mark.parametrize("agent_class", AGENTS.values())
def test_initialisation(agent_class):
    scenario = Scenario.create_random(400, default_rng())
    agent_class("test", scenario.utility_function_A, Deadline(10000), {})


@pytest.mark.parametrize("agent_class", AGENTS.values())
def test_opening_bid(agent_class):
    scenario = Scenario.create_random(400, default_rng())
    agent = agent_class("test", scenario.utility_function_A, Deadline(10000), {})
    last_actions = deque()
    agent.select_action(last_actions)


@pytest.mark.parametrize("agent_class", AGENTS.values())
def test_accept_first_offer(agent_class):
    last_actions = deque()
    scenario = Scenario.create_random(400, default_rng())
    agent = agent_class("test", scenario.utility_function_A, Deadline(10000), {})
    last_actions = deque()
    action = agent.select_action(last_actions)
    assert isinstance(action, dict)
    last_actions.append(action)
    action = action.copy()
    action["accept"] = 1
    action["agent_id"] = "test_opponent"
    last_actions.append(action)
    agent.final(last_actions)


@pytest.mark.parametrize("agent_class", AGENTS.values())
def test_round_deadline(agent_class):
    scenario = Scenario.create_random(400, default_rng())
    offer = np.array(scenario.utility_function_B.get_max_utility_bid(), dtype=np.int32)
    last_actions = deque()
    bid = {"agent_id": "opponent", "offer": offer, "accept": 0}
    agent = agent_class("test", scenario.utility_function_A, Deadline(10000, 10), {})
    for i in range(9):
        agent.select_action(last_actions)
        if i == 0:
            last_actions.append(bid)
    if hasattr(agent, "progress"):
        assert agent.progress.get(None) < 1
    elif hasattr(agent, "_progress"):
        assert agent._progress.get(None) < 1
    elif hasattr(agent, "_session_progress"):
        assert agent._session_progress.get(None) < 1
    else:
        raise ValueError("Agent does not have progress attribute")
    
    agent.select_action(last_actions)

    if hasattr(agent, "progress"):
        assert agent.progress.get(None) == 1
    elif hasattr(agent, "_progress"):
        assert agent._progress.get(None) == 1
    elif hasattr(agent, "_session_progress"):
        assert agent._session_progress.get(None) == 1
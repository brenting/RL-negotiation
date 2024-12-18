from .agent007.agent007 import Agent007
from .agent4410.agent_4410 import Agent4410
from .agentfish.agentfish import AgentFish
from .AgentFO2.AgentFO2 import AgentFO2
from .BIU_agent.BIU_agent import BIU_agent
from .charging_boul.charging_boul import ChargingBoul
from .compromising_agent.compromising_agent import CompromisingAgent
from .dreamteam109_agent.dreamteam109_agent import DreamTeam109Agent
from .gea_agent.gea_agent import GEAAgent
from .learning_agent.learning_agent import LearningAgent
from .LuckyAgent2022.LuckyAgent2022 import LuckyAgent2022
from .micro_agent.micro_agent.micro_agent import MiCROAgent
# from .Pinar_Agent.Pinar_Agent import Pinar_Agent
# from .procrastin_agent.procrastin_agent import ProcrastinAgent
from .rg_agent.rg_agent import RGAgent
from .smart_agent.smart_agent import SmartAgent
from .super_agent.super_agent import SuperAgent
from .thirdagent.third_agent import ThirdAgent
from .tjaronchery10_agent.tjaronchery10_agent import Tjaronchery10Agent

AGENTS = {
    "Agent007": Agent007,
    "Agent4410": Agent4410,
    "AgentFish": AgentFish,
    "AgentFO2": AgentFO2,
    # "BIU_agent": BIU_agent, #NOTE: times out >60 secs
    "ChargingBoul": ChargingBoul,
    # "CompromisingAgent": CompromisingAgent, #NOTE: causes Action cannot be None errors
    "DreamTeam109Agent": DreamTeam109Agent,
    # "GEAAgent": GEAAgent, #NOTE: very slow, a turn takes ~1.5sec
    # "LearningAgent": LearningAgent, #NOTE: causes Action cannot be None errors
    "LuckyAgent2022": LuckyAgent2022,
    "MiCROAgent": MiCROAgent,
    # "Pinar_Agent": Pinar_Agent, #NOTE: requires lightgbm package
    # "ProcrastinAgent": ProcrastinAgent, #NOTE: can't handle first offer accepted
    "RGAgent": RGAgent,
    "SmartAgent": SmartAgent,
    "SuperAgent": SuperAgent,
    "ThirdAgent": ThirdAgent,
    "Tjaronchery10Agent": Tjaronchery10Agent,
}

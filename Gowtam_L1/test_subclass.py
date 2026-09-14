from src.strategy.protocol import Strategy as ProtocolStrategy
from candidates.hypothesis_md_learning_the_dynamics_of_t_20260911_153217_480131.train import Strategy as WorkerStrategy

print("Is subclass:", issubclass(WorkerStrategy, ProtocolStrategy))

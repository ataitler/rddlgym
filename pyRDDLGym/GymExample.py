'''In this example, a random policy is constructed and its performance is
evaluated on a specified domain. 

The syntax for running this example is:

    python GymExample.py <domain> <instance> [<episodes>] [<seed>]
    
where:
    <domain> is the name of a domain located in the /Examples directory
    <instance> is the instance number
    <episodes> is a positive integer for the number of episodes to simulate
    (defaults to 1)
    <seed> is a positive integer RNG key (defaults to 42)
'''
import sys

from pyRDDLGym import ExampleManager
from pyRDDLGym import RDDLEnv
from pyRDDLGym.Core.Policies.Agents import RandomAgent


def main(domain, instance, episodes=1, seed=42):
    
    # get the environment info
    EnvInfo = ExampleManager.GetEnvInfo(domain)
    
    # set up the environment, RNG key and visualizer
    env = RDDLEnv.RDDLEnv(domain=EnvInfo.get_domain(),
                          instance=EnvInfo.get_instance(instance))
    env.seed(seed)
    env.set_visualizer(EnvInfo.get_visualizer())
    
    # set up an example agent
    agent = RandomAgent(action_space=env.action_space,
                        num_actions=env.numConcurrentActions,
                        seed=seed)
    
    # main evaluation loop
    agent.evaluate(env, episodes=episodes, verbose=True, render=True)
    
    # important when logging to save all traces
    env.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) < 2:
        print('python GymExample.py <domain> <instance> [<episodes>] [<seed>]')
        exit(0)
    kwargs = {'domain': args[0], 'instance': args[1]}
    if len(args) >= 3: kwargs['episodes'] = int(args[2])
    if len(args) >= 4: kwargs['seed'] = int(args[3])
    main(**kwargs)

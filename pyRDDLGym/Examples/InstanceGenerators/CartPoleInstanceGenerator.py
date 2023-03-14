import os
from typing import Dict

from pyRDDLGym.Examples.InstanceGenerator import InstanceGenerator


class CartPoleInstanceGenerator(InstanceGenerator):
    
    def get_env_path(self) -> str:
        return os.path.join('CartPole', 'Continuous')
    
    def get_domain_name(self) -> str:
        return 'cart_pole_continuous'
    
    def generate_rddl_variables(self, params: Dict[str, object]) -> Dict[str, object]:
        nonfluent_keys = ['POLE-LEN', 'CART-FRICTION', 'POLE-FRICTION',
                          'IMPULSE-VAR', 'ANGLE-VAR']
        state_keys = ['pos', 'vel', 'ang-pos', 'ang-vel']
        
        return {
            'objects': {},
            'non-fluents': {key: params[key] for key in nonfluent_keys if key in params},
            'init-states': {key: params[key] for key in state_keys if key in params},
            'horizon': 200,
            'discount': 1.0,
            'max-nondef-actions': 'pos-inf'
        }


params = [
    
    # regular cart-pole
    {'POLE-LEN': 0.5, 'CART-FRICTION': 0.0, 'POLE-FRICTION': 0.0,
     'IMPULSE-VAR': 0.0, 'ANGLE-VAR': 0.0},
    
    # cart-pole with long pole
    {'POLE-LEN': 3.0, 'CART-FRICTION': 0.0, 'POLE-FRICTION': 0.0,
     'IMPULSE-VAR': 0.0, 'ANGLE-VAR': 0.0},
    
    # cart-pole with friction + impulse noise
    {'POLE-LEN': 0.5, 'CART-FRICTION': 0.0005, 'POLE-FRICTION': 0.000002,
     'IMPULSE-VAR': 16.0, 'ANGLE-VAR': 0.0},
    
    # cart-pole with friction + sensor noise
    {'POLE-LEN': 0.5, 'CART-FRICTION': 0.0005, 'POLE-FRICTION': 0.000002,
     'IMPULSE-VAR': 0.0, 'ANGLE-VAR': 0.001},
    
    # cart-pole with friction + sensor noise + impulse noise
    {'POLE-LEN': 0.5, 'CART-FRICTION': 0.0005, 'POLE-FRICTION': 0.000002,
     'IMPULSE-VAR': 16.0, 'ANGLE-VAR': 0.001}
]
        
inst = CartPoleInstanceGenerator()
inst.save_instances(params)
